"""Session-wide picking pause support.

The old per-participant deferral table is retained for database/history
compatibility, but it no longer affects turn order. Picking can instead be
frozen for an entire session while preserving the current turn.
"""

import hashlib
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
    conn.close()


_ensure_picking_pause_column()


def ordered_people(session_id):
    return core.db().execute(
        "SELECT u.id,u.name,u.email,u.role,o.position,"
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
        "WHERE o.session_id=? ORDER BY o.position",
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
        "SELECT position FROM session_order WHERE session_id=? ORDER BY position",
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


_base_session_state_version = live_updates.session_state_version


def session_state_version(row, viewer):
    """Include the session-wide pause flag in the live-update fingerprint."""

    base = _base_session_state_version(row, viewer)
    payload = f"{base}:{int(bool(row['picking_paused']))}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()


# Install the non-deferral turn implementation before route modules import
# these helpers from core. Existing session_deferrals rows become inert history.
core.ordered_people = ordered_people
core.next_picker = next_picker
core.advance_turn = advance_turn
live_updates.session_state_version = session_state_version
