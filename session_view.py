from collections import defaultdict

from flask import abort, redirect, render_template, url_for

from core import (
    DATE_KIND_LABELS,
    app,
    assignment_counts,
    calendar_dates,
    calendar_months,
    can_manage,
    can_view_session,
    capacities_for,
    capacity_overrides,
    current_user,
    date_kind_overrides,
    date_kinds_for,
    dates_for,
    filled_slots,
    login_required,
    next_picker,
    ordered_people,
    participant_count,
    selectable_dates,
    selection_phase_label,
    session_complete,
    session_row,
    total_slots,
    db,
)
from live_updates import session_state_version


def _session_template_context(session_id, user):
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)

    people = ordered_people(session_id)
    picks = db().execute(
        "SELECT a.*,u.name,u.role,o.position FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
        "WHERE a.session_id=? ORDER BY a.duty_date,o.position,a.id",
        (session_id,),
    ).fetchall()

    assignments_by_date = defaultdict(list)
    assignment_user_ids = defaultdict(list)
    for assignment in picks:
        assignments_by_date[assignment["duty_date"]].append(assignment)
        assignment_user_ids[assignment["duty_date"]].append(str(assignment["user_id"]))

    counts = assignment_counts(session_id)
    dates = calendar_dates(row)
    ordered_dates = dates_for(row)
    capacities = capacities_for(row)
    overrides = capacity_overrides(session_id)
    kinds = date_kinds_for(row)
    kind_overrides = date_kind_overrides(session_id)
    current_picker = next_picker(session_id)
    self_selectable = (
        set(selectable_dates(row, user["id"]))
        if row["status"] == "OPEN"
        and not row["picking_paused"]
        and current_picker
        and current_picker["id"] == user["id"]
        else set()
    )
    complete = session_complete(row)
    filled = filled_slots(session_id)
    slots = total_slots(row)
    people_count = participant_count(session_id)
    manager_allowed = can_manage(user, row)
    phase_label = selection_phase_label(row)
    live_version = session_state_version(row, user)

    return {
        "me": user,
        "draft": row,
        "people": people,
        "picks": picks,
        "counts": counts,
        "dates": dates,
        "ordered_dates": ordered_dates,
        "months": calendar_months(row),
        "capacities": capacities,
        "capacity_overrides": overrides,
        "date_kinds": kinds,
        "date_kind_overrides": kind_overrides,
        "date_kind_labels": DATE_KIND_LABELS,
        "assignments_by_date": dict(assignments_by_date),
        "assignment_user_ids": {
            duty_date: ",".join(user_ids)
            for duty_date, user_ids in assignment_user_ids.items()
        },
        "selectable_dates": self_selectable,
        "next": current_picker,
        "can_manage": manager_allowed,
        "participant_count": people_count,
        "assigned_count": filled,
        "total_slot_count": slots,
        "open_slot_count": max(slots - filled, 0),
        "available_date_count": sum(
            1
            for duty_date in dates
            if capacities[duty_date] > 0
            and counts.get(duty_date, 0) < capacities[duty_date]
        ),
        "schedule_complete": complete,
        "selection_phase": phase_label,
        "live_version": live_version,
    }


def _begin_session_snapshot(session_id):
    conn = db()
    conn.execute("BEGIN")
    try:
        user = current_user()
        if not user:
            conn.rollback()
            return conn, None
        context = _session_template_context(session_id, user)
        return conn, context
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


@app.route("/sessions/<int:session_id>")
@login_required
def view_session(session_id):
    # Render from one read snapshot so the HTML and data-live-version describe
    # the exact same database state. A later commit is then caught by SSE.
    conn, context = _begin_session_snapshot(session_id)
    if context is None:
        return redirect(url_for("login"))
    try:
        page = render_template("session_v2.html", **context)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return page


@app.route("/sessions/<int:session_id>/live-fragments")
@login_required
def session_live_fragments(session_id):
    """Return the current dynamic session regions without reloading the page."""

    conn, context = _begin_session_snapshot(session_id)
    if context is None:
        return redirect(url_for("login"))
    try:
        fragments = {
            "heading": render_template("session_heading_live_v2.html", **context),
            "summary": render_template("session_summary_live_v2.html", **context),
            "status": render_template("session_status_live_v2.html", **context),
            "turn_order": render_template("session_order_v2.html", **context),
            "calendar": render_template("session_dates_v2.html", **context),
            "assignments": render_template("session_assignments_v2.html", **context),
        }
        version = context["live_version"]
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return {"version": version, "fragments": fragments}
