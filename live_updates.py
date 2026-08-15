"""Live update endpoints for dashboards and duty sessions.

The JSON endpoint remains as a compatibility fallback. The primary endpoint is
an authenticated Server-Sent Events stream. It watches SQLite's data version
and pushes a new scoped state version whenever relevant data changes.
"""

import hashlib
import json
import time

from flask import Response, abort, request, stream_with_context

from core import (
    app,
    can_view_session,
    current_user,
    db,
    session_row,
)

SSE_CHECK_INTERVAL_SECONDS = 0.75
SSE_HEARTBEAT_SECONDS = 15
SSE_MAX_CONNECTION_SECONDS = 300


def _digest(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rows(query, parameters, columns):
    return [
        [row[column] for column in columns]
        for row in db().execute(query, parameters).fetchall()
    ]


def dashboard_state_version(user):
    columns = (
        "id",
        "name",
        "building_id",
        "start_date",
        "end_date",
        "capacity",
        "date_order",
        "current_position",
        "status",
        "created_at",
    )
    if user["role"] == "ADMIN":
        sessions = _rows(
            "SELECT id,name,building_id,start_date,end_date,capacity,date_order,"
            "current_position,status,created_at FROM draft_sessions ORDER BY id",
            (),
            columns,
        )
    elif user["building_id"] is not None:
        sessions = _rows(
            "SELECT id,name,building_id,start_date,end_date,capacity,date_order,"
            "current_position,status,created_at FROM draft_sessions "
            "WHERE building_id=? ORDER BY id",
            (user["building_id"],),
            columns,
        )
    else:
        sessions = []

    return _digest(
        {
            "user": [user["id"], user["role"], user["building_id"]],
            "sessions": sessions,
        }
    )


def session_state_version(row):
    session_id = row["id"]
    return _digest(
        {
            "session": [
                row["id"],
                row["name"],
                row["building_id"],
                row["start_date"],
                row["end_date"],
                row["capacity"],
                row["date_order"],
                row["current_position"],
                row["status"],
            ],
            "order": _rows(
                "SELECT user_id,position FROM session_order "
                "WHERE session_id=? ORDER BY position",
                (session_id,),
                ("user_id", "position"),
            ),
            "assignments": _rows(
                "SELECT id,user_id,duty_date,created_by,created_at FROM assignments "
                "WHERE session_id=? ORDER BY id",
                (session_id,),
                ("id", "user_id", "duty_date", "created_by", "created_at"),
            ),
            "deferrals": _rows(
                "SELECT user_id,deferred_by,created_at FROM session_deferrals "
                "WHERE session_id=? ORDER BY user_id",
                (session_id,),
                ("user_id", "deferred_by", "created_at"),
            ),
            "capacities": _rows(
                "SELECT duty_date,capacity,updated_by,updated_at "
                "FROM session_date_capacities WHERE session_id=? ORDER BY duty_date",
                (session_id,),
                ("duty_date", "capacity", "updated_by", "updated_at"),
            ),
            "date_treatments": _rows(
                "SELECT duty_date,date_kind,updated_by,updated_at "
                "FROM session_date_overrides WHERE session_id=? ORDER BY duty_date",
                (session_id,),
                ("duty_date", "date_kind", "updated_by", "updated_at"),
            ),
        }
    )


def _parse_session_id():
    raw_session_id = request.args.get("session_id")
    if raw_session_id is None:
        return None
    try:
        return int(raw_session_id)
    except (TypeError, ValueError):
        abort(400)


def _authorized_version(session_id):
    user = current_user()
    if not user:
        abort(401)
    if session_id is None:
        return dashboard_state_version(user)

    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)
    return session_state_version(row)


def _stream_version(session_id):
    """Return a version for an existing stream, or None if access disappeared."""
    user = current_user()
    if not user:
        return None
    if session_id is None:
        return dashboard_state_version(user)

    row = session_row(session_id)
    if not row or not can_view_session(user, row):
        return None
    return session_state_version(row)


def _event(event_name, payload, event_id=None):
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    lines.extend(f"data: {line}" for line in serialized.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


@app.route("/live-state")
def live_state():
    session_id = _parse_session_id()
    return {"version": _authorized_version(session_id)}


@app.route("/live-events")
def live_events():
    session_id = _parse_session_id()
    initial_version = _authorized_version(session_id)

    @stream_with_context
    def generate():
        last_version = initial_version
        last_database_version = db().execute("PRAGMA data_version").fetchone()[0]
        started_at = time.monotonic()
        last_heartbeat = started_at

        yield "retry: 1500\n\n"
        yield _event("state", {"version": last_version}, last_version)

        while time.monotonic() - started_at < SSE_MAX_CONNECTION_SECONDS:
            time.sleep(SSE_CHECK_INTERVAL_SECONDS)
            now = time.monotonic()
            database_version = db().execute("PRAGMA data_version").fetchone()[0]

            if database_version != last_database_version:
                last_database_version = database_version
                version = _stream_version(session_id)
                if version is None:
                    yield _event("reload", {"reason": "access-changed"})
                    return
                if version != last_version:
                    last_version = version
                    yield _event("update", {"version": version}, version)
                    last_heartbeat = now
                    continue

            if now - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
                yield ": keep-alive\n\n"
                last_heartbeat = now

        # EventSource reconnects automatically. Reconnecting periodically also
        # revalidates the user's session and authorization.
        yield _event("reconnect", {"version": last_version}, last_version)

    return Response(
        generate(),
        content_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )
