from flask import abort, flash, redirect, url_for

from core import app, audit, can_manage, current_user, db, require_csrf, roles, session_row


@app.route("/sessions/<int:session_id>/pause/<int:user_id>", methods=["POST"])
@roles("HRA", "ADMIN")
def toggle_participant_pause(session_id, user_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or not can_manage(user, row):
        abort(403)
    if row["status"] != "OPEN":
        flash("Reopen the session before changing the draft order.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    participant = conn.execute(
        "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if not participant:
        conn.rollback()
        abort(400)

    existing = conn.execute(
        "SELECT 1 FROM session_deferrals WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if existing:
        conn.execute(
            "DELETE FROM session_deferrals WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        )
        audit("draft.turn.restore", "user", user_id, {"session_id": session_id})
        conn.commit()
        flash("RA restored to the draft order.", "success")
        return redirect(url_for("view_session", session_id=session_id))

    assigned = conn.execute(
        "SELECT 1 FROM assignments WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if assigned:
        conn.rollback()
        flash("Remove the RA's assignment before deferring their turn.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    conn.execute(
        "INSERT INTO session_deferrals(session_id,user_id,deferred_by) VALUES(?,?,?)",
        (session_id, user_id, user["id"]),
    )
    audit("draft.turn.defer", "user", user_id, {"session_id": session_id})
    conn.commit()
    flash("RA deferred. You can restore or manually assign them later.", "success")
    return redirect(url_for("view_session", session_id=session_id))
