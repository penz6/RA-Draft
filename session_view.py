"""Session view rendering and real-time partial HTML fragment endpoints."""

from collections import defaultdict
from flask import abort, redirect, render_template, url_for

from busy_days import busy_dates_for
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
    login_required,
    next_picker,
    ordered_people,
    selectable_dates,
    selection_phase_label,
    session_row,
    user_upcoming_shifts,
    db,
)
from live_updates import session_state_version


def _session_template_context(session_id, user):
    """
    Build the complete template context for a session view.
    
    Parameters:
    	session_id: The session identifier.
    	user: The authenticated user requesting the session.
    
    Returns:
    	dict: Template context containing session details, participants, assignments, scheduling data, permissions, completion state, and live-update information.
    """
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)

    people = ordered_people(session_id)
    picks = db().execute(
        "SELECT a.*,u.name,u.role,u.picture_url,o.position FROM assignments a "
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
    complete = all(counts.get(day, 0) >= capacity for day, capacity in capacities.items())
    filled = sum(counts.values())
    slots = sum(capacities.values())
    manager_allowed = can_manage(user, row)
    phase_label = selection_phase_label(row)
    live_version = session_state_version(row, user)

    return {
        "busy_dates": busy_dates_for(user["id"]),
        "me": user,
        "draft": row,
        "people": people,
        "picks": picks,
        "counts": counts,
        "dates": dates,
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
        "self_assigned_dates": {
            assignment["duty_date"]
            for assignment in picks
            if assignment["user_id"] == user["id"]
        },
        "selectable_dates": self_selectable,
        "next": current_picker,
        "can_manage": manager_allowed,
        "participant_count": len(people),
        "assigned_count": filled,
        "total_slot_count": slots,
        "open_slot_count": max(slots - filled, 0),
        "schedule_complete": complete,
        "selection_phase": phase_label,
        "live_version": live_version,
    }


def _begin_session_snapshot(session_id):
    """Open an explicit transaction snapshot to read session state consistently."""
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


@app.route("/my-duty-shifts")
@login_required
def upcoming_duty_shifts():
    """Show the signed-in user's complete list of upcoming duty shifts."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    shifts = user_upcoming_shifts(user["id"])
    return render_template(
        "upcoming_duty_shifts.html",
        me=user,
        upcoming_shifts=shifts,
    )


@app.route("/sessions/<int:session_id>")
@login_required
def view_session(session_id):
    """Render the interactive session picking page within a read transaction."""
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
    """Return dynamic session HTML fragments for background SSE live updates."""
    conn, context = _begin_session_snapshot(session_id)
    if context is None:
        return redirect(url_for("login"))
    try:
        app.update_template_context(context)
        fragments = {
            name: app.jinja_env.get_template(template).render(context)
            for name, template in (
                ("heading", "session_heading_live_v2.html"),
                ("summary", "session_summary_live_v2.html"),
                ("status", "session_status_live_v2.html"),
                ("turn_order", "session_order_v2.html"),
                ("calendar", "session_dates_v2.html"),
                ("assignments", "session_assignments_v2.html"),
            )
        }
        version = context["live_version"]
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return {"version": version, "fragments": fragments}
