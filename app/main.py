import base64
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, ai, db, documents, scheduler

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield


app = FastAPI(title="Housekeeping", version=__version__, lifespan=lifespan)

# Optional password, for when the app is reachable by others (a home server, a VPS).
PASSWORD = os.environ.get("HOUSEKEEPING_PASSWORD", "")
USERNAME = os.environ.get("HOUSEKEEPING_USERNAME", "admin")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not PASSWORD or request.url.path == "/api/health":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            user = pw = ""
        if secrets.compare_digest(user.encode(), USERNAME.encode()) and secrets.compare_digest(pw.encode(), PASSWORD.encode()):
            return await call_next(request)
    return Response("Sign in to use Housekeeping.", 401, {"WWW-Authenticate": 'Basic realm="Housekeeping"'})


def today(override: Optional[str] = None) -> date:
    return date.fromisoformat(override) if override else date.today()


# ---------- models ----------

class AssetIn(BaseModel):
    category: str
    name: str
    details: str = ""
    year: Optional[int] = None
    mileage: Optional[int] = None
    notes: str = ""


class TaskIn(BaseModel):
    asset_id: Optional[int] = None
    title: str
    description: str = ""
    recurrence: str = "interval"
    interval_days: Optional[int] = None
    months: Optional[str] = None
    mileage_interval: Optional[int] = None
    priority: int = 2
    est_minutes: Optional[int] = None
    status: str = "active"
    source: str = "manual"


class CompletionIn(BaseModel):
    task_id: int
    done_on: Optional[str] = None
    mileage: Optional[int] = None
    notes: str = ""


class ApproveIn(BaseModel):
    last_done_on: Optional[str] = None  # when you last did it, if you remember


class GuideIn(BaseModel):
    text: str


TASK_FIELDS = ["asset_id", "title", "description", "recurrence", "interval_days", "months",
               "mileage_interval", "priority", "est_minutes", "status", "source"]


def _validate_task(t: TaskIn):
    if t.recurrence not in ("interval", "seasonal", "mileage"):
        raise HTTPException(400, "recurrence must be interval, seasonal or mileage")
    if t.recurrence == "interval" and not t.interval_days:
        raise HTTPException(400, "Interval tasks need interval_days")
    if t.recurrence == "seasonal" and not scheduler.parse_months(t.months):
        raise HTTPException(400, "Seasonal tasks need at least one month")
    if t.recurrence == "mileage" and not t.mileage_interval:
        raise HTTPException(400, "Mileage tasks need mileage_interval")


def _insert_suggestions(conn, suggestions, asset_id_for, source: str) -> int:
    n = 0
    for s in suggestions:
        conn.execute(
            """INSERT INTO tasks (asset_id, title, description, recurrence, interval_days, months,
                                  mileage_interval, priority, est_minutes, status, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?)""",
            (asset_id_for(s), s.title, s.description, s.recurrence, s.interval_days,
             ",".join(str(m) for m in s.months) if s.months else None,
             s.mileage_interval, s.priority, s.est_minutes, source),
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


@app.put("/api/settings")
def write_settings(values: dict[str, str]):
    allowed = set(db.DEFAULT_SETTINGS)
    with db.connect() as conn:
        for k, v in values.items():
            if k in allowed:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
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
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(400, "That photo is larger than 5 MB. Try a smaller one.")
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


def _read_and_store(doc_id: int) -> Optional[str]:
    """Have Claude read a stored document and save its notes. Returns an error message instead of raising,
    so a document you just added is kept even when reading fails."""
    with db.connect() as conn:
        doc = _get_doc(conn, doc_id)
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (doc["asset_id"],)).fetchone() if doc["asset_id"] else None
    try:
        notes, mode, file_id = _ai_errors(lambda: ai.read_document(doc, documents.path_for(doc), dict(asset) if asset else None))
    except HTTPException as e:
        return e.detail
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


@app.post("/api/assets/{asset_id}/find-documents")
def find_documents(asset_id: int):
    """Claude searches the web for this item's manuals; the app downloads and reads each one it finds."""
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
    return FileResponse(path, media_type=doc["mime"], content_disposition_type="inline", filename=doc["filename"])


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
            [getattr(t, f) for f in TASK_FIELDS],
        )
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, t: TaskIn):
    _validate_task(t)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE tasks SET {', '.join(f + ' = ?' for f in TASK_FIELDS)} WHERE id = ?",
            [getattr(t, f) for f in TASK_FIELDS] + [task_id],
        )
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


@app.post("/api/tasks/{task_id}/approve")
def approve_task(task_id: int, body: ApproveIn):
    with db.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'active' WHERE id = ?", (task_id,))
        if body.last_done_on:
            conn.execute("INSERT INTO completions (task_id, done_on, notes) VALUES (?, ?, 'Initial history')",
                         (task_id, body.last_done_on))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/status/{status}")
def set_task_status(task_id: int, status: str):
    if status not in ("active", "paused"):
        raise HTTPException(400, "status must be active or paused")
    with db.connect() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/snooze")
def snooze_task(task_id: int, days: int = 7, as_of: Optional[str] = None):
    until = today(as_of) + timedelta(days=days)
    with db.connect() as conn:
        conn.execute("UPDATE tasks SET snoozed_until = ? WHERE id = ?", (until.isoformat(), task_id))
        conn.execute(
            """DELETE FROM plan_items WHERE task_id = ? AND task_id NOT IN
               (SELECT task_id FROM completions WHERE done_on >= ?)""",
            (task_id, (today(as_of) - timedelta(days=7)).isoformat()),
        )
    return {"snoozed_until": until.isoformat()}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}


# ---------- completions ----------

@app.post("/api/completions")
def complete(c: CompletionIn, as_of: Optional[str] = None):
    done_on = c.done_on or today(as_of).isoformat()
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (c.task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        cur = conn.execute("INSERT INTO completions (task_id, done_on, mileage, notes) VALUES (?, ?, ?, ?)",
                           (c.task_id, done_on, c.mileage, c.notes))
        conn.execute("UPDATE tasks SET snoozed_until = NULL WHERE id = ?", (c.task_id,))
        # A mileage reading on a vehicle task also updates the vehicle's odometer.
        if c.mileage and task["asset_id"]:
            conn.execute("UPDATE assets SET mileage = MAX(COALESCE(mileage, 0), ?) WHERE id = ? AND category = 'vehicle'",
                         (c.mileage, task["asset_id"]))
        return {"id": cur.lastrowid}


@app.delete("/api/completions/{completion_id}")
def undo_completion(completion_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM completions WHERE id = ?", (completion_id,))
    return {"ok": True}


@app.get("/api/history")
def history(limit: int = 200):
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
    with db.connect() as conn:
        return scheduler.get_plan(conn, db.get_settings(conn), today(as_of))


@app.post("/api/plan/regenerate")
def regenerate_plan(as_of: Optional[str] = None):
    with db.connect() as conn:
        return scheduler.get_plan(conn, db.get_settings(conn), today(as_of), regenerate=True)


@app.post("/api/plan/more")
def more_plan(as_of: Optional[str] = None, n: int = 1):
    with db.connect() as conn:
        return scheduler.get_plan(conn, db.get_settings(conn), today(as_of), extra=n)


# ---------- frontend ----------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
