import sqlite3

from flask import abort, request

from core import (
    advance_turn,
    app,
    audit,
    can_view_session,
    current_user,
    db,
    login_required,
    next_picker,
    require_csrf,
    selectable_dates,
    session_complete,
    session_row,
)
from session_action_response import session_action_response


@app.route("/sessions/<int:session_id>/choose", methods=["POST"])
@login_required
def choose_shift(session_id):
    require_csrf()
    user = current_user()
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)
    if row["status"] != "OPEN":
        return session_action_response(
            session_id,
            "This duty session is closed.",
            category="error",
            status=409,
        )
    if row["picking_paused"]:
        return session_action_response(
            session_id,
            "Picking is paused by an HRA or Admin.",
            category="error",
            status=409,
        )

    duty_date = request.form.get("duty_date", "")
    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    user = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_view_session(user, row):
        conn.rollback()
        abort(403)
    if row["status"] != "OPEN":
        conn.rollback()
        return session_action_response(
            session_id,
            "This duty session is closed.",
            category="error",
            status=409,
        )
    if row["picking_paused"]:
        conn.rollback()
        return session_action_response(
            session_id,
            "Picking is paused by an HRA or Admin.",
            category="error",
            status=409,
        )

    current = next_picker(session_id)
    if not current or current["id"] != user["id"]:
        conn.rollback()
        return session_action_response(
            session_id,
            "It is no longer your turn. The schedule has been refreshed.",
            category="error",
            status=409,
        )
    if duty_date not in selectable_dates(row, user["id"]):
        conn.rollback()
        return session_action_response(
            session_id,
            "That date is not available for this turn.",
            category="error",
            status=409,
        )

    try:
        cur = conn.execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
            "VALUES(?,?,?,?)",
            (session_id, user["id"], duty_date, user["id"]),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return session_action_response(
            session_id,
            "You are already assigned to that date.",
            category="error",
            status=409,
        )

    advance_turn(session_id, user["id"])
    audit(
        "assignment.self_pick",
        "assignment",
        cur.lastrowid,
        {
            "session_id": session_id,
            "user_id": user["id"],
            "duty_date": duty_date,
        },
    )
    complete = session_complete(row)
    conn.commit()
    return session_action_response(
        session_id,
        "Every duty slot is filled."
        if complete
        else "Duty date selected. The turn advanced.",
    )
