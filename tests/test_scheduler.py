from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db, scheduler


def test_interval_scoring():
    t = {"recurrence": "interval", "interval_days": 90}
    assert scheduler.evaluate(t, None, None, date(2026, 10, 13))["score"] == 1.0
    ev = scheduler.evaluate(t, {"done_on": "2026-08-14", "mileage": None}, None, date(2026, 10, 13))
    assert ev["score"] == pytest.approx(60 / 90)
    assert ev["due_on"] == "2026-11-12"


def test_seasonal_window_and_wraparound():
    fall = {"recurrence": "seasonal", "months": "10,11"}
    assert scheduler.evaluate(fall, None, None, date(2026, 9, 1))["score"] == 0
    in_season = scheduler.evaluate(fall, None, None, date(2026, 10, 13))
    assert 1.0 < in_season["score"] < 1.3 and in_season["due_on"] == "2026-11-30"
    done = scheduler.evaluate(fall, {"done_on": "2026-10-02", "mileage": None}, None, date(2026, 10, 13))
    assert done["score"] == 0
    # Last year's completion doesn't count for this season.
    assert scheduler.evaluate(fall, {"done_on": "2025-11-02", "mileage": None}, None, date(2026, 10, 13))["score"] > 1

    winter = {"recurrence": "seasonal", "months": "12,1,2"}
    ev = scheduler.evaluate(winter, {"done_on": "2026-12-20", "mileage": None}, None, date(2027, 1, 10))
    assert ev["score"] == 0  # done in Dec counts for the Dec–Feb window
    assert scheduler.evaluate(winter, None, None, date(2027, 1, 10))["due_on"] == "2027-02-28"


def test_mileage_uses_whichever_comes_first():
    t = {"recurrence": "mileage", "mileage_interval": 5000, "interval_days": 180}
    last = {"done_on": "2026-09-13", "mileage": 40000}
    ev = scheduler.evaluate(t, last, {"mileage": 45500}, date(2026, 10, 13))
    assert ev["score"] == pytest.approx(1.1) and "mi" in ev["reason"]
    ev = scheduler.evaluate(t, {"done_on": "2026-01-01", "mileage": 40000}, {"mileage": 41000}, date(2026, 10, 13))
    assert ev["score"] > 1.5 and "since last" in ev["reason"]


def test_snooze():
    t = {"recurrence": "interval", "interval_days": 7, "snoozed_until": "2026-10-20"}
    assert scheduler.evaluate(t, None, None, date(2026, 10, 13))["score"] == 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    from app import main
    db.init()
    return TestClient(main.app)


def set_today(monkeypatch, d: str):
    from app import main
    monkeypatch.setattr(main, "clock", lambda: date.fromisoformat(d))


def test_plan_end_to_end(client, monkeypatch):
    set_today(monkeypatch, "2026-10-13")
    client.put("/api/settings", json={"plan_mode": "weekly", "plan_count": "2"})
    car = client.post("/api/assets", json={"category": "vehicle", "name": "CR-V", "year": 2019, "mileage": 45000}).json()
    ids = []
    for title, extra in [
        ("Oil change", {"recurrence": "mileage", "mileage_interval": 5000, "interval_days": 180, "asset_id": car["id"], "priority": 1}),
        ("Clean gutters", {"recurrence": "seasonal", "months": "10,11"}),
        ("Test smoke alarms", {"recurrence": "interval", "interval_days": 30, "priority": 3}),
        ("Plant tulips", {"recurrence": "seasonal", "months": "4"}),
    ]:
        ids.append(client.post("/api/tasks", json={"title": title, **extra}).json()["id"])

    plan = client.get("/api/plan").json()
    assert plan["period"] == "W2026-10-12"
    titles = [i["title"] for i in plan["items"]]
    assert set(titles) == {"Clean gutters", "Oil change"}  # in-season + high priority beat low priority
    assert plan["backlog_count"] == 1  # smoke alarms; tulips are out of season

    # Same plan on another day this week.
    set_today(monkeypatch, "2026-10-15")
    assert [i["id"] for i in client.get("/api/plan").json()["items"]] == [i["id"] for i in plan["items"]]

    # Log the oil change with an odometer reading; it stays in the plan as done and updates the car.
    set_today(monkeypatch, "2026-10-14")
    client.post("/api/completions", json={"task_id": ids[0], "mileage": 45200})
    plan = client.get("/api/plan").json()
    assert {i["title"]: i["done"] for i in plan["items"]} == {"Oil change": True, "Clean gutters": False}
    assert client.get("/api/assets").json()[0]["mileage"] == 45200

    # Snoozing gutters frees the slot, which the smoke alarm test takes.
    client.post(f"/api/tasks/{ids[1]}/snooze?days=7")
    titles = [i["title"] for i in client.get("/api/plan").json()["items"]]
    assert set(titles) == {"Oil change", "Test smoke alarms"}

    # Previewing next week shows a fresh plan without the oil change, and saves nothing.
    nxt = client.get("/api/plan?as_of=2026-10-21").json()
    assert nxt["period"] == "W2026-10-19" and "Oil change" not in [i["title"] for i in nxt["items"]]
    from app import db as _db
    with _db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM plan_items WHERE period = 'W2026-10-19'").fetchone()[0] == 0
    assert client.post("/api/plan/regenerate?as_of=2026-10-21").status_code == 400


def test_suggested_tasks_wait_for_approval(client):
    t = client.post("/api/tasks", json={"title": "Flush water heater", "recurrence": "interval", "interval_days": 365, "status": "suggested"}).json()
    assert client.get("/api/plan?as_of=2026-10-13").json()["items"] == []
    client.post(f"/api/tasks/{t['id']}/approve?as_of=2026-10-13", json={"last_done_on": "2026-03-01"})
    status = [x for x in client.get("/api/tasks?as_of=2026-10-13").json() if x["id"] == t["id"]][0]
    assert status["status"] == "active" and status["score"] < 0.85


def test_bad_task_rejected(client):
    r = client.post("/api/tasks", json={"title": "x", "recurrence": "seasonal", "months": ""})
    assert r.status_code == 400


def test_one_off_deadlines():
    t = {"recurrence": "once", "due_date": "2026-10-20"}
    at = lambda d, last=None: scheduler.evaluate(t, last, None, date.fromisoformat(d))
    assert at("2026-10-01")["score"] < 0.85                       # weeks away: not due yet
    assert at("2026-10-13") == {"score": 0.85, "reason": "Due in 7 days", "due_on": "2026-10-20"}  # a week ahead: due
    assert at("2026-10-20")["reason"] == "Due today" and at("2026-10-20")["score"] == 1.0
    assert at("2026-10-27")["score"] == 1.5 and at("2026-10-27")["reason"] == "7 days past its deadline"
    assert at("2027-01-01")["score"] == 2.0                        # lateness is capped
    assert at("2026-10-25", {"done_on": "2026-10-19"})["score"] == 0  # done once, done for good
    assert scheduler.evaluate({"recurrence": "once"}, None, None, date(2026, 10, 13))["reason"] == "No deadline"


def test_one_off_tasks_join_a_full_plan_and_leave_when_done(client, monkeypatch):
    set_today(monkeypatch, "2026-10-13")
    client.put("/api/settings", json={"plan_mode": "weekly", "plan_count": "1"})
    client.post("/api/tasks", json={"title": "Test smoke alarms", "recurrence": "interval", "interval_days": 30})
    assert len(client.get("/api/plan").json()["items"]) == 1  # the plan is full

    fence = client.post("/api/tasks", json={"title": "Fix fence gate", "recurrence": "once", "due_date": "2026-10-16",
                                            "interval_days": 30}).json()
    assert fence["planned"] and fence["interval_days"] is None
    later = client.post("/api/tasks", json={"title": "Paint shed", "recurrence": "once", "due_date": "2026-12-01"}).json()
    assert not later["planned"]
    plan = client.get("/api/plan").json()
    assert [i["title"] for i in plan["items"]] == ["Test smoke alarms", "Fix fence gate"]

    client.post("/api/completions", json={"task_id": fence["id"]})
    assert {i["title"]: i["done"] for i in client.get("/api/plan").json()["items"]}["Fix fence gate"] is True
    set_today(monkeypatch, "2026-10-20")
    assert "Fix fence gate" not in [i["title"] for i in client.get("/api/plan").json()["items"]]
    # Deadlines only apply to one-off tasks.
    r = client.post("/api/tasks", json={"title": "x", "recurrence": "interval", "interval_days": 7, "due_date": "2026-11-01"}).json()
    assert r["due_date"] is None
