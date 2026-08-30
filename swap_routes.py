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


def _swap_date_collision(conn, session_id, requester_user_id, target_user_id, validated_pairs):
    """Return True if the final batch would leave either RA assigned twice on one date."""
    requester_dates = {
        row["duty_date"]
        for row in conn.execute(
            "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=?",
            (session_id, requester_user_id),
        ).fetchall()
    }
    target_dates = {
        row["duty_date"]
        for row in conn.execute(
            "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=?",
            (session_id, target_user_id),
        ).fetchall()
    }

    requester_outgoing = set()
    target_outgoing = set()
    requester_incoming = []
    target_incoming = []

    for requester_assignment, target_assignment in validated_pairs:
        requester_date = requester_assignment["duty_date"]
        target_date = target_assignment["duty_date"]
        if requester_date == target_date:
            return True
        requester_outgoing.add(requester_date)
        target_outgoing.add(target_date)
        requester_incoming.append(target_date)
        target_incoming.append(requester_date)

    projected_requester_dates = [
        duty_date for duty_date in requester_dates if duty_date not in requester_outgoing
    ] + requester_incoming
    if len(projected_requester_dates) != len(set(projected_requester_dates)):
        return True

    projected_target_dates = [
        duty_date for duty_date in target_dates if duty_date not in target_outgoing
    ] + target_incoming
    return len(projected_target_dates) != len(set(projected_target_dates))


@app.route("/swaps")
@login_required
def swap_home():
    """Show the dedicated duty-swap menu for closed sessions the user may access."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))

    conn = db()
    conn.execute("BEGIN")
    try:
        if user["role"] == "ADMIN":
            closed_sessions = conn.execute(
                "SELECT s.*,b.name building_name FROM draft_sessions s "
                "JOIN buildings b ON b.id=s.building_id "
                "WHERE s.status='CLOSED' ORDER BY s.created_at DESC"
            ).fetchall()
        elif user["building_id"]:
            closed_sessions = conn.execute(
                "SELECT s.*,b.name building_name FROM draft_sessions s "
                "JOIN buildings b ON b.id=s.building_id "
                "WHERE s.status='CLOSED' AND s.building_id=? "
                "ORDER BY s.created_at DESC",
                (user["building_id"],),
            ).fetchall()
        else:
            closed_sessions = []

        page = render_template(
            "swap_home.html",
            me=user,
            closed_sessions=closed_sessions,
        )
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return page


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
        picks = conn.execute(
            "SELECT a.*,u.name,u.role,o.position FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
            "WHERE a.session_id=? ORDER BY a.duty_date,o.position,a.id",
            (session_id,),
        ).fetchall()

        my_picks = [p for p in picks if p["user_id"] == user["id"]]
        other_picks = [p for p in picks if p["user_id"] != user["id"]]

        other_participants = conn.execute(
            "SELECT DISTINCT u.id, u.name FROM session_order o "
            "JOIN users u ON u.id=o.user_id "
            "WHERE o.session_id=? AND u.id<>? AND u.building_id=? "
            "ORDER BY u.name",
            (session_id, user["id"], row["building_id"]),
        ).fetchall()

        swaps = session_swap_requests(session_id)

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
        incoming = [b for b in batch_list if b["target_user_id"] == user["id"] and b["status"] == "PENDING"]
        outgoing = [b for b in batch_list if b["requester_user_id"] == user["id"]]
        manager_allowed = can_manage(user, row)
        hra_review = [b for b in batch_list if b["status"] == "TARGET_APPROVED"] if manager_allowed else []
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

    if len({m for m, _ in pairs}) != len(pairs) or len({t for _, t in pairs}) != len(pairs):
        return _swap_action_response(
            session_id, "Each assignment can only appear once in a swap request.",
            category="error", status=400,
        )

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    target_user_id = None
    validated_pairs = []
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

        if my_assign["duty_date"] == target_assign["duty_date"]:
            conn.rollback()
            return _swap_action_response(
                session_id, "You cannot swap two assignments that are already on the same duty date.",
                category="error", status=400,
            )

        if target_assign["building_id"] != row["building_id"]:
            conn.rollback()
            return _swap_action_response(
                session_id, "Swaps can only be made with someone in the same building.",
                category="error", status=400,
            )

        if target_user_id is None:
            target_user_id = target_assign["user_id"]
        elif target_assign["user_id"] != target_user_id:
            conn.rollback()
            return _swap_action_response(
                session_id, "All swap pairs in a batch must be with the same person.",
                category="error", status=400,
            )

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

        validated_pairs.append((my_assign, target_assign))

    if _swap_date_collision(conn, session_id, user["id"], target_user_id, validated_pairs):
        conn.rollback()
        return _swap_action_response(
            session_id,
            "This swap would leave one of you assigned twice on the same duty date. Choose a different combination of shifts.",
            category="error",
            status=400,
        )

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

    resolved_pairs = []
    requester_user_id = rows[0]["requester_user_id"]
    target_user_id = rows[0]["target_user_id"]
    seen_requester_assignments = set()
    seen_target_assignments = set()

    for row in rows:
        if row["requester_user_id"] != requester_user_id or row["target_user_id"] != target_user_id:
            conn.rollback()
            return _swap_action_response(
                session_id, "This swap batch is inconsistent and cannot be approved.",
                category="error", status=409,
            )
        if row["requester_assignment_id"] in seen_requester_assignments or row["target_assignment_id"] in seen_target_assignments:
            conn.rollback()
            return _swap_action_response(
                session_id, "This swap batch repeats an assignment and cannot be approved.",
                category="error", status=409,
            )
        seen_requester_assignments.add(row["requester_assignment_id"])
        seen_target_assignments.add(row["target_assignment_id"])

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
        if req_assign["duty_date"] == target_assign["duty_date"]:
            conn.rollback()
            return _swap_action_response(
                session_id,
                "The swap now contains two assignments on the same duty date and cannot be approved.",
                category="error", status=409,
            )
        resolved_pairs.append((req_assign, target_assign))

    if _swap_date_collision(conn, session_id, requester_user_id, target_user_id, resolved_pairs):
        conn.rollback()
        return _swap_action_response(
            session_id,
            "The schedule changed and this swap would now leave one of the RAs assigned twice on the same duty date.",
            category="error",
            status=409,
        )

    # Temporarily move selected assignments onto unique holding dates so a
    # multi-pair swap can exchange overlapping dates without tripping the
    # immediate UNIQUE(session_id, user_id, duty_date) constraint mid-update.
    held_dates = {}
    for requester_assignment, target_assignment in resolved_pairs:
        held_dates[requester_assignment["id"]] = requester_assignment["duty_date"]
        held_dates[target_assignment["id"]] = target_assignment["duty_date"]

    for assignment_id in held_dates:
        conn.execute(
            "UPDATE assignments SET duty_date=? WHERE id=?",
            (f"__swap_hold__{batch_id}_{assignment_id}", assignment_id),
        )

    for row in rows:
        conn.execute(
            "UPDATE assignments SET user_id=? WHERE id=?",
            (row["target_user_id"], row["requester_assignment_id"]),
        )
        conn.execute(
            "UPDATE assignments SET user_id=? WHERE id=?",
            (row["requester_user_id"], row["target_assignment_id"]),
        )

    for assignment_id, duty_date in held_dates.items():
        conn.execute(
            "UPDATE assignments SET duty_date=? WHERE id=?",
            (duty_date, assignment_id),
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