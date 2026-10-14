"""Decides what is due and builds the fixed-size daily/weekly plan.

Every active task gets an urgency score:
  < 0.85  not due yet
  ~1.0    due now (or never logged)
  > 1.0   overdue; seasonal tasks climb toward 2.0 as their window closes
"""
import calendar
from datetime import date, timedelta

DUE_THRESHOLD = 0.85
PRIORITY_WEIGHT = {1: 1.3, 2: 1.0, 3: 0.8}


def parse_months(months: str | None) -> set[int]:
    if not months:
        return set()
    return {int(m) for m in months.replace(" ", "").split(",") if m.isdigit() and 1 <= int(m) <= 12}


def _season_window(months: set[int], today: date) -> tuple[date, date]:
    """Start and end of the contiguous run of months containing today (handles Nov–Feb wraps)."""
    start_y, start_m = today.year, today.month
    for _ in range(11):
        prev_m = 12 if start_m == 1 else start_m - 1
        if prev_m not in months:
            break
        start_y, start_m = (start_y - 1, 12) if start_m == 1 else (start_y, prev_m)
    end_y, end_m = today.year, today.month
    for _ in range(11):
        next_m = 1 if end_m == 12 else end_m + 1
        if next_m not in months:
            break
        end_y, end_m = (end_y + 1, 1) if end_m == 12 else (end_y, next_m)
    end = date(end_y, end_m, calendar.monthrange(end_y, end_m)[1])
    return date(start_y, start_m, 1), end


def evaluate(task: dict, last: dict | None, asset: dict | None, today: date) -> dict:
    """Return {score, reason, due_on} for one task. `last` is its most recent completion row."""
    if task.get("snoozed_until") and task["snoozed_until"] > today.isoformat():
        return {"score": 0.0, "reason": f"Snoozed until {task['snoozed_until']}", "due_on": task["snoozed_until"]}

    last_on = date.fromisoformat(last["done_on"]) if last else None
    kind = task["recurrence"]

    if kind == "seasonal":
        months = parse_months(task.get("months"))
        if today.month not in months:
            nxt = next((today.month + i - 1) % 12 + 1 for i in range(1, 13) if (today.month + i - 1) % 12 + 1 in months) if months else None
            return {"score": 0.0, "reason": f"Out of season (next: {calendar.month_abbr[nxt]})" if nxt else "No months set", "due_on": None}
        start, end = _season_window(months, today)
        if last_on and last_on >= start:
            return {"score": 0.0, "reason": f"Done this season ({last_on})", "due_on": None}
        frac = (today - start).days / max((end - start).days, 1)
        return {"score": 1.0 + frac, "reason": f"Seasonal — finish by {end:%b %-d}", "due_on": end.isoformat()}

    if last_on is None:
        return {"score": 1.0, "reason": "Never logged", "due_on": today.isoformat()}

    scores, reasons, due_dates = [], [], []
    if task.get("interval_days"):
        elapsed = (today - last_on).days
        scores.append(elapsed / task["interval_days"])
        due = last_on + timedelta(days=task["interval_days"])
        due_dates.append(due)
        reasons.append(f"{elapsed}d since last (every {task['interval_days']}d)")

    if kind == "mileage" and task.get("mileage_interval") and asset and asset.get("mileage") and last and last.get("mileage"):
        driven = asset["mileage"] - last["mileage"]
        scores.append(driven / task["mileage_interval"])
        reasons.append(f"{driven:,} mi since last (every {task['mileage_interval']:,})")

    if not scores:
        return {"score": 1.0, "reason": "Needs a mileage reading", "due_on": None}

    i = max(range(len(scores)), key=lambda j: scores[j])
    return {
        "score": scores[i],
        "reason": reasons[i],
        "due_on": min(due_dates).isoformat() if due_dates else None,
    }


def period_for(mode: str, today: date) -> tuple[str, date, date]:
    if mode == "daily":
        return f"D{today.isoformat()}", today, today
    monday = today - timedelta(days=today.weekday())
    return f"W{monday.isoformat()}", monday, monday + timedelta(days=6)


def all_task_status(conn, today: date) -> list[dict]:
    """Every active task with its score, latest completion and asset."""
    rows = conn.execute(
        """
        SELECT t.*, a.name AS asset_name, a.category AS asset_category, a.mileage AS asset_mileage,
               c.done_on AS last_done_on, c.mileage AS last_mileage
        FROM tasks t
        LEFT JOIN assets a ON a.id = t.asset_id
        LEFT JOIN completions c ON c.id = (
            SELECT id FROM completions WHERE task_id = t.id ORDER BY done_on DESC, id DESC LIMIT 1)
        WHERE t.status = 'active'
        """
    ).fetchall()
    out = []
    for r in rows:
        t = dict(r)
        last = {"done_on": t["last_done_on"], "mileage": t["last_mileage"]} if t["last_done_on"] else None
        asset = {"mileage": t["asset_mileage"]} if t["asset_id"] else None
        ev = evaluate(t, last, asset, today)
        t.update(ev)
        t["rank"] = ev["score"] * PRIORITY_WEIGHT.get(t["priority"] or 2, 1.0)
        out.append(t)
    out.sort(key=lambda t: (-t["rank"], t["priority"] or 2, t["id"]))
    return out


def _fill(conn, period: str, start: date, end: date, statuses: list[dict], count: int):
    # Only active tasks count toward the plan size, so pausing one frees its slot.
    active_ids = {t["id"] for t in statuses}
    planned = {r["task_id"] for r in conn.execute("SELECT task_id FROM plan_items WHERE period = ?", (period,))} & active_ids
    done_in_period = {
        r["task_id"]
        for r in conn.execute(
            "SELECT task_id FROM completions WHERE done_on BETWEEN ? AND ?", (start.isoformat(), end.isoformat())
        )
    }
    for t in statuses:
        if len(planned) >= count:
            break
        if t["score"] < DUE_THRESHOLD or t["id"] in planned or t["id"] in done_in_period:
            continue
        conn.execute("INSERT INTO plan_items (period, task_id) VALUES (?, ?)", (period, t["id"]))
        planned.add(t["id"])


def get_plan(conn, settings: dict, today: date, regenerate: bool = False, extra: int = 0) -> dict:
    mode = settings.get("plan_mode", "weekly")
    count = int(settings.get("plan_count") or 5)
    period, start, end = period_for(mode, today)
    statuses = all_task_status(conn, today)

    if regenerate:
        # Keep anything already checked off; replace the rest.
        conn.execute(
            """DELETE FROM plan_items WHERE period = ? AND task_id NOT IN
               (SELECT task_id FROM completions WHERE done_on BETWEEN ? AND ?)""",
            (period, start.isoformat(), end.isoformat()),
        )
    # Top up to the plan size; this also refills slots freed by snoozing or pausing.
    _fill(conn, period, start, end, statuses, count)
    if extra:
        active_ids = {t["id"] for t in statuses}
        current = sum(1 for r in conn.execute("SELECT task_id FROM plan_items WHERE period = ?", (period,)) if r["task_id"] in active_ids)
        _fill(conn, period, start, end, statuses, current + extra)

    by_id = {t["id"]: t for t in statuses}
    items = []
    for r in conn.execute("SELECT task_id FROM plan_items WHERE period = ? ORDER BY rowid", (period,)):
        task = by_id.get(r["task_id"])
        if not task:  # paused or deleted since planning
            continue
        done = conn.execute(
            "SELECT id, done_on FROM completions WHERE task_id = ? AND done_on BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
            (task["id"], start.isoformat(), end.isoformat()),
        ).fetchone()
        items.append({**task, "done": bool(done), "completion_id": done["id"] if done else None})

    planned_ids = {i["id"] for i in items}
    backlog = [t for t in statuses if t["score"] >= DUE_THRESHOLD and t["id"] not in planned_ids]
    return {
        "period": period,
        "mode": mode,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": count,
        "items": items,
        "backlog_count": len(backlog),
        "up_next": backlog[:10],
    }
