import sqlite3

from flask import abort, flash, redirect, request, url_for

from core import (
    advance_turn,
    app,
    audit,
    calendar_dates,
    can_manage,
    current_user,
    db,
    effective_capacity,
    is_participant,
    next_picker,
    require_csrf,
    roles,
    session_complete,
    session_row,
)


@app.route("/sessions/<int:session_id>/assign", methods=["POST"])
@roles("HRA", "ADMIN")
def manual_assign(session_id):
    require_csrf()
    row = session_row(session_id)
    manager = current_user()
    if not row or not can_manage(manager, row):
        abort(403)

    try:
        user_id = int(request.form["user_id"])
    except (KeyError, TypeError, ValueError):
        abort(400)
    duty_date = request.form.get("duty_date", "")

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_manage(manager, row):
        conn.rollback()
        abort(403)
    if row["status"] != "OPEN":
        conn.rollback()
        flash("Reopen the session before assigning duty dates.", "error")
        return redirect(url_for("view_session", session_id=session_id))
    if row["picking_paused"]:
        conn.rollback()
        flash("Resume picking before assigning duty dates.", "error")
        return redirect(url_for("view_session", session_id=session_id))
    if not is_participant(session_id, user_id):
        conn.rollback()
        abort(400)
    if duty_date not in calendar_dates(row):
        conn.rollback()
        abort(400)

    current = next_picker(session_id)
    duplicate = conn.execute(
        "SELECT 1 FROM assignments WHERE session_id=? AND user_id=? AND duty_date=?",
        (session_id, user_id, duty_date),
    ).fetchone()
    if duplicate:
        conn.rollback()
        flash("That participant is already assigned to that date.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    assigned = conn.execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()["n"]
    capacity = effective_capacity(row, duty_date)
    if assigned >= capacity:
        conn.rollback()
        flash("That date is already at capacity.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    # Legacy per-user deferral markers are inert, but remove one if present so
    # old records do not linger once that participant is touched again.
    legacy_marker = bool(
        conn.execute(
            "SELECT 1 FROM session_deferrals WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
    )
    try:
        cur = conn.execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
            "VALUES(?,?,?,?)",
            (session_id, user_id, duty_date, manager["id"]),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That assignment could not be added.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    if legacy_marker:
        conn.execute(
            "DELETE FROM session_deferrals WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        )
    consumed_turn = bool(current and current["id"] == user_id)
    if consumed_turn:
        advance_turn(session_id, user_id)
    audit(
        "assignment.manager_add",
        "assignment",
        cur.lastrowid,
        {
            "session_id": session_id,
            "user_id": user_id,
            "duty_date": duty_date,
            "consumed_turn": consumed_turn,
        },
    )
    complete = session_complete(row)
    conn.commit()
    flash(
        "Every duty slot is filled."
        if complete
        else ("Assignment added and the turn advanced." if consumed_turn else "Assignment added."),
        "success",
    )
    return redirect(url_for("view_session", session_id=session_id))


@app.route(
    "/sessions/<int:session_id>/assignments/<int:assignment_id>/delete",
    methods=["POST"],
)
@roles("HRA", "ADMIN")
def delete_assignment(session_id, assignment_id):
    require_csrf()
    row = session_row(session_id)
    manager = current_user()
    if not row or not can_manage(manager, row):
        abort(403)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_manage(manager, row):
        conn.rollback()
        abort(403)

    assignment = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=?",
        (assignment_id, session_id),
    ).fetchone()
    if not assignment:
        conn.rollback()
        abort(404)
    conn.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))
    audit(
        "assignment.manager_remove",
        "assignment",
        assignment_id,
        {
            "session_id": session_id,
            "user_id": assignment["user_id"],
            "duty_date": assignment["duty_date"],
        },
    )
    conn.commit()
    flash("Assignment removed.", "success")
    return redirect(url_for("view_session", session_id=session_id))
