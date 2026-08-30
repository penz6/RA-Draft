"""Real-time SSE event broker and state hashing for reactive multi-client sync."""

import hashlib
import json
import queue
import secrets
import sqlite3
import threading
import time

from core import (
    DATE_ORDER_WEEKDAYS_FIRST,
    DATE_ORDER_WEEKENDS_FIRST,
    app,
    capacities_for,
    dates_for,
    date_kinds_for,
    db,
    effective_capacity,
    effective_date_kind,
    is_participant,
    selectable_dates,
    session_row,
)


class EventBroker:
    """Thread-safe multi-subscriber broadcast broker for Server-Sent Events."""

    def __init__(self):
        self.listeners = []
        self.lock = threading.Lock()

    def listen(self):
        """Register a new listener and return its thread-safe queue."""
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.listeners.append(q)
        return q

    def remove(self, q):
        """Unregister an existing listener queue."""
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def publish(self, event_data):
        """Broadcast an event payload to all active listener queues."""
        msg = f"data: {json.dumps(event_data)}\n\n"
        with self.lock:
            to_remove = []
            for q in self.listeners:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    to_remove.append(q)
            for q in to_remove:
                if q in self.listeners:
                    self.listeners.remove(q)


broker = EventBroker()


def notify_session_changed(session_id):
    """Publish a real-time SSE notification that session state has changed."""
    broker.publish(
        {
            "type": "session_update",
            "session_id": session_id,
            "ts": time.time(),
        }
    )


def notify_dashboard_changed():
    """Publish a real-time SSE notification that global dashboard state has changed."""
    broker.publish(
        {
            "type": "dashboard_update",
            "ts": time.time(),
        }
    )


def compute_current_picker_fast(session_id, capacity, date_order, picking_paused, current_position):
    """Compute the active turn participant using memory-cached participants and assignments."""
    if picking_paused:
        return None

    conn = db()
    active_rows = conn.execute(
        "SELECT u.id, u.name, u.email, u.role, u.building_id, o.position "
        "FROM session_order o "
        "JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? AND u.disabled=0 "
        "ORDER BY o.position",
        (session_id,),
    ).fetchall()
    if not active_rows:
        return None

    start_pos = current_position or 1
    rotated = [r for r in active_rows if r["position"] >= start_pos]
    rotated.extend(r for r in active_rows if r["position"] < start_pos)

    dates = dates_for(session_id)
    if not dates:
        return None

    assignments = conn.execute(
        "SELECT user_id, duty_date FROM assignments WHERE session_id=?",
        (session_id,),
    ).fetchall()

    capacities = capacities_for(session_id)
    date_kinds = date_kinds_for(session_id)

    counts = {d: 0 for d in dates}
    user_picks = {r["id"]: set() for r in active_rows}
    for a in assignments:
        u_id = a["user_id"]
        d_date = a["duty_date"]
        if d_date in counts:
            counts[d_date] += 1
        if u_id in user_picks:
            user_picks[u_id].add(d_date)

    is_complete = all(counts.get(d, 0) >= capacities.get(d, capacity) for d in dates)
    if is_complete:
        return None

    # Precompute phase
    weekdays_open = any(
        date_kinds.get(d) == "WEEKDAY" and counts.get(d, 0) < capacities.get(d, capacity)
        for d in dates
    )
    weekends_open = any(
        date_kinds.get(d) == "WEEKEND" and counts.get(d, 0) < capacities.get(d, capacity)
        for d in dates
    )

    for participant in rotated:
        p_id = participant["id"]
        p_picks = user_picks[p_id]
        has_pick = False

        for d in dates:
            cap = capacities.get(d, capacity)
            if cap <= 0 or counts[d] >= cap or d in p_picks:
                continue

            k = date_kinds.get(d)
            if k == "NO_DUTY":
                continue

            if date_order == DATE_ORDER_WEEKDAYS_FIRST:
                if weekdays_open and k != "WEEKDAY":
                    continue
            elif date_order == DATE_ORDER_WEEKENDS_FIRST:
                if weekends_open and k != "WEEKEND":
                    continue

            has_pick = True
            break

        if has_pick:
            return participant

    return None


def session_state_version(session_dict, user_dict):
    """Compute a deterministic hash representing the exact live view of a session for a user."""
    s_id = session_dict["id"]
    conn = db()

    # Query last audit timestamp or ID for session
    last_audit = conn.execute(
        "SELECT max(id) as max_id FROM audit_log WHERE target_id=? OR actor_user_id=?",
        (s_id, user_dict["id"]),
    ).fetchone()

    # Query assignment count and max assignment ID
    assign_info = conn.execute(
        "SELECT count(*) as total, max(id) as max_id FROM assignments WHERE session_id=?",
        (s_id,),
    ).fetchone()

    # Query swap status
    swap_info = conn.execute(
        "SELECT count(*) as total, max(id) as max_id FROM duty_swap_requests WHERE session_id=?",
        (s_id,),
    ).fetchone()

    # Query current position and status
    s_meta = conn.execute(
        "SELECT status, picking_paused, current_position FROM draft_sessions WHERE id=?",
        (s_id,),
    ).fetchone()

    components = [
        str(s_id),
        str(user_dict["id"]),
        str(user_dict["role"]),
        str(s_meta["status"] if s_meta else ""),
        str(s_meta["picking_paused"] if s_meta else ""),
        str(s_meta["current_position"] if s_meta else ""),
        str(assign_info["total"] if assign_info else 0),
        str(assign_info["max_id"] if assign_info else 0),
        str(swap_info["total"] if swap_info else 0),
        str(swap_info["max_id"] if swap_info else 0),
        str(last_audit["max_id"] if last_audit else 0),
    ]

    raw = ":".join(components)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def dashboard_state_version(user_dict):
    """Compute a deterministic hash representing the live view of the dashboard for a user."""
    conn = db()
    u_id = user_dict["id"]
    role = user_dict["role"]
    b_id = user_dict["building_id"]

    if role == "ADMIN":
        session_info = conn.execute(
            "SELECT count(*) total, max(id) max_id FROM draft_sessions"
        ).fetchone()
        user_info = conn.execute(
            "SELECT count(*) total, max(id) max_id FROM users"
        ).fetchone()
    else:
        session_info = conn.execute(
            "SELECT count(*) total, max(id) max_id FROM draft_sessions WHERE building_id=?",
            (b_id,),
        ).fetchone()
        user_info = conn.execute(
            "SELECT count(*) total, max(id) max_id FROM users WHERE building_id=?",
            (b_id,),
        ).fetchone()

    my_shifts_info = conn.execute(
        "SELECT count(*) total, max(id) max_id FROM assignments WHERE user_id=?",
        (u_id,),
    ).fetchone()

    raw = f"{u_id}:{role}:{b_id}:{session_info['total']}:{session_info['max_id']}:{user_info['total']}:{user_info['max_id']}:{my_shifts_info['total']}:{my_shifts_info['max_id']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
