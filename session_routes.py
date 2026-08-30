"""Additional session management and duty swap routes."""

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


@app.route("/sessions/<int:session_id>/swaps/request", methods=["POST"])
@login_required
def request_duty_swap(session_id):
    """Submit a peer-to-peer duty shift swap request for HRA approval."""
    require_csrf()
    user = current_user()
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)

    try:
        my_assignment_id = int(request.form.get("my_assignment_id", ""))
        target_assignment_id = int(request.form.get("target_assignment_id", ""))
    except (TypeError, ValueError):
        abort(400)

    if my_assignment_id == target_assignment_id:
        return session_action_response(
            session_id,
            "You cannot swap an assignment with itself.",
            category="error",
            status=400,
        )

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    my_assignment = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
        (my_assignment_id, session_id, user["id"]),
    ).fetchone()
    if not my_assignment:
        conn.rollback()
        return session_action_response(
            session_id,
            "Your selected assignment is not valid.",
            category="error",
            status=400,
        )

    target_assignment = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=?",
        (target_assignment_id, session_id),
    ).fetchone()
    if not target_assignment or target_assignment["user_id"] == user["id"]:
        conn.rollback()
        return session_action_response(
            session_id,
            "Target assignment must belong to a different participant in this session.",
            category="error",
            status=400,
        )

    target_user_id = target_assignment["user_id"]

    existing_pending = conn.execute(
        "SELECT 1 FROM duty_swap_requests WHERE session_id=? AND status='PENDING' "
        "AND (requester_assignment_id IN (?, ?) OR target_assignment_id IN (?, ?))",
        (session_id, my_assignment_id, target_assignment_id, my_assignment_id, target_assignment_id),
    ).fetchone()
    if existing_pending:
        conn.rollback()
        return session_action_response(
            session_id,
            "A pending swap request already exists for one of these assignments.",
            category="error",
            status=409,
        )

    cur = conn.execute(
        "INSERT INTO duty_swap_requests("
        "session_id, requester_user_id, requester_assignment_id, target_user_id, target_assignment_id, status"
        ") VALUES (?, ?, ?, ?, ?, 'PENDING')",
        (session_id, user["id"], my_assignment_id, target_user_id, target_assignment_id),
    )
    audit(
        "swap.request",
        "swap_request",
        cur.lastrowid,
        {
            "session_id": session_id,
            "requester_user_id": user["id"],
            "requester_assignment_id": my_assignment_id,
            "target_user_id": target_user_id,
            "target_assignment_id": target_assignment_id,
        },
    )
    conn.commit()
    return session_action_response(
        session_id,
        "Swap request submitted. An HRA or Admin can now review and approve it.",
    )


@app.route("/sessions/<int:session_id>/swaps/<int:swap_id>/review", methods=["POST"])
@roles("HRA", "ADMIN")
def review_duty_swap(session_id, swap_id):
    """Approve or reject a pending duty swap request (HRA/Admin only)."""
    require_csrf()
    action = request.form.get("action", "").strip().upper()
    if action not in ("APPROVE", "REJECT"):
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)

    swap = conn.execute(
        "SELECT * FROM duty_swap_requests WHERE id=? AND session_id=?",
        (swap_id, session_id),
    ).fetchone()
    if not swap or swap["status"] != "PENDING":
        conn.rollback()
        return session_action_response(
            session_id,
            "That swap request is no longer pending.",
            category="error",
            status=409,
        )

    if action == "REJECT":
        conn.execute(
            "UPDATE duty_swap_requests SET status='REJECTED', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (manager["id"], swap_id),
        )
        audit(
            "swap.reject",
            "swap_request",
            swap_id,
            {"session_id": session_id, "reviewed_by": manager["id"]},
        )
        conn.commit()
        return session_action_response(session_id, "Duty swap request rejected.")

    req_assign = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
        (swap["requester_assignment_id"], session_id, swap["requester_user_id"]),
    ).fetchone()
    target_assign = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
        (swap["target_assignment_id"], session_id, swap["target_user_id"]),
    ).fetchone()

    if not req_assign or not target_assign:
        conn.rollback()
        return session_action_response(
            session_id,
            "One or both assignments have changed; swap cannot be approved.",
            category="error",
            status=409,
        )

    conn.execute(
        "UPDATE assignments SET user_id=? WHERE id=?",
        (swap["target_user_id"], swap["requester_assignment_id"]),
    )
    conn.execute(
        "UPDATE assignments SET user_id=? WHERE id=?",
        (swap["requester_user_id"], swap["target_assignment_id"]),
    )
    conn.execute(
        "UPDATE duty_swap_requests SET status='APPROVED', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (manager["id"], swap_id),
    )
    audit(
        "swap.approve",
        "swap_request",
        swap_id,
        {
            "session_id": session_id,
            "requester_user_id": swap["requester_user_id"],
            "requester_assignment_id": swap["requester_assignment_id"],
            "target_user_id": swap["target_user_id"],
            "target_assignment_id": swap["target_assignment_id"],
            "reviewed_by": manager["id"],
        },
    )
    conn.commit()
    return session_action_response(session_id, "Duty swap approved and schedule updated.")


@app.route("/sessions/<int:session_id>/swaps/<int:swap_id>/cancel", methods=["POST"])
@login_required
def cancel_duty_swap(session_id, swap_id):
    """Cancel a pending swap request by the user who initiated it."""
    require_csrf()
    user = current_user()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    row = session_row(session_id)
    if not row or not can_view_session(user, row):
        conn.rollback()
        abort(403 if row else 404)

    swap = conn.execute(
        "SELECT * FROM duty_swap_requests WHERE id=? AND session_id=? AND requester_user_id=?",
        (swap_id, session_id, user["id"]),
    ).fetchone()
    if not swap or swap["status"] != "PENDING":
        conn.rollback()
        return session_action_response(
            session_id,
            "That swap request is no longer pending or cannot be cancelled.",
            category="error",
            status=409,
        )

    conn.execute(
        "UPDATE duty_swap_requests SET status='CANCELLED', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (swap_id,),
    )
    audit("swap.cancel", "swap_request", swap_id, {"session_id": session_id, "user_id": user["id"]})
    conn.commit()
    return session_action_response(session_id, "Swap request cancelled.")
