from flask import abort, flash, redirect, request, url_for

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


def _locked_manager_session(conn, session_id):
    manager = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_manage(manager, row):
        conn.rollback()
        abort(403)
    if row["status"] != "OPEN":
        conn.rollback()
        flash("Reopen the session before changing picking.", "error")
        return None, None
    return manager, row


@app.route("/sessions/<int:session_id>/picking", methods=["POST"])
@roles("HRA", "ADMIN")
def session_picking(session_id):
    require_csrf()
    paused_value = request.form.get("paused")
    if paused_value not in ("0", "1"):
        abort(400)
    paused = int(paused_value)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)
    if not row:
        return redirect(url_for("view_session", session_id=session_id))

    current = int(bool(row["picking_paused"]))
    if current == paused:
        conn.rollback()
        return redirect(url_for("view_session", session_id=session_id))

    conn.execute(
        "UPDATE draft_sessions SET picking_paused=? WHERE id=?",
        (paused, session_id),
    )
    audit(
        "draft.picking.pause" if paused else "draft.picking.resume",
        "session",
        session_id,
        {"paused": bool(paused)},
    )
    conn.commit()
    flash(
        "Picking paused. The current turn is frozen until picking is resumed."
        if paused
        else "Picking resumed. The current turn can continue.",
        "success",
    )
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/pause/<int:user_id>", methods=["POST"])
@roles("HRA", "ADMIN")
def toggle_participant_pause(session_id, user_id):
    """Compatibility endpoint for old links; individual pausing is retired."""

    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)
    if not row:
        return redirect(url_for("view_session", session_id=session_id))

    participant = conn.execute(
        "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if not participant:
        conn.rollback()
        abort(400)

    # Clear any legacy per-user deferral marker rather than creating or using
    # one. The active turn model no longer considers session_deferrals.
    conn.execute(
        "DELETE FROM session_deferrals WHERE session_id=? AND user_id=?",
        (session_id, user_id),
    )
    audit(
        "draft.participant.pause_retired",
        "user",
        user_id,
        {"session_id": session_id},
    )
    conn.commit()
    flash(
        "Individual pausing is no longer used. Pause picking for the whole session instead.",
        "error",
    )
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/skip/<int:user_id>", methods=["POST"])
@roles("HRA", "ADMIN")
def skip_participant_turn(session_id, user_id):
    require_csrf()

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)
    if not row:
        return redirect(url_for("view_session", session_id=session_id))
    if row["picking_paused"]:
        conn.rollback()
        flash("Resume picking before skipping a turn.", "error")
        return redirect(url_for("view_session", session_id=session_id))

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
