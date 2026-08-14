from flask import abort, flash, redirect, request, url_for

from core import (
    app,
    audit,
    can_manage,
    current_user,
    dates_for,
    db,
    effective_capacity,
    require_csrf,
    roles,
    session_row,
)


@app.route("/sessions/<int:session_id>/assign", methods=["POST"])
@roles("HRA", "ADMIN")
def manual_assign(session_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or not can_manage(user, row):
        abort(403)

    try:
        uid = int(request.form["user_id"])
    except (KeyError, TypeError, ValueError):
        abort(400)
    duty_date = request.form.get("duty_date", "")

    if not db().execute(
        "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, uid),
    ).fetchone():
        abort(400)
    if duty_date and duty_date not in dates_for(row):
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT id,duty_date FROM assignments WHERE session_id=? AND user_id=?",
        (session_id, uid),
    ).fetchone()

    if duty_date:
        count = conn.execute(
            "SELECT COUNT(*) n FROM assignments "
            "WHERE session_id=? AND duty_date=? AND user_id<>?",
            (session_id, duty_date, uid),
        ).fetchone()["n"]
        capacity = effective_capacity(row, duty_date)
        if count >= capacity:
            conn.rollback()
            flash("That date is already at capacity.", "error")
            return redirect(url_for("view_session", session_id=session_id))

        conn.execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(session_id,user_id) DO UPDATE SET "
            "duty_date=excluded.duty_date,created_by=excluded.created_by,"
            "created_at=CURRENT_TIMESTAMP",
            (session_id, uid, duty_date, user["id"]),
        )
        assignment = conn.execute(
            "SELECT id FROM assignments WHERE session_id=? AND user_id=?",
            (session_id, uid),
        ).fetchone()
        conn.execute(
            "DELETE FROM session_deferrals WHERE session_id=? AND user_id=?",
            (session_id, uid),
        )
        audit(
            "assignment.manual_set",
            "assignment",
            assignment["id"],
            {
                "session_id": session_id,
                "user_id": uid,
                "old_duty_date": existing["duty_date"] if existing else None,
                "new_duty_date": duty_date,
                "effective_capacity": capacity,
            },
        )
    elif existing:
        conn.execute(
            "DELETE FROM assignments WHERE session_id=? AND user_id=?",
            (session_id, uid),
        )
        audit(
            "assignment.manual_remove",
            "assignment",
            existing["id"],
            {
                "session_id": session_id,
                "user_id": uid,
                "old_duty_date": existing["duty_date"],
            },
        )

    conn.commit()
    flash("Assignment updated.", "success")
    return redirect(url_for("view_session", session_id=session_id))
