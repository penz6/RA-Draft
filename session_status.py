from flask import abort, redirect, request, url_for

from core import app, audit, can_manage, current_user, db, require_csrf, roles, session_row


@app.route("/sessions/<int:session_id>/status", methods=["POST"])
@roles("HRA", "ADMIN")
def session_status(session_id):
    require_csrf()
    row = session_row(session_id)
    if not row or not can_manage(current_user(), row):
        abort(403)
    status = request.form.get("status")
    if status not in ("OPEN", "CLOSED"):
        abort(400)
    if status != row["status"]:
        db().execute("UPDATE draft_sessions SET status=? WHERE id=?", (status, session_id))
        audit(
            "draft.session.status",
            "session",
            session_id,
            {"old_status": row["status"], "new_status": status},
        )
        db().commit()
    return redirect(url_for("view_session", session_id=session_id))


import session_date_rules  # noqa: E402,F401
