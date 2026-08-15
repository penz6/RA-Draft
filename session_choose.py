import sqlite3

from flask import abort, flash, redirect, request, url_for

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


@app.route("/sessions/<int:session_id>/choose", methods=["POST"])
@login_required
def choose_shift(session_id):
    require_csrf()
    user = current_user()
    row = session_row(session_id)
    if not row or row["status"] != "OPEN":
        abort(400)
    if not can_view_session(user, row):
        abort(403)

    duty_date = request.form.get("duty_date", "")
    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    # Re-check all authorization and session state after taking the write lock.
    # This closes the race where the session could be closed or the user's
    # building/role could change between the initial page submission and write.
    user = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if row["status"] != "OPEN":
        conn.rollback()
        flash("This duty session is closed.", "error")
        return redirect(url_for("view_session", session_id=session_id))
    if not can_view_session(user, row):
        conn.rollback()
        abort(403)

    current = next_picker(session_id)
    if not current or current["id"] != user["id"]:
        conn.rollback()
        abort(403)
    if duty_date not in selectable_dates(row, user["id"]):
        conn.rollback()
        flash("That date is not available for this turn.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    try:
        cur = conn.execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
            "VALUES(?,?,?,?)",
            (session_id, user["id"], duty_date, user["id"]),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("You are already assigned to that date.", "error")
        return redirect(url_for("view_session", session_id=session_id))

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
    flash(
        "Every duty slot is filled." if complete else "Duty date selected. The turn advanced.",
        "success",
    )
    return redirect(url_for("view_session", session_id=session_id))
