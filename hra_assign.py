from flask import abort, flash, redirect, request, url_for
from core import app, can_manage, current_user, dates_for, db, require_csrf, roles, session_row

@app.route("/sessions/<int:session_id>/assign", methods=["POST"])
@roles("HRA", "ADMIN")
def manual_assign(session_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or not can_manage(user, row):
        abort(403)
    uid = int(request.form["user_id"])
    duty_date = request.form.get("duty_date", "")
    if not db().execute(
        "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, uid),
    ).fetchone():
        abort(400)

    if duty_date:
        if duty_date not in dates_for(row):
            abort(400)
        count = db().execute(
            "SELECT COUNT(*) n FROM assignments WHERE session_id=? AND duty_date=? AND user_id<>?",
            (session_id, duty_date, uid),
        ).fetchone()["n"]
        if count >= row["capacity"]:
            flash("That date is already at capacity.", "error")
            return redirect(url_for("view_session", session_id=session_id))
        db().execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id,user_id) DO UPDATE SET "
            "duty_date=excluded.duty_date,created_by=excluded.created_by,created_at=CURRENT_TIMESTAMP",
            (session_id, uid, duty_date, user["id"]),
        )
        db().execute(
            "DELETE FROM session_deferrals WHERE session_id=? AND user_id=?",
            (session_id, uid),
        )
    else:
        db().execute(
            "DELETE FROM assignments WHERE session_id=? AND user_id=?",
            (session_id, uid),
        )
    db().commit()
    flash("Assignment updated.", "success")
    return redirect(url_for("view_session", session_id=session_id))
