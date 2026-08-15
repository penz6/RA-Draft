"""Server-pushed updates for dashboards and active duty sessions."""

import hashlib
import json
import queue
import threading

from flask import Response, abort, request, stream_with_context

from core import (
    app,
    can_view_session,
    current_user,
    db,
    session_row,
)

SSE_HEARTBEAT_SECONDS = 15


class LiveEventBroker:
    """Fan out coalesced change signals to connected SSE clients.

    The production image intentionally runs one threaded Gunicorn worker so
    every request and event stream shares this in-process broker. Scaling to
    multiple app replicas would require a shared pub/sub service such as Redis.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = set()

    def subscribe(self):
        subscriber = queue.Queue(maxsize=1)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self):
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(True)
            except queue.Full:
                # One queued signal is sufficient because each client
                # recomputes its complete authorized state before refreshing.
                pass


live_event_broker = LiveEventBroker()


@app.after_request
def publish_successful_changes(response):
    """Wake connected clients after successful state-changing requests."""

    changed = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    # OAuth account creation/linking changes the available participant list but
    # arrives through a GET callback.
    changed = changed or request.endpoint == "auth_callback"
    if changed and response.status_code < 400:
        live_event_broker.publish()
    return response


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
    """Return a current version, or None when access has disappeared."""

    user = current_user()
    if not user:
        return None
    if session_id is None:
        return dashboard_state_version(user)

    row = session_row(session_id)
    if not row or not can_view_session(user, row):
        return None
    return session_state_version(row)


def _event(event_name, payload, *, retry=None):
    lines = []
    if retry is not None:
        lines.append(f"retry: {retry}")
    lines.append(f"event: {event_name}")
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    lines.extend(f"data: {line}" for line in serialized.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


@app.route("/live-state")
def live_state():
    """Return a version snapshot for diagnostics and fallback clients."""

    session_id = _parse_session_id()
    return {"version": _authorized_version(session_id)}


@app.route("/live-events")
def live_events():
    """Push authorized state changes to the browser with Server-Sent Events."""

    session_id = _parse_session_id()
    initial_version = _authorized_version(session_id)
    subscriber = live_event_broker.subscribe()

    @stream_with_context
    def generate():
        version = initial_version
        try:
            # This first event catches changes made after the page rendered but
            # before EventSource finished connecting.
            yield _event("state", {"version": version}, retry=1500)
            while True:
                try:
                    subscriber.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    # Keep Pangolin/Traefik and mobile networks from treating
                    # an otherwise idle stream as abandoned.
                    yield ": keep-alive\n\n"
                    continue

                refreshed_version = _stream_version(session_id)
                if refreshed_version is None:
                    yield _event("reload", {"reason": "access-changed"})
                    return
                if refreshed_version != version:
                    version = refreshed_version
                    yield _event("update", {"version": version})
        except GeneratorExit:
            return
        finally:
            live_event_broker.unsubscribe(subscriber)

    return Response(
        generate(),
        content_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )
