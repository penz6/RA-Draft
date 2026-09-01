"""Manager-facing pending duty-swap counts and landing-page queue."""

from flask import request

from core import app, current_user, db


def _pending_swap_approvals(user):
    if not user or user["role"] not in {"HRA", "ADMIN"}:
        return []

    params = []
    scope_sql = ""
    if user["role"] == "HRA":
        if not user["building_id"]:
            return []
        scope_sql = "AND s.building_id=? "
        params.append(user["building_id"])

    return db().execute(
        "SELECT COALESCE(sr.batch_id, printf('legacy-%d', sr.id)) AS batch_key, "
        "sr.session_id, s.name AS session_name, b.name AS building_name, "
        "requester.name AS requester_name, target.name AS target_name, "
        "COUNT(*) AS pair_count, MIN(sr.created_at) AS created_at "
        "FROM duty_swap_requests sr "
        "JOIN draft_sessions s ON s.id=sr.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "JOIN users requester ON requester.id=sr.requester_user_id "
        "JOIN users target ON target.id=sr.target_user_id "
        "WHERE sr.status='TARGET_APPROVED' AND s.status='CLOSED' "
        + scope_sql
        + "GROUP BY batch_key, sr.session_id, s.name, b.name, "
        "requester.name, target.name "
        "ORDER BY MIN(sr.created_at) ASC, sr.session_id ASC",
        tuple(params),
    ).fetchall()


@app.context_processor
def inject_pending_swap_approval_ui():
    """Expose a manager-scoped nav count and full queue on the Duty Swaps page."""
    user = current_user()
    pending = _pending_swap_approvals(user)
    return {
        "pending_swap_approval_count": len(pending),
        "pending_swap_approvals": pending if request.endpoint == "swap_home" else [],
    }
