import asyncio
import base64
import ipaddress
import json
import logging
import os
import secrets
import threading
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Optional

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, ai, db, documents, scheduler

STATIC = Path(__file__).resolve().parent.parent / "static"


log = logging.getLogger("uvicorn.error")

# Optional password, for when the app is reachable by others (a home server, a VPS).
PASSWORD = os.environ.get("HOUSEKEEPING_PASSWORD", "")
USERNAME = os.environ.get("HOUSEKEEPING_USERNAME") or "admin"
# Host names the app answers to, besides IP addresses, localhost and local names like nas.local.
ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("HOUSEKEEPING_ALLOWED_HOSTS", "").split(",") if h.strip()]
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".home.arpa", ".internal")
MAX_REQUEST_BYTES = documents.MAX_BYTES + 4 * 1024 * 1024  # the largest upload plus form overhead
AUTH_FAILURE_DELAY = 1.0  # seconds; failed sign-ins are answered one at a time, so guessing is slow


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if not PASSWORD:
        log.warning("Sign-in is off. Anyone who can reach this port can see your data and use your API credit. "
                    "Set HOUSEKEEPING_PASSWORD unless the app is only reachable from this computer.")
    yield


app = FastAPI(title="Housekeeping", version=__version__, lifespan=lifespan)


def _host_allowed(host_header: str) -> bool:
    """Without this, a web page on any site can use DNS rebinding to talk to the app from your browser."""
    host = urllib.parse.urlsplit("//" + host_header).hostname or ""
    if not host:
        return False
    if "*" in ALLOWED_HOSTS or host in ALLOWED_HOSTS or any(h.startswith(".") and host.endswith(h) for h in ALLOWED_HOSTS):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES)


def _cross_site(request: Request) -> bool:
    """True for a state-changing request sent by another website's page (CSRF)."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("cross-site", "same-site")
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin == "null"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urllib.parse.urlsplit(origin).netloc.lower() != host.lower()


_auth_gate: Optional[asyncio.Lock] = None
_auth_waiting = 0


async def _failed_sign_in() -> Response:
    global _auth_gate, _auth_waiting
    if _auth_waiting >= 20:
        return Response("Too many sign-in attempts. Wait a minute and try again.", 429)
    _auth_gate = _auth_gate or asyncio.Lock()
    _auth_waiting += 1
    try:
        async with _auth_gate:
            await asyncio.sleep(AUTH_FAILURE_DELAY)
    finally:
        _auth_waiting -= 1
    return Response("Sign in to use Housekeeping.", 401, {"WWW-Authenticate": 'Basic realm="Housekeeping"'})


def _signed_in(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user.encode(), USERNAME.encode()) & secrets.compare_digest(pw.encode(), PASSWORD.encode())


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path == "/api/health":
        return await call_next(request)
    host = request.headers.get("host", "")
    if (not PASSWORD or ALLOWED_HOSTS) and not _host_allowed(host):
        return Response(f"Housekeeping doesn't answer to the address {host!r}. If that's how you reach it, "
                        "add the name to HOUSEKEEPING_ALLOWED_HOSTS.", 400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if _cross_site(request):
            return Response("Requests from other websites aren't allowed.", 403)
        length = request.headers.get("content-length")
        if length is None and "chunked" in request.headers.get("transfer-encoding", "").lower():
            return Response("Send a Content-Length header.", 411)
        if length and (not length.isdigit() or int(length) > MAX_REQUEST_BYTES):
            return Response("That upload is larger than 60 MB.", 413)
    if PASSWORD and not _signed_in(request):
        return await _failed_sign_in()
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # Check for a newer copy on every load (cheap: unchanged files come back as 304), so after an update
        # the browser never pairs the new page with an old stylesheet or script.
        response.headers.setdefault("Cache-Control", "no-cache")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


clock = date.today  # tests replace this to fix "today"


def today(override: Optional[str] = None) -> date:
    if not override:
        return clock()
    try:
        return date.fromisoformat(override)
    except ValueError:
        raise HTTPException(400, "as_of must be a date like 2026-11-20")


def _require(conn, table: str, row_id: int, what: str):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"{what} not found")
    return row


# ---------- models ----------

class AssetIn(BaseModel):
    category: Literal["appliance", "home", "outdoor", "plant", "vehicle"]
    name: str = Field(min_length=1, max_length=200)
    details: str = ""
    year: Optional[int] = Field(None, ge=1800, le=2200)
    mileage: Optional[int] = Field(None, ge=0)
    notes: str = ""


class TaskIn(BaseModel):
    asset_id: Optional[int] = None
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    recurrence: Literal["interval", "seasonal", "mileage", "once"] = "interval"
    interval_days: Optional[int] = Field(None, ge=1, le=36500)
    months: Optional[str] = None
    mileage_interval: Optional[int] = Field(None, ge=1)
    priority: Literal[1, 2, 3] = 2
    est_minutes: Optional[int] = Field(None, ge=0, le=100000)
    status: Literal["active", "paused", "suggested"] = "active"
    source: str = "manual"
    due_date: Optional[date] = None  # one-off tasks only: the deadline


class CompletionIn(BaseModel):
    task_id: int
    done_on: Optional[date] = None
    mileage: Optional[int] = Field(None, ge=0)
    notes: str = ""


class ApproveIn(BaseModel):
    last_done_on: Optional[date] = None  # when you last did it, if you remember


class GuideIn(BaseModel):
    text: str


TASK_FIELDS = ["asset_id", "title", "description", "recurrence", "interval_days", "months",
               "mileage_interval", "priority", "est_minutes", "status", "source", "due_date"]


def _task_values(t: "TaskIn") -> list:
    return [v.isoformat() if isinstance(v, date) else v for v in (getattr(t, f) for f in TASK_FIELDS)]


def _validate_task(t: TaskIn):
    """Checks that need more than one field. Also tidies months to a sorted list of valid months."""
    if t.recurrence == "interval" and not t.interval_days:
        raise HTTPException(400, "Interval tasks need interval_days")
    if t.recurrence == "seasonal":
        months = scheduler.parse_months(t.months)
        if not months:
            raise HTTPException(400, "Seasonal tasks need at least one month (1-12)")
        t.months = ",".join(str(m) for m in sorted(months))
    if t.recurrence == "mileage" and not t.mileage_interval:
        raise HTTPException(400, "Mileage tasks need mileage_interval")
    if t.recurrence == "once":
        t.interval_days = t.months = t.mileage_interval = None
    else:
        t.due_date = None


def _insert_suggestions(conn, suggestions, asset_id_for, source: str) -> int:
    """Store Claude's suggestions for review. Any that wouldn't make a workable task are dropped here,
    so approving one can never produce a task that is always due or never due."""
    n = 0
    for s in suggestions:
        try:
            t = TaskIn(asset_id=asset_id_for(s), title=s.title, description=s.description, recurrence=s.recurrence,
                       interval_days=s.interval_days, months=",".join(str(m) for m in s.months or []) or None,
                       mileage_interval=s.mileage_interval, priority=s.priority, est_minutes=s.est_minutes,
                       status="suggested", source=source)
            _validate_task(t)
        except (ValidationError, HTTPException):
            continue
        conn.execute(
            f"INSERT INTO tasks ({', '.join(TASK_FIELDS)}) VALUES ({', '.join('?' * len(TASK_FIELDS))})",
            _task_values(t),
        )
        n += 1
    return n


def _ai_errors(fn):
    try:
        return fn()
    except anthropic.AuthenticationError:
        raise HTTPException(503, "No valid Anthropic credentials. Set ANTHROPIC_API_KEY or run `ant auth login`, then restart.")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Rate limited by the Anthropic API. Try again in a minute.")
    except anthropic.APIConnectionError:
        raise HTTPException(503, "Couldn't reach the Anthropic API. Check your connection.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Anthropic API error: {e.message}")
    except anthropic.AnthropicError as e:  # e.g. no credentials configured at all
        raise HTTPException(503, f"Anthropic client error: {e}")
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except TypeError as e:
        if "authentication" not in str(e):
            raise
        raise HTTPException(503, "Claude isn't set up yet. Set ANTHROPIC_API_KEY or run `ant auth login`, then restart the app.")


# ---------- status ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    return {
        "version": __version__,
        "ai_configured": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "model": ai.MODEL,
    }


# ---------- settings ----------

@app.get("/api/settings")
def read_settings():
    with db.connect() as conn:
        return db.get_settings(conn)


SETTING_CHOICES = {"hemisphere": {"north", "south"}, "plan_mode": {"daily", "weekly"}}


@app.put("/api/settings")
def write_settings(values: dict[str, str]):
    values = {k: str(v).strip() for k, v in values.items() if k in db.DEFAULT_SETTINGS}
    for k, choices in SETTING_CHOICES.items():
        if k in values and values[k] not in choices:
            raise HTTPException(400, f"{k} must be one of: {', '.join(sorted(choices))}")
    if "plan_count" in values and not (values["plan_count"].isdigit() and 1 <= int(values["plan_count"]) <= 50):
        raise HTTPException(400, "plan_count must be a whole number from 1 to 50")
    with db.connect() as conn:
        for k, v in values.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        return db.get_settings(conn)


# ---------- assets ----------

@app.get("/api/assets")
def list_assets():
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT a.*, (SELECT COUNT(*) FROM tasks t WHERE t.asset_id = a.id AND t.status = 'active') AS task_count,
                      (SELECT COUNT(*) FROM tasks t WHERE t.asset_id = a.id AND t.status = 'suggested') AS suggested_count,
                      (SELECT COUNT(*) FROM documents d WHERE d.asset_id = a.id) AS doc_count
               FROM assets a ORDER BY a.category, a.name"""
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/assets")
def create_asset(a: AssetIn):
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO assets (category, name, details, year, mileage, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (a.category, a.name, a.details, a.year, a.mileage, a.notes),
        )
        return dict(conn.execute("SELECT * FROM assets WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.post("/api/assets/scan")
async def scan_asset(file: UploadFile = File(...)):
    """Read a photo of a label or the item itself and return the fields for a new item. Nothing is saved."""
    data = await file.read(documents.MAX_BYTES + 1)
    mime = documents.sniff_mime(data, file.content_type or "")
    if mime not in documents.IMAGE_TYPES:
        raise HTTPException(400, "Choose a photo (JPEG, PNG, WebP or GIF).")
    if (len(data) + 2) // 3 * 4 > 5 * 1024 * 1024:  # the API's 5 MB limit applies to the base64 encoding
        raise HTTPException(400, "That photo is larger than 3.75 MB. Try a smaller one.")
    s = _ai_errors(lambda: ai.scan_item(data, mime))
    if not s.readable:
        raise HTTPException(422, s.problem or "Couldn't make out the item in that photo. Try a closer, sharper one.")
    notes = [f"Serial {s.serial}"] if s.serial else []
    notes += [f"{p.name}: {p.spec}" for p in s.specs]
    return {
        "category": s.category,
        "name": s.name,
        "details": " ".join(x for x in (s.make, s.model) if x),
        "year": s.year,
        "year_basis": s.year_basis,
        "mileage": s.mileage if s.category == "vehicle" else None,
        "notes": " · ".join(notes),
        "label_notes": ai.label_notes(s).model_dump(),
    }


@app.put("/api/assets/{asset_id}")
def update_asset(asset_id: int, a: AssetIn):
    with db.connect() as conn:
        _require(conn, "assets", asset_id, "Item")
        conn.execute(
            "UPDATE assets SET category=?, name=?, details=?, year=?, mileage=?, notes=? WHERE id=?",
            (a.category, a.name, a.details, a.year, a.mileage, a.notes, asset_id),
        )
        return dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone())


@app.delete("/api/assets/{asset_id}")
def delete_asset(asset_id: int):
    with db.connect() as conn:
        for d in conn.execute("SELECT * FROM documents WHERE asset_id = ?", (asset_id,)).fetchall():
            _remove_document(dict(d))
        conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    return {"ok": True}


@app.post("/api/assets/{asset_id}/suggest")
def suggest_for_asset(asset_id: int, as_of: Optional[str] = None):
    with db.connect() as conn:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            raise HTTPException(404, "Asset not found")
        existing = [r["title"] for r in conn.execute(
            "SELECT title FROM tasks WHERE asset_id = ? AND status != 'paused'", (asset_id,))]
        settings = db.get_settings(conn)
        doc_notes = [(r["title"], json.loads(r["notes"])) for r in conn.execute(
            "SELECT title, notes FROM documents WHERE asset_id = ? AND notes IS NOT NULL", (asset_id,))]
    suggestions = _ai_errors(lambda: ai.suggest_for_asset(dict(asset), existing, settings, today(as_of), doc_notes))
    with db.connect() as conn:
        n = _insert_suggestions(conn, suggestions, lambda s: asset_id, "ai")
    return {"added": n}


@app.post("/api/guide")
def import_guide(g: GuideIn, as_of: Optional[str] = None):
    if not g.text.strip():
        raise HTTPException(400, "Paste your guide first")
    with db.connect() as conn:
        assets = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM assets")}
        existing = [r["title"] for r in conn.execute("SELECT title FROM tasks WHERE status != 'paused'")]
        settings = db.get_settings(conn)
    suggestions = _ai_errors(lambda: ai.parse_guide(g.text, list(assets), existing, settings, today(as_of)))
    with db.connect() as conn:
        n = _insert_suggestions(conn, suggestions, lambda s: assets.get(s.asset_name or ""), "guide")
    return {"added": n}


# ---------- documents ----------

class LinkIn(BaseModel):
    url: str
    asset_id: Optional[int] = None
    title: str = ""
    kind: str = "manual"


def _doc_row(r) -> dict:
    d = dict(r)
    d["notes"] = json.loads(d["notes"]) if d["notes"] else None
    return d


def _get_doc(conn, doc_id: int) -> dict:
    r = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Document not found")
    return dict(r)


def _remove_document(doc: dict):
    documents.delete_file(doc)
    ai.forget_file(doc.get("anthropic_file_id"))


def _remember_file(doc_id: int, file_id: str):
    with db.connect() as conn:
        conn.execute("UPDATE documents SET anthropic_file_id = ? WHERE id = ?", (file_id, doc_id))


def _read_and_store(doc_id: int) -> Optional[str]:
    """Have Claude read a stored document and save its notes. Returns an error message instead of raising,
    so a document you just added is kept even when reading fails."""
    with db.connect() as conn:
        doc = _get_doc(conn, doc_id)
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (doc["asset_id"],)).fetchone() if doc["asset_id"] else None
    try:
        notes, mode, file_id = _ai_errors(lambda: ai.read_document(
            doc, documents.path_for(doc), dict(asset) if asset else None, on_upload=lambda fid: _remember_file(doc_id, fid)))
    except HTTPException as e:
        return e.detail
    except Exception as e:  # never lose a stored document to an unexpected error while reading it
        log.exception("Reading document %s failed", doc_id)
        return f"Reading it failed unexpectedly ({type(e).__name__}: {e})"
    with db.connect() as conn:
        conn.execute("UPDATE documents SET notes = ?, read_mode = ?, anthropic_file_id = ? WHERE id = ?",
                     (notes.model_dump_json(), mode, file_id, doc_id))
    return None


def _store(**kwargs) -> int:
    try:
        with db.connect() as conn:
            return documents.save(conn, **kwargs)
    except documents.DocumentError as e:
        raise HTTPException(400, str(e))


@app.get("/api/documents")
def list_documents(asset_id: Optional[int] = None):
    with db.connect() as conn:
        q = """SELECT d.*, a.name AS asset_name, a.category AS asset_category
               FROM documents d LEFT JOIN assets a ON a.id = d.asset_id"""
        rows = conn.execute(q + " WHERE d.asset_id = ? ORDER BY d.id", (asset_id,)) if asset_id \
            else conn.execute(q + " ORDER BY a.category, a.name, d.id")
        return [_doc_row(r) for r in rows]


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...), asset_id: Optional[int] = Form(None),
                          title: str = Form(""), kind: str = Form("manual"), read: bool = Form(True),
                          notes: str = Form("")):
    data = await file.read(documents.MAX_BYTES + 1)
    if len(data) > documents.MAX_BYTES:
        raise HTTPException(400, "That file is larger than 60 MB.")
    mime = documents.sniff_mime(data, file.content_type or "")
    if mime is None:
        raise HTTPException(400, "Upload a PDF, an image (PNG, JPEG, WebP, GIF), a text file or a saved web page.")
    parsed = None
    if notes:  # already read, e.g. the label photo from a scan
        try:
            parsed = ai.DocNotes.model_validate_json(notes)
        except ValueError:
            raise HTTPException(400, "notes must match the document notes format")
    if mime.startswith("image/") and kind == "manual":
        kind = "photo"
    doc_id = _store(data=data, mime=mime, filename=file.filename or "upload", title=title or Path(file.filename or "").stem,
                    asset_id=asset_id, kind=kind, source="uploaded")
    error = None
    if parsed:
        with db.connect() as conn:
            conn.execute("UPDATE documents SET notes = ?, read_mode = 'full' WHERE id = ?", (parsed.model_dump_json(), doc_id))
    elif read:
        error = _read_and_store(doc_id)
    with db.connect() as conn:
        return {"document": _doc_row(conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()), "read_error": error}


@app.post("/api/documents/link")
def add_document_link(body: LinkIn):
    try:
        data, mime, filename = documents.download(body.url.strip())
    except documents.DocumentError as e:
        raise HTTPException(400, str(e))
    doc_id = _store(data=data, mime=mime, filename=filename, title=body.title, asset_id=body.asset_id,
                    kind=body.kind, source="link", source_url=body.url.strip())
    error = _read_and_store(doc_id)
    with db.connect() as conn:
        return {"document": _doc_row(conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()), "read_error": error}


_searching: set[int] = set()
_searching_lock = threading.Lock()


@app.post("/api/assets/{asset_id}/find-documents")
def find_documents(asset_id: int):
    """Claude searches the web for this item's manuals; the app downloads and reads each one it finds."""
    with _searching_lock:
        if asset_id in _searching:
            raise HTTPException(409, "Already searching for this item's manuals.")
        _searching.add(asset_id)
    try:
        return _find_documents(asset_id)
    finally:
        _searching.discard(asset_id)


def _find_documents(asset_id: int):
    with db.connect() as conn:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            raise HTTPException(404, "Asset not found")
        settings = db.get_settings(conn)
        known = {r["source_url"] for r in conn.execute("SELECT source_url FROM documents WHERE asset_id = ?", (asset_id,))}
    found = _ai_errors(lambda: ai.find_documents(dict(asset), settings))
    added, skipped = [], []
    for f in found:
        if f.url in known:
            skipped.append({"title": f.title, "url": f.url, "reason": "Already saved"})
            continue
        known.add(f.url)  # Claude sometimes reports the same link twice
        try:
            data, mime, filename = documents.download(f.url)
            doc_id = _store(data=data, mime=mime, filename=filename, title=f.title, asset_id=asset_id,
                            kind=f.kind, source="found", source_url=f.url)
        except (documents.DocumentError, HTTPException) as e:
            skipped.append({"title": f.title, "url": f.url, "reason": getattr(e, "detail", None) or str(e)})
            continue
        read_error = _read_and_store(doc_id)
        if not read_error:
            with db.connect() as conn:
                doc = _get_doc(conn, doc_id)
                notes = json.loads(doc["notes"])
                # Search results are only worth keeping if they say how to maintain the item.
                # Spec sheets and parts pages get read, judged and dropped.
                if not notes["maintenance"]:
                    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            if not notes["maintenance"]:
                _remove_document(doc)
                skipped.append({"title": f.title, "url": f.url, "reason": f"no maintenance instructions ({notes['document_type']})"})
                continue
        added.append({"id": doc_id, "title": f.title, "why": f.why, "read_error": read_error})
    return {"found": len(found), "added": added, "skipped": skipped}


@app.post("/api/documents/{doc_id}/read")
def reread_document(doc_id: int):
    error = _read_and_store(doc_id)
    if error:
        raise HTTPException(502, error)
    with db.connect() as conn:
        return _doc_row(conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())


@app.post("/api/documents/{doc_id}/tasks")
def tasks_from_document(doc_id: int, as_of: Optional[str] = None):
    """Item documents: re-run suggestions using all of that item's manuals. General guides: turn the guide into tasks."""
    with db.connect() as conn:
        doc = _get_doc(conn, doc_id)
    if doc["asset_id"]:
        return suggest_for_asset(doc["asset_id"], as_of)
    with db.connect() as conn:
        assets = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM assets")}
        existing = [r["title"] for r in conn.execute("SELECT title FROM tasks WHERE status != 'paused'")]
        settings = db.get_settings(conn)

    def run():
        block, mode, file_id = ai.document_block(doc, documents.path_for(doc))
        if file_id and file_id != doc.get("anthropic_file_id"):
            with db.connect() as conn:
                conn.execute("UPDATE documents SET anthropic_file_id = ? WHERE id = ?", (file_id, doc_id))
        return ai.parse_guide(None, list(assets), existing, settings, today(as_of), attachment=block)

    suggestions = _ai_errors(run)
    with db.connect() as conn:
        n = _insert_suggestions(conn, suggestions, lambda s: assets.get(s.asset_name or ""), "guide")
    return {"added": n}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int):
    with db.connect() as conn:
        doc = _get_doc(conn, doc_id)
    path = documents.path_for(doc)
    if not path.exists():
        raise HTTPException(404, "The file is missing from the documents folder.")
    # Stored files came from the internet; never let one run as a page on this site. (PDFs are exempt
    # because browsers won't show a PDF under a sandbox policy, and a PDF can't script the page.)
    headers = {} if doc["mime"] == "application/pdf" else {"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'"}
    return FileResponse(path, media_type=doc["mime"], content_disposition_type="inline", filename=doc["filename"], headers=headers)


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    with db.connect() as conn:
        doc = _get_doc(conn, doc_id)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    _remove_document(doc)
    return {"ok": True}


# ---------- tasks ----------

@app.get("/api/tasks")
def list_tasks(as_of: Optional[str] = None):
    d = today(as_of)
    with db.connect() as conn:
        status = {t["id"]: t for t in scheduler.all_task_status(conn, d)}
        rows = conn.execute(
            """SELECT t.*, a.name AS asset_name, a.category AS asset_category,
                      (SELECT MAX(done_on) FROM completions c WHERE c.task_id = t.id) AS last_done_on
               FROM tasks t LEFT JOIN assets a ON a.id = t.asset_id
               ORDER BY t.status, a.category, a.name, t.title"""
        ).fetchall()
        out = []
        for r in rows:
            t = dict(r)
            if t["id"] in status:
                s = status[t["id"]]
                t.update(score=s["score"], reason=s["reason"], due_on=s["due_on"])
            out.append(t)
        return out


@app.post("/api/tasks")
def create_task(t: TaskIn):
    _validate_task(t)
    with db.connect() as conn:
        cur = conn.execute(
            f"INSERT INTO tasks ({', '.join(TASK_FIELDS)}) VALUES ({', '.join('?' * len(TASK_FIELDS))})",
            _task_values(t),
        )
        # A one-off job you add by hand that's already due goes straight into this period's plan.
        planned = t.recurrence == "once" and t.status == "active" and scheduler.add_if_due(conn, db.get_settings(conn), clock(), cur.lastrowid)
        return {**dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()), "planned": planned}


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, t: TaskIn):
    _validate_task(t)
    with db.connect() as conn:
        _require(conn, "tasks", task_id, "Task")
        conn.execute(
            f"UPDATE tasks SET {', '.join(f + ' = ?' for f in TASK_FIELDS)} WHERE id = ?",
            _task_values(t) + [task_id],
        )
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


@app.post("/api/tasks/{task_id}/approve")
def approve_task(task_id: int, body: ApproveIn):
    with db.connect() as conn:
        task = dict(_require(conn, "tasks", task_id, "Task"))
        if task["status"] != "suggested":
            return {"ok": True}  # already approved, e.g. a double click; don't log the history twice
        try:
            _validate_task(TaskIn(**{f: task[f] for f in TASK_FIELDS}))
        except (ValidationError, HTTPException):
            raise HTTPException(400, "This suggestion is missing its schedule. Edit it before approving.")
        conn.execute("UPDATE tasks SET status = 'active' WHERE id = ?", (task_id,))
        if body.last_done_on:
            conn.execute("INSERT INTO completions (task_id, done_on, notes) VALUES (?, ?, 'Initial history')",
                         (task_id, body.last_done_on.isoformat()))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/status/{status}")
def set_task_status(task_id: int, status: str):
    if status not in ("active", "paused"):
        raise HTTPException(400, "status must be active or paused")
    with db.connect() as conn:
        _require(conn, "tasks", task_id, "Task")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/snooze")
def snooze_task(task_id: int, days: int = Query(7, ge=1, le=3650), as_of: Optional[str] = None):
    d = today(as_of)
    until = d + timedelta(days=days)
    with db.connect() as conn:
        _require(conn, "tasks", task_id, "Task")
        conn.execute("UPDATE tasks SET snoozed_until = ? WHERE id = ?", (until.isoformat(), task_id))
        # Free its slot in the current plan, unless it was already done this period.
        period, start, end = scheduler.period_for(db.get_settings(conn).get("plan_mode", "weekly"), d)
        done = conn.execute("SELECT 1 FROM completions WHERE task_id = ? AND done_on BETWEEN ? AND ?",
                            (task_id, start.isoformat(), end.isoformat())).fetchone()
        if not done:
            conn.execute("DELETE FROM plan_items WHERE task_id = ? AND period = ?", (task_id, period))
    return {"snoozed_until": until.isoformat()}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}


# ---------- completions ----------

@app.post("/api/completions")
def complete(c: CompletionIn, as_of: Optional[str] = None):
    done_on = (c.done_on or today(as_of)).isoformat()
    with db.connect() as conn:
        task = _require(conn, "tasks", c.task_id, "Task")
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (task["asset_id"],)).fetchone() if task["asset_id"] else None
        vehicle = asset is not None and asset["category"] == "vehicle"
        mileage, prev = c.mileage, None
        if mileage is None and task["recurrence"] == "mileage" and vehicle and asset["mileage"]:
            mileage = asset["mileage"]  # done now with no reading: count miles from the car's last known odometer
        elif c.mileage and vehicle and c.mileage > (asset["mileage"] or 0):
            prev = asset["mileage"] if asset["mileage"] is not None else -1  # -1: the car had no reading before
        cur = conn.execute("INSERT INTO completions (task_id, done_on, mileage, notes, prev_asset_mileage) VALUES (?, ?, ?, ?, ?)",
                           (c.task_id, done_on, mileage, c.notes, prev))
        conn.execute("UPDATE tasks SET snoozed_until = NULL WHERE id = ?", (c.task_id,))
        # A mileage reading on a vehicle task also updates the vehicle's odometer.
        if prev is not None:
            conn.execute("UPDATE assets SET mileage = ? WHERE id = ?", (c.mileage, asset["id"]))
        return {"id": cur.lastrowid}


@app.delete("/api/completions/{completion_id}")
def undo_completion(completion_id: int):
    with db.connect() as conn:
        c = _require(conn, "completions", completion_id, "Completion")
        conn.execute("DELETE FROM completions WHERE id = ?", (completion_id,))
        # Put the odometer back if this completion moved it and nothing has changed it since.
        if c["prev_asset_mileage"] is not None:
            conn.execute(
                "UPDATE assets SET mileage = ? WHERE id = (SELECT asset_id FROM tasks WHERE id = ?) AND mileage = ?",
                (None if c["prev_asset_mileage"] < 0 else c["prev_asset_mileage"], c["task_id"], c["mileage"]))
    return {"ok": True}


@app.get("/api/history")
def history(limit: int = Query(200, ge=1, le=5000)):
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT c.*, t.title, a.name AS asset_name FROM completions c
               JOIN tasks t ON t.id = c.task_id LEFT JOIN assets a ON a.id = t.asset_id
               ORDER BY c.done_on DESC, c.id DESC LIMIT ?""", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- plan ----------

@app.get("/api/plan")
def plan(as_of: Optional[str] = None):
    """With as_of, a preview: the plan that date would get, computed without saving anything."""
    with db.connect() as conn:
        return scheduler.get_plan(conn, db.get_settings(conn), today(as_of), persist=as_of is None)


def _no_preview(as_of: Optional[str]):
    if as_of:
        raise HTTPException(400, "Previews are read-only. Remove as_of to change the plan.")


@app.post("/api/plan/regenerate")
def regenerate_plan(as_of: Optional[str] = None):
    _no_preview(as_of)
    with db.connect() as conn:
        return scheduler.get_plan(conn, db.get_settings(conn), clock(), regenerate=True)


@app.post("/api/plan/more")
def more_plan(as_of: Optional[str] = None, n: int = Query(1, ge=1, le=20)):
    _no_preview(as_of)
    with db.connect() as conn:
        return scheduler.get_plan(conn, db.get_settings(conn), clock(), extra=n)


# ---------- frontend ----------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    # Version the stylesheet and script URLs, so browsers holding copies from an older release fetch the new ones.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={__version__}"')
    return HTMLResponse(html)
