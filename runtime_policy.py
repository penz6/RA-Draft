"""School-specific runtime policy for authorization and personal schedules."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import flash, has_request_context, redirect, request, url_for

import core

# The deployment serves one school in the US Eastern time zone. Using the
# geographic zone keeps the date correct across both EST and EDT.
SCHOOL_TIMEZONE = ZoneInfo("America/New_York")


def school_today():
    """Return the current calendar date at the school."""
    return datetime.now(SCHOOL_TIMEZONE).date()


_base_roles = core.roles


def roles(*allowed):
    """Treat Admin as satisfying any HRA-only route permission."""
    expanded = list(allowed)
    if "HRA" in allowed and "ADMIN" not in allowed:
        expanded.append("ADMIN")
    return _base_roles(*expanded)


core.roles = roles


_base_can_manage = core.can_manage


def can_manage(user, row):
    """Preserve manager permissions and reject a raced manual swap after reopen."""
    if not _base_can_manage(user, row):
        return False
    if (
        has_request_context()
        and request.endpoint == "manager_manual_swap"
        and core.db().in_transaction
        and row
        and row["status"] != "CLOSED"
    ):
        return False
    return True


core.can_manage = can_manage


def user_upcoming_shifts(user_id):
    """Return upcoming duty shifts using the school's Eastern calendar date."""
    today_str = school_today().isoformat()
    rows = core.db().execute(
        "SELECT a.id AS assignment_id, a.duty_date, s.id AS session_id, "
        "s.name AS session_name, s.status AS session_status, "
        "s.shift_start, s.shift_end, b.name AS building_name "
        "FROM assignments a "
        "JOIN draft_sessions s ON s.id=a.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "WHERE a.user_id=? AND a.duty_date >= ? "
        "ORDER BY a.duty_date ASC, s.id ASC",
        (user_id, today_str),
    ).fetchall()

    shifts = []
    for row in rows:
        partners = core.db().execute(
            "SELECT u.name FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "WHERE a.session_id=? AND a.duty_date=? AND a.user_id<>? "
            "ORDER BY a.id",
            (row["session_id"], row["duty_date"], user_id),
        ).fetchall()
        shifts.append(
            {
                "assignment_id": row["assignment_id"],
                "duty_date": row["duty_date"],
                "session_id": row["session_id"],
                "session_name": row["session_name"],
                "session_status": row["session_status"],
                "shift_start": row["shift_start"],
                "shift_end": row["shift_end"],
                "building_name": row["building_name"],
                "partner_names": [partner["name"] for partner in partners],
            }
        )
    return shifts


core.user_upcoming_shifts = user_upcoming_shifts


def _past_swap_response(session_id):
    """Return the normal swap-page error response for a past-duty attempt."""
    message = "Past duty shifts cannot be swapped. Choose a duty date that is today or later."
    if request.headers.get("X-RA-Draft-Async") == "1":
        return {"ok": False, "message": message}, 409
    flash(message, "error")
    return redirect(url_for("swap_page", session_id=session_id))


def _assignment_ids_from_form(*field_names):
    ids = []
    for field_name in field_names:
        for raw in request.form.getlist(field_name):
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
    return ids


def _contains_past_assignment(assignment_ids, *, session_id=None):
    if not assignment_ids:
        return False
    today = school_today().isoformat()
    for assignment_id in assignment_ids:
        if session_id is None:
            row = core.db().execute(
                "SELECT duty_date FROM assignments WHERE id=?",
                (assignment_id,),
            ).fetchone()
        else:
            row = core.db().execute(
                "SELECT duty_date FROM assignments WHERE id=? AND session_id=?",
                (assignment_id, session_id),
            ).fetchone()
        if row and row["duty_date"] < today:
            return True
    return False


def _batch_past_state(batch_id):
    rows = core.db().execute(
        "SELECT sr.session_id,sr.target_user_id,"
        "req_a.duty_date AS requester_date,target_a.duty_date AS target_date "
        "FROM duty_swap_requests sr "
        "JOIN assignments req_a ON req_a.id=sr.requester_assignment_id "
        "JOIN assignments target_a ON target_a.id=sr.target_assignment_id "
        "WHERE sr.batch_id=?",
        (batch_id,),
    ).fetchall()
    if not rows:
        return None, False, rows
    today = school_today().isoformat()
    has_past = any(
        row["requester_date"] < today or row["target_date"] < today
        for row in rows
    )
    return rows[0]["session_id"], has_past, rows


@core.app.before_request
def block_past_duty_swap_actions():
    """Prevent any approval or execution of a swap involving an elapsed duty date."""
    if request.method != "POST":
        return None

    endpoint = request.endpoint
    if endpoint == "request_swap_batch":
        session_id = (request.view_args or {}).get("session_id")
        user = core.current_user()
        row = core.session_row(session_id) if session_id is not None else None
        if not user or not row or not core.can_view_session(user, row):
            return None
        assignment_ids = _assignment_ids_from_form(
            "my_assignment_ids", "target_assignment_ids"
        )
        if _contains_past_assignment(assignment_ids, session_id=session_id):
            core.require_csrf()
            return _past_swap_response(session_id)

    if endpoint == "manager_manual_swap":
        session_id = (request.view_args or {}).get("session_id")
        manager = core.current_user()
        row = core.session_row(session_id) if session_id is not None else None
        if not manager or not row or not core.can_manage(manager, row):
            return None
        assignment_ids = _assignment_ids_from_form(
            "first_assignment_id", "second_assignment_id"
        )
        if _contains_past_assignment(assignment_ids, session_id=session_id):
            core.require_csrf()
            return _past_swap_response(session_id)

    if endpoint in {"target_review_swap", "hra_review_swap"}:
        if request.form.get("action", "").strip().upper() != "APPROVE":
            return None
        batch_id = (request.view_args or {}).get("batch_id")
        if not batch_id:
            return None
        session_id, has_past, rows = _batch_past_state(batch_id)
        if not rows or not has_past:
            return None

        user = core.current_user()
        if not user:
            return None
        if endpoint == "target_review_swap":
            if any(row["target_user_id"] != user["id"] for row in rows):
                return None
        else:
            session_row = core.session_row(session_id)
            if not session_row or not core.can_manage(user, session_row):
                return None

        core.require_csrf()
        return _past_swap_response(session_id)

    return None
