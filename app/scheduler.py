"""Decides what is due and builds the fixed-size daily/weekly plan.

Every active task gets an urgency score:
  < 0.85  not due yet
  ~1.0    due now (or never logged)
  > 1.0   overdue; seasonal tasks climb toward 2.0 as their window closes, one-off tasks as their deadline passes
"""
import calendar
from datetime import date, timedelta

DUE_THRESHOLD = 0.85
DEADLINE_LEAD_DAYS = 7  # a one-off task with a deadline comes due this many days before it
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

    if kind == "once":
        if last_on:
            return {"score": 0.0, "reason": f"Done {last_on}", "due_on": None}
        if not task.get("due_date"):
            return {"score": 1.0, "reason": "No deadline", "due_on": None}
        deadline = date.fromisoformat(task["due_date"])
        left = (deadline - today).days
        if left > 0:
            score = max(0.0, 1.0 - (1.0 - DUE_THRESHOLD) * left / DEADLINE_LEAD_DAYS)
            reason = "Due tomorrow" if left == 1 else f"Due in {left} days"
        else:
            score = 1.0 + min(-left, 14) / 14
            reason = "Due today" if left == 0 else f"{-left} day{'s' if left != -1 else ''} past its deadline"
        return {"score": score, "reason": reason, "due_on": deadline.isoformat()}

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

    if (kind == "mileage" and task.get("mileage_interval") and asset and asset.get("mileage") is not None
            and last and last.get("mileage") is not None):  # 0 is a real reading
        driven = asset["mileage"] - last["mileage"]
        scores.append(driven / task["mileage_interval"])
        reasons.append(f"{driven:,} mi since last (every {task['mileage_interval']:,})")

    if not scores:  # a mileage task with no time cap and no odometer readings: check on it yearly
        elapsed = (today - last_on).days
        return {"score": elapsed / 365, "reason": f"{elapsed}d since last; log the odometer to track miles",
                "due_on": (last_on + timedelta(days=365)).isoformat()}

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


# Completions of task t that count toward its schedule: done by `:today`, and on the item the task is on now.
# A task moved to another car starts fresh there, rather than carrying over the old car's dates and odometer.
ON_CURRENT_ITEM = "(c.asset_id IS NULL OR c.asset_id = t.asset_id)"
COUNTED_COMPLETIONS = f"c.task_id = t.id AND c.done_on <= :today AND {ON_CURRENT_ITEM}"
LAST_DONE_ON = f"(SELECT MAX(done_on) FROM completions c WHERE {COUNTED_COMPLETIONS})"


def all_task_status(conn, today: date) -> list[dict]:
    """Every active task with its score, latest completion and asset."""
    rows = conn.execute(
        f"""
        SELECT t.*, a.name AS asset_name, a.category AS asset_category, a.mileage AS asset_mileage,
               {LAST_DONE_ON} AS last_done_on,
               (SELECT mileage FROM completions c WHERE {COUNTED_COMPLETIONS} AND c.mileage IS NOT NULL
                ORDER BY done_on DESC, id DESC LIMIT 1) AS last_mileage
        FROM tasks t
        LEFT JOIN assets a ON a.id = t.asset_id
        WHERE t.status = 'active'
        """,
        {"today": today.isoformat()},
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


def _pick(statuses: list[dict], planned: list[int], done_in_period: set[int], count: int) -> list[int]:
    """The most urgent due tasks that would bring the plan up to `count`."""
    taken, picks = set(planned), []
    for t in statuses:
        if len(taken) >= count:
            break
        if t["score"] < DUE_THRESHOLD or t["id"] in taken or t["id"] in done_in_period:
            continue
        picks.append(t["id"])
        taken.add(t["id"])
    return picks


def add_if_due(conn, settings: dict, today: date, task_id: int) -> bool:
    """Put a task you just created into the current plan if it's already due, even when the plan is full."""
    period, start, end = period_for(settings.get("plan_mode", "weekly"), today)
    task = next((t for t in all_task_status(conn, today) if t["id"] == task_id), None)
    if not task or task["score"] < DUE_THRESHOLD:
        return False
    get_plan(conn, settings, today)  # make sure this period's plan exists first, so the task is an addition to it
    conn.execute("INSERT OR IGNORE INTO plan_items (period, task_id) VALUES (?, ?)", (period, task_id))
    return True


def plan_size(settings: dict) -> int:
    try:
        return max(1, int(settings.get("plan_count") or 5))
    except ValueError:
        return 5


def get_plan(conn, settings: dict, today: date, regenerate: bool = False, extra: int = 0, persist: bool = True) -> dict:
    """The plan for the period containing `today`. With persist=False (a preview), nothing is saved."""
    mode = settings.get("plan_mode", "weekly")
    count = plan_size(settings)
    period, start, end = period_for(mode, today)
    statuses = all_task_status(conn, today)
    # Completions count up to today: a preview of an earlier date mustn't see work done after it.
    span = (start.isoformat(), min(end, today).isoformat())
    # Like the scores, only work on the item each task is on now counts.
    on_item = f"JOIN tasks t ON t.id = c.task_id AND {ON_CURRENT_ITEM}"
    done_in_period = {r["task_id"] for r in conn.execute(
        f"SELECT c.task_id FROM completions c {on_item} WHERE c.done_on BETWEEN ? AND ?", span)}

    saved = [r["task_id"] for r in conn.execute("SELECT task_id FROM plan_items WHERE period = ? ORDER BY rowid", (period,))]
    if regenerate:
        # Keep anything already checked off; replace the rest.
        saved = [i for i in saved if i in done_in_period]
        if persist:
            conn.execute(f"DELETE FROM plan_items WHERE period = ? AND task_id NOT IN ({','.join('?' * len(saved)) or 'NULL'})",
                         (period, *saved))
    # Only active tasks count toward the plan size, so pausing or snoozing one frees its slot for the top-up.
    by_id = {t["id"]: t for t in statuses}
    planned = [i for i in saved if i in by_id]
    added = _pick(statuses, planned, done_in_period, count)
    planned += added
    if extra:
        more = _pick(statuses, planned, done_in_period, len(planned) + extra)
        planned += more
        added += more
    if persist:
        conn.executemany("INSERT OR IGNORE INTO plan_items (period, task_id) VALUES (?, ?)", [(period, i) for i in added])

    items = []
    for task_id in planned:
        task = by_id[task_id]
        done = conn.execute(
            f"SELECT c.id, c.done_on FROM completions c {on_item} WHERE c.task_id = ? AND c.done_on BETWEEN ? AND ? ORDER BY c.id DESC LIMIT 1",
            (task_id, *span),
        ).fetchone()
        items.append({**task, "done": bool(done), "completion_id": done["id"] if done else None})

    planned_ids = set(planned)
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
