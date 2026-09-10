"""Session-wide picking pause support.

The old per-participant deferral table is retained for database/history
compatibility, but it no longer affects turn order. Picking can instead be
frozen for an entire session while preserving the current turn.
"""

import hashlib
import json
import secrets
import sqlite3

import core
import live_updates
from core import DB_PATH, configure_connection


def _ensure_picking_pause_column():
    conn = configure_connection(sqlite3.connect(DB_PATH, timeout=10))
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(draft_sessions)").fetchall()
    }
    if "picking_paused" not in columns:
        conn.execute(
            "ALTER TABLE draft_sessions ADD COLUMN picking_paused INTEGER NOT NULL "
            "DEFAULT 0 CHECK(picking_paused IN (0,1))"
        )
        conn.commit()
    if "order_edit_token" not in columns:
        conn.execute("ALTER TABLE draft_sessions ADD COLUMN order_edit_token TEXT")
        conn.commit()
    # 0: weekday phase, 1: awaiting confirmation, 2: weekend order confirmed.
    if "phase_order_state" not in columns:
        conn.execute(
            "ALTER TABLE draft_sessions ADD COLUMN phase_order_state INTEGER NOT NULL "
            "DEFAULT 0 CHECK(phase_order_state IN (0,1,2))"
        )
        conn.commit()
    conn.close()


_ensure_picking_pause_column()


def pause_for_phase_confirmation(session_id):
    """Gate weekday-to-weekend picking inside the caller's write transaction.

    Confirmation is once per session. Reopening a weekday after confirmation
    does not reverse the order a second time.
    """
    row = core.session_row(session_id)
    if (not row or row["status"] != "OPEN"
            or row["date_order"] != core.DATE_ORDER_WEEKDAYS_FIRST
            or row["phase_order_state"] != 0):
        return False
    counts = core.assignment_counts(session_id)
    capacities = core.capacities_for(row)
    kinds = core.date_kinds_for(row)
    weekdays = [d for d, capacity in capacities.items()
                if capacity > 0 and kinds[d] == core.DATE_KIND_WEEKDAY]
    weekdays_full = weekdays and all(counts.get(d, 0) >= capacities[d] for d in weekdays)
    weekends_open = any(capacity > counts.get(d, 0) and kinds[d] == core.DATE_KIND_WEEKEND
                        for d, capacity in capacities.items())
    if not weekdays_full or not weekends_open:
        return False
    core.db().execute(
        "UPDATE draft_sessions SET picking_paused=1,phase_order_state=1,order_edit_token=? WHERE id=?",
        (secrets.token_urlsafe(32), session_id),
    )
    core.audit("draft.phase.awaiting_confirmation", "session", session_id,
               {"completed_phase": "WEEKDAY", "next_phase": "WEEKEND"})
    return True


def ordered_people(session_id):
    return core.db().execute(
        "SELECT u.id,u.name,u.email,u.role,u.disabled,u.picture_url,o.position,"
        "(SELECT COUNT(*) FROM assignments a "
        " WHERE a.session_id=o.session_id AND a.user_id=o.user_id) AS assignment_count,"
        "0 AS deferred "
        "FROM session_order o JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? ORDER BY o.position",
        (session_id,),
    ).fetchall()


def next_picker(session_id):
    row = core.session_row(session_id)
    if not row or core.session_complete(row):
        return None

    active = core.db().execute(
        "SELECT u.*,o.position FROM session_order o "
        "JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? AND u.disabled=0 ORDER BY o.position",
        (session_id,),
    ).fetchall()
    if not active:
        return None

    start_position = row["current_position"] or 1
    rotated = [item for item in active if item["position"] >= start_position]
    rotated.extend(item for item in active if item["position"] < start_position)
    for participant in rotated:
        if core.selectable_dates(row, participant["id"]):
            return participant
    return None


def advance_turn(session_id, after_user_id):
    current = core.db().execute(
        "SELECT position FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, after_user_id),
    ).fetchone()
    if not current:
        raise ValueError("User is not in the session order.")

    active = core.db().execute(
        "SELECT o.position FROM session_order o "
        "JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? AND u.disabled=0 ORDER BY o.position",
        (session_id,),
    ).fetchall()
    if not active:
        return

    next_position = next(
        (
            item["position"]
            for item in active
            if item["position"] > current["position"]
        ),
        active[0]["position"],
    )
    core.db().execute(
        "UPDATE draft_sessions SET current_position=? WHERE id=?",
        (next_position, session_id),
    )


_base_dashboard_state_version = live_updates.dashboard_state_version
_base_session_state_version = live_updates.session_state_version


def _status_digest(base, payload):
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{base}:{serialized}".encode("utf-8")).hexdigest()


def dashboard_state_version(user):
    """Make account enable/disable changes invalidate participant dashboards."""

    base = _base_dashboard_state_version(user)
    if user["role"] == "ADMIN":
        rows = core.db().execute(
            "SELECT id,disabled FROM users ORDER BY id"
        ).fetchall()
    elif user["role"] == "HRA" and user["building_id"] is not None:
        rows = core.db().execute(
            "SELECT id,disabled FROM users WHERE building_id=? ORDER BY id",
            (user["building_id"],),
        ).fetchall()
    else:
        rows = []
    return _status_digest(base, [[row["id"], row["disabled"]] for row in rows])


def session_state_version(row, viewer):
    """Include picking pause and participant account status in live state."""

    base = _base_session_state_version(row, viewer)
    participants = core.db().execute(
        "SELECT u.id,u.disabled FROM session_order o "
        "JOIN users u ON u.id=o.user_id WHERE o.session_id=? ORDER BY o.position",
        (row["id"],),
    ).fetchall()
    return _status_digest(
        base,
        {
            "picking_paused": int(bool(row["picking_paused"])),
            "order_edit_token": row["order_edit_token"],
            "phase_order_state": row["phase_order_state"],
            "participant_status": [
                [item["id"], item["disabled"]] for item in participants
            ],
        },
    )


# Install the non-deferral turn implementation before route modules import
# these helpers from core. Existing session_deferrals rows become inert history.
core.ordered_people = ordered_people
core.next_picker = next_picker
core.advance_turn = advance_turn
live_updates.dashboard_state_version = dashboard_state_version
live_updates.session_state_version = session_state_version
