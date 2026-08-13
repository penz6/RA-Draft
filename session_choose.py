from flask import abort, flash, redirect, request, url_for
from core import app, current_user, dates_for, db, login_required, next_picker, require_csrf, session_row

@app.route("/sessions/<int:session_id>/choose", methods=["POST"])
@login_required
def choose_shift(session_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or row["status"] != "OPEN":
        abort(400)
    if user["role"] != "ADMIN" and user["building_id"] != row["building_id"]:
        abort(403)
    current = next_picker(session_id)
    if not current or current["id"] != user["id"]:
        abort(403)
    duty_date = request.form["duty_date"]
    if duty_date not in dates_for(row):
        abort(400)
    count = db().execute(
        "SELECT COUNT(*) n FROM assignments WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()["n"]
    if count >= row["capacity"]:
        flash("That duty date is full.", "error")
    else:
        db().execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
            (session_id, user["id"], duty_date, user["id"]),
        )
        db().commit()
        flash("Duty shift selected.", "success")
    return redirect(url_for("view_session", session_id=session_id))
