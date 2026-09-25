"""Area-coordinator managed recurring one-on-one appointments."""

from calendar import monthcalendar, monthrange
from datetime import date, datetime, timedelta
from math import gcd

from flask import abort, flash, redirect, request, url_for

from core import app, audit, clean_single_line, current_user, db, require_csrf
from staff_event_schedule import (
    SCHOOL_TIMEZONE,
    next_occurrence,
    parse_event_start,
    parse_repeat_weeks,
)


def _calendar_month(raw):
    try:
        return datetime.strptime(str(raw or ""), "%Y-%m").date().replace(day=1)
    except ValueError:
        return datetime.now(SCHOOL_TIMEZONE).date().replace(day=1)


def _next_month(value):
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _series_occurrences(row, start_date, end_date):
    """Return stored or recurring occurrences in the half-open date range."""
    anchor = datetime.fromisoformat(row["scheduled_at"])
    repeat_weeks = row["repeat_weeks"]
    recurrence_kind = row["recurrence_kind"] if "recurrence_kind" in row.keys() else "weekly"
    if recurrence_kind == "monthly":
        occurrences = []
        cursor = anchor.date().replace(day=1)
        ordinal = (anchor.day - 1) // 7 + 1
        while cursor < end_date:
            offset = (anchor.weekday() - cursor.weekday()) % 7
            day = 1 + offset + (ordinal - 1) * 7
            if day <= monthrange(cursor.year, cursor.month)[1]:
                occurrence = anchor.replace(year=cursor.year, month=cursor.month, day=day)
                if occurrence >= anchor and start_date <= occurrence.date() < end_date:
                    occurrences.append(occurrence)
            cursor = _next_month(cursor)
        return occurrences
    if not repeat_weeks:
        return [anchor] if start_date <= anchor.date() < end_date else []
    step = timedelta(weeks=repeat_weeks)
    current = anchor
    if current.date() < start_date:
        missed = max(0, (start_date - current.date()).days // step.days)
        current += step * missed
        while current.date() < start_date:
            current += step
    occurrences = []
    while current.date() < end_date:
        occurrences.append(current)
        current += step
    return occurrences


def _series_overlap(first_at, first_repeat, second_at, second_repeat,
                    first_kind="weekly", second_kind="weekly"):
    """Return whether two forward-only recurrence series share an occurrence."""
    if "monthly" in (first_kind, second_kind):
        start = min(datetime.fromisoformat(first_at), datetime.fromisoformat(second_at)).date()
        end = date(start.year + 10, start.month, 1)
        first_row = {"scheduled_at": first_at, "repeat_weeks": first_repeat,
                     "recurrence_kind": first_kind}
        second_row = {"scheduled_at": second_at, "repeat_weeks": second_repeat,
                      "recurrence_kind": second_kind}
        return bool(set(_series_occurrences(first_row, start, end)) &
                    set(_series_occurrences(second_row, start, end)))
    first = datetime.fromisoformat(first_at)
    second = datetime.fromisoformat(second_at)
    if not first_repeat and not second_repeat:
        return first == second
    if not first_repeat:
        delta = first - second
        return delta >= timedelta(0) and delta % timedelta(weeks=second_repeat) == timedelta(0)
    if not second_repeat:
        delta = second - first
        return delta >= timedelta(0) and delta % timedelta(weeks=first_repeat) == timedelta(0)
    period = timedelta(weeks=gcd(first_repeat, second_repeat))
    return (first - second) % period == timedelta(0)


def _has_schedule_conflict(conn, actor_id, recipient_id, scheduled_at, repeat_weeks,
                           recurrence_kind="weekly"):
    rows = conn.execute(
        "SELECT scheduled_at,repeat_weeks,recurrence_kind FROM one_on_one_appointments "
        "WHERE ra_user_id=? OR scheduled_by=?",
        (recipient_id, actor_id),
    ).fetchall()
    return any(
        _series_overlap(scheduled_at, repeat_weeks, row["scheduled_at"], row["repeat_weeks"],
                        recurrence_kind, row["recurrence_kind"])
        for row in rows
    )


def _recipient_next_appointment(user_id):
    now = datetime.now(SCHOOL_TIMEZONE).replace(tzinfo=None)
    candidates = []
    rows = db().execute(
        "SELECT o.*,u.name scheduler_name FROM one_on_one_appointments o "
        "JOIN users u ON u.id=o.scheduled_by WHERE o.ra_user_id=?",
        (user_id,),
    ).fetchall()
    for row in rows:
        if row["recurrence_kind"] == "monthly":
            start = now.date().replace(day=1)
            end = date(start.year + 2, start.month, 1)
            future = [item for item in _series_occurrences(row, start, end) if item >= now]
            occurrence_text = min(future).isoformat(timespec="minutes") if future else None
        else:
            occurrence_text = next_occurrence(row["scheduled_at"], row["repeat_weeks"], now=now)
        if occurrence_text and datetime.fromisoformat(occurrence_text) >= now:
            item = dict(row)
            item["scheduled_at"] = occurrence_text
            item["recurrence_label"] = _recurrence_label(item)
            candidates.append(item)
    return min(candidates, key=lambda item: item["scheduled_at"]) if candidates else None


def one_on_one_calendar_context(user):
    selected = _calendar_month(request.args.get("one_on_one_month"))
    end = _next_month(selected)
    base_query = (
        "SELECT o.*,u.name ra_name,u.role recipient_role,b.name building_name "
        "FROM one_on_one_appointments o JOIN users u ON u.id=o.ra_user_id "
        "LEFT JOIN buildings b ON b.id=u.building_id"
    )
    building_limited = bool(user["is_prostaff"] or user["admin_lite"])
    if building_limited and not user["building_id"]:
        rows = []
    elif building_limited:
        rows = db().execute(
            base_query + " WHERE u.building_id=? ORDER BY o.scheduled_at,u.name",
            (user["building_id"],),
        ).fetchall()
    else:
        rows = db().execute(
            base_query + " ORDER BY o.scheduled_at,u.name"
        ).fetchall()
    by_day = {}
    for row in rows:
        for occurrence in _series_occurrences(row, selected, end):
            item = dict(row)
            item["scheduled_at"] = occurrence.strftime("%Y-%m-%dT%H:%M")
            item["recurrence_label"] = _recurrence_label(item)
            by_day.setdefault(occurrence.day, []).append(item)
    for items in by_day.values():
        items.sort(key=lambda item: (item["scheduled_at"], item["ra_name"]))
    if building_limited and not user["building_id"]:
        recipients = []
    else:
        recipient_scope = " AND u.building_id=?" if building_limited else ""
        recipient_params = (user["building_id"],) if building_limited else ()
        recipients = db().execute(
            "SELECT u.id,u.name,u.role,b.name building_name FROM users u "
            "LEFT JOIN buildings b ON b.id=u.building_id "
            "WHERE u.role IN ('RA','HRA','ADMIN') AND u.is_prostaff=0 AND u.disabled=0 "
            "AND u.building_id IS NOT NULL" + recipient_scope + " ORDER BY b.name,u.role,u.name",
            recipient_params,
        ).fetchall()
    return {
        "one_on_one_recipients": recipients,
        "one_on_one_weeks": monthcalendar(selected.year, selected.month),
        "one_on_one_by_day": by_day,
        "one_on_one_month": selected.strftime("%Y-%m"),
        "one_on_one_month_label": selected.strftime("%B %Y"),
    }


def _recurrence_label(row):
    if row.get("recurrence_kind") == "monthly":
        anchor = datetime.fromisoformat(row["scheduled_at"])
        ordinal = (anchor.day - 1) // 7 + 1
        names = ("first", "second", "third", "fourth", "fifth")
        return f"Monthly · {names[ordinal - 1]} {anchor.strftime('%A')}"
    if row.get("repeat_weeks") == 2:
        return "Every 2 weeks"
    if row.get("repeat_weeks") == 1:
        return "Every week"
    return "One time"


@app.context_processor
def inject_one_on_ones():
    user = current_user()
    if user and request.endpoint in {"prostaff_dashboard", "prostaff_one_on_ones"} and (
            user["is_prostaff"] or user["role"] == "ADMIN" or user["admin_lite"]):
        return {"one_on_one": None, **one_on_one_calendar_context(user)}
    if not user or user["is_prostaff"] or user["role"] not in ("RA", "HRA", "ADMIN"):
        return {"one_on_one": None}
    return {"one_on_one": _recipient_next_appointment(user["id"])}


def _require_locked_ac(conn, actor_id):
    actor = conn.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone()
    if not actor or actor["disabled"] or not actor["is_prostaff"]:
        conn.rollback()
        abort(403)
    return actor


def _require_recipient(conn, recipient_id, building_id):
    recipient = conn.execute("SELECT * FROM users WHERE id=?", (recipient_id,)).fetchone()
    if (not recipient or recipient["disabled"] or recipient["is_prostaff"]
            or recipient["role"] not in ("RA", "HRA", "ADMIN")
            or not building_id or recipient["building_id"] != building_id):
        conn.rollback()
        abort(403)
    return recipient


@app.route("/one-on-ones", methods=["POST"])
def schedule_one_on_one():
    require_csrf()
    actor = current_user()
    if not actor or not actor["is_prostaff"]:
        abort(403)
    try:
        recipient_id = int(request.form.get("recipient_user_id", ""))
        raw_start = request.form.get("scheduled_at") or (
            f"{request.form.get('start_date', '')}T{request.form.get('start_time', '')}"
        )
        scheduled_at = parse_event_start(raw_start)
        recurrence_kind = request.form.get("recurrence", "legacy")
        if recurrence_kind == "monthly":
            repeat_weeks = 0
        elif recurrence_kind == "biweekly":
            recurrence_kind, repeat_weeks = "weekly", 2
        elif recurrence_kind == "once":
            repeat_weeks = 0
        else:
            recurrence_kind = "weekly"
            repeat_weeks = parse_repeat_weeks(request.form.get("repeat_weeks"))
        if scheduled_at <= datetime.now(SCHOOL_TIMEZONE).strftime("%Y-%m-%dT%H:%M"):
            raise ValueError
        location = clean_single_line(request.form.get("location"), max_length=120)
    except (TypeError, ValueError):
        flash("Choose a staff member, a future time, a valid repeat interval, and a location.", "error")
        return redirect(url_for("prostaff_one_on_ones"))
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    locked_actor = _require_locked_ac(conn, actor["id"])
    _require_recipient(conn, recipient_id, locked_actor["building_id"])
    if _has_schedule_conflict(conn, actor["id"], recipient_id, scheduled_at, repeat_weeks,
                              recurrence_kind):
        conn.rollback()
        flash("That time conflicts with an existing one-on-one for you or that staff member.", "error")
        return redirect(url_for("prostaff_one_on_ones"))
    cur = conn.execute(
        "INSERT INTO one_on_one_appointments"
        "(ra_user_id,scheduled_by,scheduled_at,location,repeat_weeks,recurrence_kind) "
        "VALUES(?,?,?,?,?,?)",
        (recipient_id, actor["id"], scheduled_at, location, repeat_weeks, recurrence_kind),
    )
    audit("one_on_one.create", "one_on_one", cur.lastrowid,
          {"recipient_user_id": recipient_id, "scheduled_at": scheduled_at,
           "repeat_weeks": repeat_weeks})
    conn.commit()
    flash("One-on-one schedule saved.", "success")
    return redirect(url_for("prostaff_one_on_ones", one_on_one_month=scheduled_at[:7]))


@app.route("/one-on-ones/<int:appointment_id>/delete", methods=["POST"])
def delete_one_on_one(appointment_id):
    require_csrf()
    actor = current_user()
    if not actor or not actor["is_prostaff"]:
        abort(403)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    locked_actor = _require_locked_ac(conn, actor["id"])
    row = conn.execute(
        "SELECT o.* FROM one_on_one_appointments o JOIN users u ON u.id=o.ra_user_id "
        "WHERE o.id=? AND u.building_id=?",
        (appointment_id, locked_actor["building_id"]),
    ).fetchone()
    if not row:
        conn.rollback()
        abort(404)
    conn.execute("DELETE FROM one_on_one_appointments WHERE id=?", (appointment_id,))
    audit("one_on_one.delete", "one_on_one", appointment_id,
          {"recipient_user_id": row["ra_user_id"], "scheduled_at": row["scheduled_at"]})
    conn.commit()
    flash("One-on-one series removed.", "success")
    return redirect(url_for("prostaff_one_on_ones", one_on_one_month=row["scheduled_at"][:7]))
