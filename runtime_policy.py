"""School-specific runtime policy for authorization and personal schedules."""

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import has_request_context, request

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
