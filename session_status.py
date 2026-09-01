from flask import abort, redirect, request, url_for

from core import app, audit, can_manage, current_user, db, require_csrf, roles, session_row
from email_notifications import send_session_closed_notifications
import swap_email_hooks  # noqa: F401
import swap_view_helpers  # noqa: F401


@app.route("/sessions/<int:session_id>/status", methods=["POST"])
@roles("HRA", "ADMIN")
def session_status(session_id):
    require_csrf()
    status = request.form.get("status")
    if status not in ("OPEN", "CLOSED"):
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    user = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_manage(user, row):
        conn.rollback()
        abort(403)
    if status == row["status"] and not row["picking_paused"]:
        conn.rollback()
        return redirect(url_for("view_session", session_id=session_id))

    notify_closed = row["status"] != "CLOSED" and status == "CLOSED"
    conn.execute(
        "UPDATE draft_sessions SET status=?,picking_paused=0 WHERE id=?",
        (status, session_id),
    )
    audit(
        "draft.session.status",
        "session",
        session_id,
        {
            "old_status": row["status"],
            "new_status": status,
            "cleared_picking_pause": bool(row["picking_paused"]),
        },
    )
    conn.commit()

    if notify_closed:
        try:
            send_session_closed_notifications(session_id)
        except Exception:  # Email must never change an already-committed session result.
            app.logger.exception("Could not prepare or dispatch RA Draft schedule emails.")

    return redirect(url_for("view_session", session_id=session_id))


import session_date_rules  # noqa: E402,F401
