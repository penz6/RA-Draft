"""Additional session management routes (non-swap)."""

import secrets
import sqlite3
from flask import abort, flash, redirect, render_template, request, url_for

from core import (
    app,
    audit,
    can_manage,
    can_view_session,
    current_user,
    db,
    login_required,
    next_picker,
    ordered_people,
    selectable_dates,
    require_csrf,
    roles,
    session_row,
)
from session_action_response import session_action_response
from session_pause import pause_for_phase_confirmation


def _locked_manager_session(conn, session_id):
    """Verify management authorization and fetch session row inside an immediate transaction."""
    manager = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_manage(manager, row):
        conn.rollback()
        abort(403)
    return manager, row


@app.route("/sessions/<int:session_id>/delete", methods=["POST"])
@roles("ADMIN")
def delete_session(session_id):
    """Permanently delete a draft session and all its associated records (Admin only)."""
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)

    conn.execute("DELETE FROM draft_sessions WHERE id=?", (session_id,))
    audit(
        "draft.session.delete",
        "session",
        session_id,
        {
            "name": row["name"],
            "building_id": row["building_id"],
            "building_name": row["building_name"],
        },
    )
    conn.commit()
    flash(f"Session '{row['name']}' was permanently deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/sessions/<int:session_id>/order/start", methods=["POST"])
@roles("HRA", "ADMIN")
def begin_picking_order_edit(session_id):
    """Commit a picking pause before showing the order editor."""
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)
    if row["status"] != "OPEN":
        conn.rollback()
        abort(409, "Reopen the session before editing the picking order.")
    if pause_for_phase_confirmation(session_id):
        row = session_row(session_id)
    if not row["order_edit_token"]:
        conn.execute(
            "UPDATE draft_sessions SET picking_paused=1,order_edit_token=? WHERE id=?",
            (secrets.token_urlsafe(32), session_id),
        )
        audit("draft.order.edit_started", "session", session_id, {})
    conn.commit()
    return redirect(url_for("edit_picking_order", session_id=session_id))


@app.route("/sessions/<int:session_id>/order", methods=["GET", "POST"])
@roles("HRA", "ADMIN")
def edit_picking_order(session_id):
    """Validate and atomically save the complete order and selected next turn."""
    if request.method == "POST":
        require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE" if request.method == "POST" else "BEGIN")
    manager, row = _locked_manager_session(conn, session_id)
    if row["status"] != "OPEN" or not row["picking_paused"] or not row["order_edit_token"]:
        conn.rollback()
        flash("Start editing from the session manager to pause picking first.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    people = ordered_people(session_id)
    old_order = [p["id"] for p in people]
    if row["phase_order_state"] == 1:
        people = list(reversed(people))
    eligible = [p for p in people if not p["disabled"] and selectable_dates(row, p["id"])]
    error = None
    if request.method == "POST":
        if request.form.get("edit_token") != row["order_edit_token"]:
            conn.rollback()
            abort(409, "This editor is out of date. Return to the session and open it again.")
        action = request.form.get("action")
        if action == "cancel":
            if row["phase_order_state"] != 1:
                conn.execute("UPDATE draft_sessions SET order_edit_token=NULL WHERE id=?", (session_id,))
            audit("draft.order.edit_canceled", "session", session_id, {})
            conn.commit()
            flash("Order unchanged. Picking remains paused.", "success")
            return redirect(url_for("view_session", session_id=session_id))
        try:
            positions = [int(request.form.get(f"position_{p['id']}", "")) for p in people]
            next_id = int(request.form.get("next_user_id", ""))
            if sorted(positions) != list(range(1, len(people) + 1)):
                raise ValueError
            if next_id not in {p["id"] for p in eligible} or action != "save":
                raise ValueError
        except (ValueError, TypeError):
            error = "Use each position exactly once and choose an eligible next picker."
        else:
            new_order = [p["id"] for _, p in sorted(zip(positions, people), key=lambda item: item[0])]
            # Temporary positions avoid the unique session/position constraint
            # without deleting membership rows or touching existing assignments.
            offset = max((p["position"] for p in people), default=0) + len(people)
            conn.execute("UPDATE session_order SET position=position+? WHERE session_id=?", (offset, session_id))
            conn.executemany(
                "UPDATE session_order SET position=? WHERE session_id=? AND user_id=?",
                [(position, session_id, uid) for position, uid in enumerate(new_order, 1)],
            )
            conn.execute(
                "UPDATE draft_sessions SET current_position=?,picking_paused=0,order_edit_token=NULL, "
                "phase_order_state=CASE WHEN phase_order_state=1 THEN 2 ELSE phase_order_state END WHERE id=?",
                (new_order.index(next_id) + 1, session_id),
            )
            audit("draft.order.updated", "session", session_id, {
                "old_order": old_order, "new_order": new_order,
                "next_user_id": next_id, "confirmed_phase_reversal": row["phase_order_state"] == 1,
            })
            conn.commit()
            flash("Picking order saved. Picking resumed with the selected participant.", "success")
            return redirect(url_for("view_session", session_id=session_id))

    page = render_template("session_order_edit.html", me=manager, draft=row,
                           people=people, eligible=eligible,
                           next=(eligible[0] if eligible else None) if row["phase_order_state"] == 1 else next_picker(session_id),
                           error=error)
    conn.commit()
    return page, 400 if error else 200
