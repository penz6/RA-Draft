"""Additional session management routes (non-swap)."""

import sqlite3
from flask import abort, flash, redirect, request, url_for

from core import (
    app,
    audit,
    can_manage,
    can_view_session,
    current_user,
    db,
    login_required,
    require_csrf,
    roles,
    session_row,
)
from session_action_response import session_action_response


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
