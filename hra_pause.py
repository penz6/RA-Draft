from flask import abort, flash, redirect, url_for
from core import app, can_manage, current_user, db, require_csrf, roles, session_row

@app.route("/sessions/<int:session_id>/pause/<int:user_id>", methods=["POST"])
@roles("HRA", "ADMIN")
def toggle_participant_pause(session_id, user_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or not can_manage(user, row):
        abort(403)
    if not db().execute(
        "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone():
        abort(400)
    existing = db().execute(
        "SELECT 1 FROM session_deferrals WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if existing:
        db().execute(
            "DELETE FROM session_deferrals WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        )
        db().commit()
        flash("RA restored to the draft order.", "success")
        return redirect(url_for("view_session", session_id=session_id))
    if db().execute(
        "SELECT 1 FROM assignments WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone():
        flash("Remove the RA's assignment before pausing them.", "error")
        return redirect(url_for("view_session", session_id=session_id))
    db().execute(
        "INSERT INTO session_deferrals(session_id,user_id,deferred_by) VALUES(?,?,?)",
        (session_id, user_id, user["id"]),
    )
    db().commit()
    flash("RA paused. You can restore or manually assign them later.", "success")
    return redirect(url_for("view_session", session_id=session_id))
