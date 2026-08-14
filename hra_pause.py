from flask import abort, flash, redirect, url_for

from core import (
    advance_turn,
    app,
    audit,
    can_manage,
    current_user,
    db,
    next_picker,
    require_csrf,
    roles,
    session_row,
)


@app.route("/sessions/<int:session_id>/pause/<int:user_id>", methods=["POST"])
@roles("HRA", "ADMIN")
def toggle_participant_pause(session_id, user_id):
    require_csrf()
    row = session_row(session_id)
    manager = current_user()
    if not row or not can_manage(manager, row):
        abort(403)
    if row["status"] != "OPEN":
        flash("Reopen the session before changing participant availability.", "error")
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
        audit("draft.participant.restore", "user", user_id, {"session_id": session_id})
        conn.commit()
        flash("Participant restored to future turns.", "success")
        return redirect(url_for("view_session", session_id=session_id))

    current = next_picker(session_id)
    conn.execute(
        "INSERT INTO session_deferrals(session_id,user_id,deferred_by) VALUES(?,?,?)",
        (session_id, user_id, manager["id"]),
    )
    if current and current["id"] == user_id:
        advance_turn(session_id, user_id)
    audit("draft.participant.pause", "user", user_id, {"session_id": session_id})
    conn.commit()
    flash("Participant paused. They can be restored later.", "success")
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/skip/<int:user_id>", methods=["POST"])
@roles("HRA", "ADMIN")
def skip_participant_turn(session_id, user_id):
    require_csrf()
    row = session_row(session_id)
    manager = current_user()
    if not row or not can_manage(manager, row):
        abort(403)
    if row["status"] != "OPEN":
        flash("Reopen the session before skipping a turn.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    current = next_picker(session_id)
    if not current or current["id"] != user_id:
        conn.rollback()
        flash("That participant is not the current picker.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    advance_turn(session_id, user_id)
    audit("draft.turn.skip", "user", user_id, {"session_id": session_id})
    conn.commit()
    flash("Turn skipped once. The participant remains active for later rounds.", "success")
    return redirect(url_for("view_session", session_id=session_id))
