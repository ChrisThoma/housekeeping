import base64

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "test.db")
    with TestClient(main.app) as c:  # runs startup, which creates the data folder and database
        yield c


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_startup_creates_database(client, tmp_path):
    assert (tmp_path / "data" / "test.db").exists()
    assert client.get("/api/settings").json()["plan_mode"] == "weekly"


def test_health_and_status(client, monkeypatch):
    assert client.get("/api/health").json() == {"ok": True}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    status = client.get("/api/status").json()
    assert status["version"] == main.__version__
    assert status["ai_configured"] is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert client.get("/api/status").json()["ai_configured"] is True


def test_frontend_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Housekeeping" in page.text
    for path in ("/static/app.js", "/static/style.css", "/static/fonts/fonts.css", "/static/favicon.svg", "/static/manifest.webmanifest"):
        assert client.get(path).status_code == 200, path


def test_password_protects_everything_but_health(client, monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "hunter2")
    monkeypatch.setattr(main, "USERNAME", "admin")
    assert client.get("/api/health").status_code == 200
    r = client.get("/api/plan")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get("/", headers=basic("admin", "wrong")).status_code == 401
    assert client.get("/api/plan", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert client.get("/api/plan", headers=basic("admin", "hunter2")).status_code == 200
