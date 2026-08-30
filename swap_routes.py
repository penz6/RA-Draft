"""Duty swap routes with two-stage approval: target RA → building HRA."""

import secrets
from flask import abort, flash, redirect, render_template, request, url_for

from core import (
    app,
    audit,
    can_manage,
    can_view_session,
    current_user,
    db,
    hra_pending_swaps,
    login_required,
    require_csrf,
    session_row,
    session_swap_requests,
    swap_batch_details,
)


def _swap_action_response(session_id, message, *, category="success", status=200):
    """Return JSON for async swap actions, otherwise redirect to the swap page."""
    if request.headers.get("X-RA-Draft-Async") == "1":
        return {
            "ok": category != "error",
            "message": message,
        }, status

    flash(message, category)
    return redirect(url_for("swap_page", session_id=session_id))


@app.route("/swaps/session/<int:session_id>")
@login_required
def swap_page(session_id):
    """Render the swap management page for a closed session."""
    user = current_user()
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)
    if row["status"] != "CLOSED":
        flash("Duty swaps are only available after a session is closed.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    conn = db()
    conn.execute("BEGIN")
    try:
        # All assignments in this session
        picks = conn.execute(
            "SELECT a.*,u.name,u.role,o.position FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
            "WHERE a.session_id=? ORDER BY a.duty_date,o.position,a.id",
            (session_id,),
        ).fetchall()

        # My assignments for this session
        my_picks = [p for p in picks if p["user_id"] == user["id"]]

        # Other participants' assignments (for swap target selection)
        other_picks = [p for p in picks if p["user_id"] != user["id"]]

        # Other participants in same building (for partner selection)
        other_participants = conn.execute(
            "SELECT DISTINCT u.id, u.name FROM session_order o "
            "JOIN users u ON u.id=o.user_id "
            "WHERE o.session_id=? AND u.id<>? AND u.building_id=? "
            "ORDER BY u.name",
            (session_id, user["id"], row["building_id"]),
        ).fetchall()

        # All swap requests for this session
        swaps = session_swap_requests(session_id)

        # Group swaps by batch_id
        batches = {}
        for s in swaps:
            bid = s["batch_id"] or str(s["id"])
            if bid not in batches:
                batches[bid] = {
                    "batch_id": bid,
                    "status": s["status"],
                    "requester_user_id": s["requester_user_id"],
                    "requester_name": s["requester_name"],
                    "target_user_id": s["target_user_id"],
                    "target_name": s["target_name"],
                    "created_at": s["created_at"],
                    "reviewed_by": s["reviewed_by"],
                    "reviewer_name": s["reviewer_name"],
                    "pairs": [],
                }
            batches[bid]["pairs"].append({
                "id": s["id"],
                "requester_date": s["requester_date"],
                "target_date": s["target_date"],
            })

        batch_list = list(batches.values())

        # Incoming requests (I'm the target, status=PENDING)
        incoming = [b for b in batch_list if b["target_user_id"] == user["id"] and b["status"] == "PENDING"]

        # Outgoing requests (I'm the requester)
        outgoing = [b for b in batch_list if b["requester_user_id"] == user["id"]]

        # HRA review panel (TARGET_APPROVED swaps for this building)
        manager_allowed = can_manage(user, row)
        hra_review = [b for b in batch_list if b["status"] == "TARGET_APPROVED"] if manager_allowed else []

        # Completed history
        history = [
            b for b in batch_list
            if b["status"] in ("APPROVED", "REJECTED", "CANCELLED")
            and b["requester_user_id"] != user["id"]
            and b["target_user_id"] != user["id"]
        ]

        page = render_template(
            "swap_page.html",
            me=user,
            draft=row,
            my_picks=my_picks,
            other_picks=other_picks,
            other_participants=other_participants,
            incoming=incoming,
            outgoing=outgoing,
            hra_review=hra_review,
            history=history,
            can_manage=manager_allowed,
        )
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return page


@app.route("/swaps/session/<int:session_id>/request", methods=["POST"])
@login_required
def request_swap_batch(session_id):
    """Submit a batch of swap pairs (same two people, multiple dates)."""
    require_csrf()
    user = current_user()
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)
    if row["status"] != "CLOSED":
        return _swap_action_response(
            session_id, "Swaps are only available after the session is closed.",
            category="error", status=400,
        )

    # Parse pairs: my_assignment_ids[] and target_assignment_ids[] arrays
    my_ids = request.form.getlist("my_assignment_ids")
    target_ids = request.form.getlist("target_assignment_ids")

    if not my_ids or not target_ids or len(my_ids) != len(target_ids):
        return _swap_action_response(
            session_id, "Invalid swap request. Select at least one pair of dates.",
            category="error", status=400,
        )

    try:
        pairs = [(int(m), int(t)) for m, t in zip(my_ids, target_ids)]
    except (TypeError, ValueError):
        abort(400)

    if not pairs:
        return _swap_action_response(
            session_id, "Select at least one date to swap.",
            category="error", status=400,
        )

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    # Validate all pairs
    target_user_id = None
    for my_aid, target_aid in pairs:
        if my_aid == target_aid:
            conn.rollback()
            return _swap_action_response(
                session_id, "Cannot swap an assignment with itself.",
                category="error", status=400,
            )

        my_assign = conn.execute(
            "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
            (my_aid, session_id, user["id"]),
        ).fetchone()
        if not my_assign:
            conn.rollback()
            return _swap_action_response(
                session_id, "One of your selected assignments is not valid.",
                category="error", status=400,
            )

        target_assign = conn.execute(
            "SELECT a.*, u.building_id FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "WHERE a.id=? AND a.session_id=?",
            (target_aid, session_id),
        ).fetchone()
        if not target_assign or target_assign["user_id"] == user["id"]:
            conn.rollback()
            return _swap_action_response(
                session_id, "Target assignment must belong to a different participant.",
                category="error", status=400,
            )

        # Enforce same building
        if target_assign["building_id"] != row["building_id"]:
            conn.rollback()
            return _swap_action_response(
                session_id, "Swaps can only be made with someone in the same building.",
                category="error", status=400,
            )

        # Enforce all pairs target the same person
        if target_user_id is None:
            target_user_id = target_assign["user_id"]
        elif target_assign["user_id"] != target_user_id:
            conn.rollback()
            return _swap_action_response(
                session_id, "All swap pairs in a batch must be with the same person.",
                category="error", status=400,
            )

        # Check no existing pending/target_approved swap for these assignments
        existing = conn.execute(
            "SELECT 1 FROM duty_swap_requests WHERE session_id=? "
            "AND status IN ('PENDING','TARGET_APPROVED') "
            "AND (requester_assignment_id IN (?, ?) OR target_assignment_id IN (?, ?))",
            (session_id, my_aid, target_aid, my_aid, target_aid),
        ).fetchone()
        if existing:
            conn.rollback()
            return _swap_action_response(
                session_id, "A pending swap already exists for one of these dates.",
                category="error", status=409,
            )

    # Create the batch
    batch_id = secrets.token_urlsafe(16)
    for my_aid, target_aid in pairs:
        cur = conn.execute(
            "INSERT INTO duty_swap_requests("
            "session_id, requester_user_id, requester_assignment_id, "
            "target_user_id, target_assignment_id, status, batch_id"
            ") VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
            (session_id, user["id"], my_aid, target_user_id, target_aid, batch_id),
        )
        audit(
            "swap.request",
            "swap_request",
            cur.lastrowid,
            {
                "session_id": session_id,
                "batch_id": batch_id,
                "requester_user_id": user["id"],
                "requester_assignment_id": my_aid,
                "target_user_id": target_user_id,
                "target_assignment_id": target_aid,
            },
        )

    conn.commit()
    count = len(pairs)
    return _swap_action_response(
        session_id,
        f"Swap request submitted ({count} date{'s' if count > 1 else ''}). "
        f"Waiting for the other RA to review.",
    )


@app.route("/swaps/batch/<batch_id>/target-review", methods=["POST"])
@login_required
def target_review_swap(batch_id):
    """Target RA approves or rejects the entire swap batch."""
    require_csrf()
    action = request.form.get("action", "").strip().upper()
    if action not in ("APPROVE", "REJECT"):
        abort(400)

    user = current_user()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    rows = conn.execute(
        "SELECT * FROM duty_swap_requests WHERE batch_id=?",
        (batch_id,),
    ).fetchall()
    if not rows:
        conn.rollback()
        abort(404)

    session_id = rows[0]["session_id"]

    # Verify the current user is the target
    for row in rows:
        if row["target_user_id"] != user["id"]:
            conn.rollback()
            abort(403)
        if row["status"] != "PENDING":
            conn.rollback()
            return _swap_action_response(
                session_id, "This swap request is no longer pending.",
                category="error", status=409,
            )

    new_status = "TARGET_APPROVED" if action == "APPROVE" else "REJECTED"
    conn.execute(
        "UPDATE duty_swap_requests SET status=?, target_reviewed_at=CURRENT_TIMESTAMP WHERE batch_id=?",
        (new_status, batch_id),
    )

    audit_action = "swap.target_approve" if action == "APPROVE" else "swap.target_reject"
    audit(
        audit_action,
        "swap_request",
        rows[0]["id"],
        {"batch_id": batch_id, "session_id": session_id, "target_user_id": user["id"]},
    )
    conn.commit()

    if action == "APPROVE":
        return _swap_action_response(
            session_id,
            "Swap approved. It has been sent to your building's HRA for final approval.",
        )
    return _swap_action_response(session_id, "Swap request rejected.")


@app.route("/swaps/batch/<batch_id>/hra-review", methods=["POST"])
@login_required
def hra_review_swap(batch_id):
    """Building HRA or Admin approves or rejects a TARGET_APPROVED swap batch."""
    require_csrf()
    action = request.form.get("action", "").strip().upper()
    if action not in ("APPROVE", "REJECT"):
        abort(400)

    manager = current_user()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    rows = conn.execute(
        "SELECT sr.*, s.building_id FROM duty_swap_requests sr "
        "JOIN draft_sessions s ON s.id=sr.session_id "
        "WHERE sr.batch_id=?",
        (batch_id,),
    ).fetchall()
    if not rows:
        conn.rollback()
        abort(404)

    session_id = rows[0]["session_id"]
    session = session_row(session_id)

    # Verify manager permissions
    if not session or not can_manage(manager, session):
        conn.rollback()
        abort(403)

    for row in rows:
        if row["status"] != "TARGET_APPROVED":
            conn.rollback()
            return _swap_action_response(
                session_id, "This swap batch is not ready for HRA review.",
                category="error", status=409,
            )

    if action == "REJECT":
        conn.execute(
            "UPDATE duty_swap_requests SET status='REJECTED', reviewed_by=?, "
            "reviewed_at=CURRENT_TIMESTAMP WHERE batch_id=?",
            (manager["id"], batch_id),
        )
        audit(
            "swap.hra_reject",
            "swap_request",
            rows[0]["id"],
            {"batch_id": batch_id, "session_id": session_id, "reviewed_by": manager["id"]},
        )
        conn.commit()
        return _swap_action_response(session_id, "Swap batch rejected.")

    # Approve: swap all assignment user_ids
    for row in rows:
        req_assign = conn.execute(
            "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
            (row["requester_assignment_id"], session_id, row["requester_user_id"]),
        ).fetchone()
        target_assign = conn.execute(
            "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
            (row["target_assignment_id"], session_id, row["target_user_id"]),
        ).fetchone()

        if not req_assign or not target_assign:
            conn.rollback()
            return _swap_action_response(
                session_id,
                "One or both assignments have changed. Swap cannot be approved.",
                category="error", status=409,
            )

        conn.execute(
            "UPDATE assignments SET user_id=? WHERE id=?",
            (row["target_user_id"], row["requester_assignment_id"]),
        )
        conn.execute(
            "UPDATE assignments SET user_id=? WHERE id=?",
            (row["requester_user_id"], row["target_assignment_id"]),
        )

    conn.execute(
        "UPDATE duty_swap_requests SET status='APPROVED', reviewed_by=?, "
        "reviewed_at=CURRENT_TIMESTAMP WHERE batch_id=?",
        (manager["id"], batch_id),
    )
    audit(
        "swap.hra_approve",
        "swap_request",
        rows[0]["id"],
        {"batch_id": batch_id, "session_id": session_id, "reviewed_by": manager["id"]},
    )
    conn.commit()
    return _swap_action_response(session_id, "Swap approved. The schedule has been updated.")


@app.route("/swaps/batch/<batch_id>/cancel", methods=["POST"])
@login_required
def cancel_swap_batch(batch_id):
    """Requester cancels their own PENDING or TARGET_APPROVED swap batch."""
    require_csrf()
    user = current_user()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    rows = conn.execute(
        "SELECT * FROM duty_swap_requests WHERE batch_id=?",
        (batch_id,),
    ).fetchall()
    if not rows:
        conn.rollback()
        abort(404)

    session_id = rows[0]["session_id"]

    for row in rows:
        if row["requester_user_id"] != user["id"]:
            conn.rollback()
            abort(403)
        if row["status"] not in ("PENDING", "TARGET_APPROVED"):
            conn.rollback()
            return _swap_action_response(
                session_id, "This swap request can no longer be cancelled.",
                category="error", status=409,
            )

    conn.execute(
        "UPDATE duty_swap_requests SET status='CANCELLED', reviewed_at=CURRENT_TIMESTAMP WHERE batch_id=?",
        (batch_id,),
    )
    audit(
        "swap.cancel",
        "swap_request",
        rows[0]["id"],
        {"batch_id": batch_id, "session_id": session_id, "user_id": user["id"]},
    )
    conn.commit()
    return _swap_action_response(session_id, "Swap request cancelled.")
