"""Regression tests for problems found in the 0.1.0 security and correctness review."""
import base64
import http.server
import socket
import sqlite3
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import ai, auth, db, documents, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(main, "clock", lambda: date(2026, 10, 13))
    db.init()
    return TestClient(main.app)


def task(client, **fields):
    body = {"title": "Replace filter", "recurrence": "interval", "interval_days": 90, **fields}
    r = client.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- requests from other sites ----------

def test_unknown_host_names_are_refused_without_a_password(client, monkeypatch):
    assert client.get("/api/settings", headers={"Host": "attacker.example"}).status_code == 400
    for host in ["localhost:8000", "127.0.0.1:8000", "192.168.1.20", "[::1]:8000", "nas.local", "nas"]:
        assert client.get("/api/settings", headers={"Host": host}).status_code == 200, host
    monkeypatch.setattr(main, "ALLOWED_HOSTS", ["house.example.com"])
    assert client.get("/api/settings", headers={"Host": "house.example.com"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 200


def test_cross_site_writes_are_refused(client):
    t = task(client)
    url = f"/api/tasks/{t['id']}/status/paused"
    assert client.post(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(url, headers={"Origin": "null"}).status_code == 403
    assert client.post(url, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.post(url, headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/tasks", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # reads can't be seen cross-site


def test_oversized_uploads_are_refused_before_reading(client):
    r = client.post("/api/documents/upload", headers={"Content-Length": str(200 * 1024 * 1024)}, content=b"x")
    assert r.status_code == 413


@pytest.fixture
def signed_out(client, monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "hunter2")
    monkeypatch.setattr(main, "AUTH_FAILURE_DELAY", 0)
    monkeypatch.setattr(main, "_failures", [])
    auth._keys.clear()
    return client


def sign_in(client, remember=True, next="/"):
    data = {"username": "admin", "password": "hunter2", "next": next, **({"remember": "true"} if remember else {})}
    return client.post("/login", data=data, follow_redirects=False)


def cookies(r):
    return r.headers.get_list("set-cookie")


def use(client, token):
    """Send exactly this session cookie (the test client otherwise keeps stale copies a browser would replace)."""
    client.cookies.clear()
    client.cookies.set("hk_session", token)


def test_scripts_can_use_basic_auth(signed_out):
    client = signed_out
    good = "Basic " + base64.b64encode(b"admin:hunter2").decode()
    bad = "Basic " + base64.b64encode(b"admin:wrong").decode()
    assert client.get("/api/settings", headers={"Authorization": good}).status_code == 200
    assert client.get("/api/settings", headers={"Authorization": bad}).status_code == 401
    assert client.get("/api/health").status_code == 200
    # Scripts that wait to be asked (wget, urllib) get the challenge; the app's own fetches must not.
    assert client.get("/api/settings").headers["www-authenticate"].startswith("Basic")
    assert "www-authenticate" not in client.get("/api/settings", headers={"Sec-Fetch-Site": "same-origin"}).headers
    # With a password, any host name works (behind a reverse proxy, for example).
    assert client.get("/api/settings", headers={"Authorization": good, "Host": "house.example.com"}).status_code == 200


def test_sign_in_page_and_remembered_session(signed_out):
    client = signed_out
    r = client.get("/?as_of=2026-11-20", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/%3Fas_of%3D2026-11-20"  # keeps the query
    page = client.get("/login?next=/docs")
    assert 'autocomplete="current-password"' in page.text and 'name="next" value="/docs"' in page.text
    assert 'id="login-error" hidden' in page.text
    assert client.head("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200  # the sign-in page needs it

    r = client.post("/login", data={"username": "admin", "password": "wrong", "next": "/#plan"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login?error=1") and not cookies(r)
    error_page = client.get(r.headers["location"])
    assert 'id="login-error" role="alert">' in error_page.text and 'aria-invalid="true"' in error_page.text
    assert 'value="/#plan"' in error_page.text  # the way back survives a wrong password

    r = sign_in(client, next="/#plan")
    assert r.status_code == 303 and r.headers["location"] == "/#plan"
    cookie = cookies(r)[0]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and f"Max-Age={auth.REMEMBER_SECONDS}" in cookie
    assert client.get("/api/settings").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303  # already signed in


def test_short_session_ends_with_the_browser(signed_out):
    r = sign_in(signed_out, remember=False)
    assert "Max-Age" not in cookies(r)[0]
    first, issued, expires = auth.check(r.cookies["hk_session"], "admin", "hunter2")
    assert expires - issued == auth.SHORT_SECONDS


def test_sign_out_works_for_an_older_session(signed_out):
    """Renewal used to re-add a fresh cookie after the sign-out deleted it."""
    client = signed_out
    use(client, auth.issue("admin", "hunter2", auth.REMEMBER_SECONDS, now=time.time() - 3 * 86400))
    r = client.post("/logout", follow_redirects=False)
    # The browser applies Set-Cookie headers in order, so the only one must be the deletion.
    assert r.headers["location"] == "/login" and len(cookies(r)) == 1 and 'hk_session=""' in cookies(r)[0]


def test_sign_out_when_already_signed_out_goes_to_sign_in(signed_out):
    r = signed_out.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_sign_out_everywhere(signed_out):
    client = signed_out
    other_device = auth.issue("admin", "hunter2", auth.REMEMBER_SECONDS)
    sign_in(client)
    client.post("/logout", data={"everywhere": "true"})
    use(client, other_device)
    assert client.get("/api/settings").status_code == 401
    # Without a valid session, "everywhere" signs nobody else out.
    still_good = auth.issue("admin", "hunter2", 3600)
    client.cookies.clear()
    client.post("/logout", data={"everywhere": "true"})
    use(client, still_good)
    assert client.get("/api/settings").status_code == 200


def test_bad_sessions_are_refused(signed_out, monkeypatch):
    client = signed_out
    now = time.time()
    good = auth.issue("admin", "hunter2", 3600)
    bad = [
        auth.issue("admin", "hunter2", 60, now=now - 120),                                     # expired
        auth.issue("admin", "hunter2", 3600, first=int(now - auth.MAX_AGE - 60)),              # older than a year
        good[:-1] + ("0" if good[-1] != "0" else "1"),                                         # tampered
        "garbage", "+1.2.3." + "0" * 64, " 1.2.3." + "0" * 64,
    ]
    for token in bad:
        use(client, token)
        assert client.get("/api/settings").status_code == 401, token
    use(client, good)
    assert client.get("/api/settings").status_code == 200
    monkeypatch.setattr(main, "PASSWORD", "new password")  # changing the password signs everyone out
    assert client.get("/api/settings").status_code == 401


def test_non_ascii_cookie_is_refused_not_a_crash(signed_out):
    for path in ["/api/settings", "/", "/login"]:
        r = signed_out.get(path, headers={"Cookie": "hk_session=1.2.3.\xe9".encode("latin-1")}, follow_redirects=False)
        assert r.status_code in (200, 303, 401), path


def test_sessions_are_renewed_with_use_but_not_forever(signed_out):
    client = signed_out
    first = int(time.time() - 200 * 86400)
    use(client, auth.issue("admin", "hunter2", auth.REMEMBER_SECONDS, first=first, now=time.time() - 2 * 86400))
    r = client.get("/api/settings")
    assert r.status_code == 200 and f"Max-Age={auth.REMEMBER_SECONDS}" in cookies(r)[0]
    assert auth.check(r.cookies["hk_session"], "admin", "hunter2")[0] == first  # the sign-in date carries over
    use(client, auth.issue("admin", "hunter2", auth.REMEMBER_SECONDS))
    assert not cookies(client.get("/api/settings"))  # fresh: nothing to renew
    # Short sessions slide too (hourly), and stay browser-session cookies.
    use(client, auth.issue("admin", "hunter2", auth.SHORT_SECONDS, now=time.time() - 2 * 3600))
    r = client.get("/api/settings")
    assert cookies(r) and "Max-Age" not in cookies(r)[0]


def test_guessing_is_capped_and_a_right_guess_looks_the_same(signed_out, monkeypatch):
    client = signed_out
    for _ in range(main.FAILURE_LIMIT):
        client.post("/login", data={"username": "admin", "password": "guess"}, follow_redirects=False)
    r = sign_in(client)
    assert r.status_code == 429 and not cookies(r)  # even the right password is refused for now
    good = "Basic " + base64.b64encode(b"admin:hunter2").decode()
    assert client.get("/api/settings", headers={"Authorization": good}).status_code == 429
    monkeypatch.setattr(main, "_failures", [])  # a minute later
    assert sign_in(client).status_code == 303


def test_wrong_basic_auth_counts_toward_the_limit(signed_out):
    bad = "Basic " + base64.b64encode(b"admin:guess").decode()
    for _ in range(main.FAILURE_LIMIT):
        signed_out.get("/api/settings", headers={"Authorization": bad})
    assert sign_in(signed_out).status_code == 429


def test_opening_a_manual_after_signing_out_offers_sign_in(signed_out):
    r = signed_out.get("/api/documents/1/file", headers={"Sec-Fetch-Mode": "navigate"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/api/documents/1/file")


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)"])
def test_sign_in_only_returns_to_this_site(signed_out, target):
    assert sign_in(signed_out, next=target).headers["location"] == "/"


def test_session_key_is_private_survives_restarts_and_repairs_itself(client):
    token = auth.issue("admin", "pw", 3600)
    auth._keys.clear()  # as if the app restarted
    assert auth.check(token, "admin", "pw")
    key = db.DB_PATH.parent / "session.key"
    assert key.stat().st_mode & 0o077 == 0  # readable by the app's user only
    key.write_text("")  # damaged, e.g. a crash while writing
    auth._keys.clear()
    assert not auth.check(token, "admin", "pw") and len(bytes.fromhex(key.read_text())) == 32


def test_sign_in_works_when_the_data_folder_is_read_only(signed_out, tmp_path, monkeypatch):
    ro = tmp_path / "ro"
    ro.mkdir()
    monkeypatch.setattr(db, "DB_PATH", ro / "x.db")
    ro.chmod(0o555)
    try:
        auth._keys.clear()
        token = auth.issue("admin", "hunter2", 3600)
        assert auth.check(token, "admin", "hunter2")
    finally:
        ro.chmod(0o755)


def test_secure_cookies_can_be_forced(signed_out, monkeypatch):
    assert "Secure" not in cookies(sign_in(signed_out))[0]
    monkeypatch.setattr(main, "SECURE_COOKIES", True)
    assert "Secure" in cookies(sign_in(signed_out))[0]


def test_stored_files_cannot_run_as_pages(client):
    doc = client.post("/api/documents/upload", data={"read": "false"},
                      files={"file": ("notes.txt", b"Check the sump pump monthly.\n" * 20, "text/plain")}).json()["document"]
    r = client.get(f"/api/documents/{doc['id']}/file")
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"


# ---------- downloads ----------

def test_download_checks_the_address_it_actually_connects_to(monkeypatch):
    """DNS rebinding: the name looks public when checked, then resolves to this machine."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real, lookups = socket.getaddrinfo, []

    def rebinding(host, port, *args, **kwargs):
        if host != "rebind.example":
            return real(host, port, *args, **kwargs)
        lookups.append(host)
        ip = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    try:
        with pytest.raises(documents.DocumentError, match="private or local"):
            documents.download(f"http://rebind.example:{server.server_address[1]}/")
    finally:
        server.shutdown()


@pytest.mark.parametrize("address", ["64:ff9b::a9fe:a9fe", "2002:7f00:1::1", "::ffff:10.0.0.1", "169.254.169.254", "fe80::1%en0"])
def test_addresses_hiding_a_private_ipv4_are_not_public(address):
    assert not documents.is_public(address)


# ---------- dates and validation ----------

def test_bad_dates_are_rejected_instead_of_breaking_the_plan(client):
    t = task(client)
    assert client.post("/api/completions", json={"task_id": t["id"], "done_on": "10/12/2026"}).status_code == 422
    s = task(client, status="suggested", title="Flush water heater")
    assert client.post(f"/api/tasks/{s['id']}/approve", json={"last_done_on": "last spring"}).status_code == 422
    assert client.get("/api/plan").status_code == 200 and client.get("/api/tasks").status_code == 200
    assert client.get("/api/plan?as_of=not-a-date").status_code == 400


def test_task_fields_are_checked(client):
    t = task(client)
    base = {"title": "x", "recurrence": "interval", "interval_days": 30}
    for bad in [{"interval_days": -5}, {"status": "done"}, {"priority": 99}, {"recurrence": "weekly"},
                {"recurrence": "seasonal", "months": "13,a"}, {"title": ""}]:
        assert client.put(f"/api/tasks/{t['id']}", json={**base, **bad}).status_code in (400, 422), bad
    assert client.put("/api/tasks/999", json=base).status_code == 404
    assert client.put("/api/assets/999", json={"category": "appliance", "name": "x"}).status_code == 404
    assert client.post("/api/assets", json={"category": "spaceship", "name": "x"}).status_code == 422
    seasonal = task(client, recurrence="seasonal", months="11, 4,4,13")
    assert seasonal["months"] == "4,11"
    assert client.post(f"/api/tasks/{t['id']}/snooze?days=99999999").status_code == 422


def test_settings_are_checked(client):
    for bad in [{"plan_count": "five"}, {"plan_count": "0"}, {"plan_mode": "hourly"}, {"hemisphere": "east"}]:
        assert client.put("/api/settings", json=bad).status_code == 400, bad
    assert client.put("/api/settings", json={"plan_count": "3", "hemisphere": "south"}).json()["plan_count"] == "3"
    with db.connect() as conn:  # a bad value already in the database falls back to the default
        conn.execute("UPDATE settings SET value = 'five' WHERE key = 'plan_count'")
    assert client.get("/api/plan").json()["count"] == 5


def test_unworkable_suggestions_are_dropped_and_approval_is_checked(client, monkeypatch):
    asset = client.post("/api/assets", json={"category": "home", "name": "Furnace"}).json()

    def suggest(*a, **kw):
        mk = lambda **f: ai.SuggestedTask(**{"title": "t", "description": "", "recurrence": "interval", "interval_days": 90,
                                              "months": None, "mileage_interval": None, "priority": 2, "est_minutes": 10,
                                              "asset_name": None, **f})
        return [mk(title="good"), mk(title="no interval", interval_days=None), mk(title="bad months", recurrence="seasonal", months=[13])]
    monkeypatch.setattr(ai, "suggest_for_asset", suggest)
    assert client.post(f"/api/assets/{asset['id']}/suggest").json()["added"] == 1

    # A broken row (from an older version) can't be approved, and approving twice logs history once.
    with db.connect() as conn:
        conn.execute("INSERT INTO tasks (title, recurrence, status) VALUES ('broken', 'interval', 'suggested')")
    broken = [t for t in client.get("/api/tasks").json() if t["title"] == "broken"][0]
    assert client.post(f"/api/tasks/{broken['id']}/approve", json={}).status_code == 400
    good = [t for t in client.get("/api/tasks").json() if t["title"] == "good"][0]
    for _ in range(2):
        client.post(f"/api/tasks/{good['id']}/approve", json={"last_done_on": "2026-03-01"})
    assert len(client.get("/api/history").json()) == 1


# ---------- mileage ----------

def test_mileage_is_tracked_from_the_latest_reading(client):
    car = client.post("/api/assets", json={"category": "vehicle", "name": "Civic", "mileage": 40000}).json()
    oil = task(client, title="Oil change", recurrence="mileage", mileage_interval=5000, interval_days=365, asset_id=car["id"])
    client.post("/api/completions", json={"task_id": oil["id"], "done_on": "2026-01-01", "mileage": 40000})
    # Done again without a reading: counted from the car's odometer at the time.
    client.post("/api/completions", json={"task_id": oil["id"], "done_on": "2026-01-02"})
    client.put(f"/api/assets/{car['id']}", json={"category": "vehicle", "name": "Civic", "mileage": 50000})
    status = [t for t in client.get("/api/tasks").json() if t["id"] == oil["id"]][0]
    assert status["score"] == 2.0 and "10,000 mi" in status["reason"]


def test_mileage_task_without_readings_is_not_always_due(client):
    mower = client.post("/api/assets", json={"category": "outdoor", "name": "Mower"}).json()
    t = task(client, title="Sharpen blade", recurrence="mileage", mileage_interval=50, interval_days=None, asset_id=mower["id"])
    client.post("/api/completions", json={"task_id": t["id"], "done_on": "2026-10-01"})
    status = [x for x in client.get("/api/tasks").json() if x["id"] == t["id"]][0]
    assert status["score"] < 0.1


def test_undo_puts_the_odometer_back(client):
    car = client.post("/api/assets", json={"category": "vehicle", "name": "Civic", "mileage": 50000}).json()
    oil = task(client, title="Oil change", recurrence="mileage", mileage_interval=5000, interval_days=180, asset_id=car["id"])
    typo = client.post("/api/completions", json={"task_id": oil["id"], "mileage": 500000}).json()
    assert client.get("/api/assets").json()[0]["mileage"] == 500000
    client.delete(f"/api/completions/{typo['id']}")
    assert client.get("/api/assets").json()[0]["mileage"] == 50000
    assert client.delete("/api/completions/999").status_code == 404


# ---------- plan ----------

def test_snooze_frees_the_slot_even_when_done_recently(client):
    t = task(client, title="Wipe filter", interval_days=3)
    client.post("/api/completions", json={"task_id": t["id"], "done_on": "2026-10-09"})  # last Friday
    assert [i["id"] for i in client.get("/api/plan").json()["items"]] == [t["id"]]
    client.post(f"/api/tasks/{t['id']}/snooze?days=14")
    assert client.get("/api/plan").json()["items"] == []


# ---------- documents ----------

def test_uploaded_files_are_remembered_even_when_reading_fails(client, monkeypatch):
    def read(doc, path, asset, on_upload=None):
        on_upload("file_123")
        raise RuntimeError("Claude declined this request.")
    monkeypatch.setattr(ai, "read_document", read)
    forgotten = []
    monkeypatch.setattr(ai, "forget_file", forgotten.append)
    r = client.post("/api/documents/upload", files={"file": ("m.txt", b"Clean the coils yearly.\n" * 20, "text/plain")}).json()
    assert r["read_error"] and r["document"]["anthropic_file_id"] == "file_123"
    client.delete(f"/api/documents/{r['document']['id']}")
    assert forgotten == ["file_123"]


def test_unexpected_errors_while_reading_keep_the_document(client, monkeypatch):
    def read(*a, **kw):
        raise TypeError("unexpected keyword argument 'fallbacks'")
    monkeypatch.setattr(ai, "read_document", read)
    r = client.post("/api/documents/upload", files={"file": ("m.txt", b"Clean the coils yearly.\n" * 20, "text/plain")})
    assert r.status_code == 200 and "TypeError" in r.json()["read_error"]


def test_found_documents_are_not_saved_twice(client, monkeypatch):
    asset = client.post("/api/assets", json={"category": "appliance", "name": "Dishwasher"}).json()
    same = ai.FoundDocument(title="Manual", url="https://example.com/m.pdf", kind="manual", why="")
    monkeypatch.setattr(ai, "find_documents", lambda *a: [same, same])
    monkeypatch.setattr(documents, "download", lambda url: (b"Clean the filter monthly.\n" * 20, "text/plain", "m.txt"))
    monkeypatch.setattr(main, "_read_and_store", lambda doc_id: "skipped in test")
    r = client.post(f"/api/assets/{asset['id']}/find-documents").json()
    assert len(r["added"]) == 1 and r["skipped"][0]["reason"] == "Already saved"


def test_scan_size_limit_allows_for_base64(client):
    big = b"\xff\xd8\xff" + b"\x00" * 4_500_000
    r = client.post("/api/assets/scan", files={"file": ("p.jpg", big, "image/jpeg")})
    assert r.status_code == 400 and "3.75 MB" in r.json()["detail"]


# ---------- upgrades ----------

def test_databases_from_0_1_0_are_upgraded(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)  # the 0.1.0 schema, with user_version 0
    conn.execute("INSERT INTO tasks (title, recurrence, interval_days) VALUES ('Old task', 'interval', 30)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init()
    db.init()  # running again is harmless
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert "prev_asset_mileage" in [r[1] for r in conn.execute("PRAGMA table_info(completions)")]
        assert conn.execute("SELECT title FROM tasks").fetchone()[0] == "Old task"


def test_app_files_are_revalidated_so_updates_reach_the_browser(client):
    for path in ["/", "/static/style.css", "/static/app.js"]:
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_page_links_versioned_assets(client):
    from app import __version__
    html = client.get("/").text
    assert f'/static/style.css?v={__version__}"' in html and f'/static/app.js?v={__version__}"' in html
