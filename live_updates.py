"""Scoped, server-pushed updates for dashboards and active duty sessions."""

import hashlib
import json
import queue
import threading
import time

from flask import Response, abort, g, request, session, stream_with_context

from core import app, can_view_session, current_user, db, session_row

SSE_HEARTBEAT_SECONDS = 15
SSE_MAX_CONNECTION_SECONDS = 300
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _topic(namespace, identifier):
    return f"{namespace}:{int(identifier)}"


def topics_for_session(session_id):
    return {_topic("session", session_id)}


def topics_for_logout(user_id):
    return {_topic("logout", user_id)}


class LiveSubscriber:
    """A topic-filtered, coalescing notification queue for one SSE client."""

    def __init__(self, topics=None):
        self.topics = frozenset(topics or {"*"})
        self._condition = threading.Condition()
        self._pending = set()

    def notify(self, topics):
        relevant = set(topics) if "*" in self.topics else self.topics.intersection(topics)
        if not relevant:
            return
        with self._condition:
            self._pending.update(relevant)
            self._condition.notify()

    def get(self, timeout=None):
        with self._condition:
            ready = self._condition.wait_for(lambda: bool(self._pending), timeout=timeout)
            if not ready:
                raise queue.Empty
            topics = frozenset(self._pending)
            self._pending.clear()
            return topics


class LiveEventBroker:
    """Fan out scoped invalidation signals to connected SSE clients."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = set()

    def subscribe(self, topics=None):
        subscriber = LiveSubscriber(topics)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, topics):
        normalized = frozenset(str(topic) for topic in topics if str(topic))
        if not normalized:
            return
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.notify(normalized)


live_event_broker = LiveEventBroker()


def publish_live_topics(*topics):
    normalized = set()
    for topic in topics:
        if isinstance(topic, (set, frozenset, list, tuple)):
            normalized.update(str(item) for item in topic if str(item))
        elif topic:
            normalized.add(str(topic))
    live_event_broker.publish(normalized)


def _topics_for_committed_request():
    endpoint = request.endpoint or ""
    view_args = request.view_args or {}
    topics = set()
    raw_session_id = view_args.get("session_id")
    if raw_session_id is not None:
        try:
            topics.add(_topic("session", raw_session_id))
        except (TypeError, ValueError):
            pass

    if endpoint == "logout":
        user_id = getattr(g, "live_request_user_id", None)
        if user_id is not None:
            topics.add(_topic("logout", user_id))
        return topics

    if endpoint == "create_session":
        topics.add("dashboard:all")
    elif endpoint in {"session_status", "update_date_order", "delete_session"}:
        topics.add("dashboard:all")
    elif endpoint in {"request_swap_batch", "target_review_swap", "hra_review_swap", "cancel_swap_batch"}:
        raw_session_id = view_args.get("session_id")
        if raw_session_id is not None:
            try:
                topics.add(_topic("session", raw_session_id))
            except (TypeError, ValueError):
                pass
        # For batch routes, find the session from the batch
        batch_id = view_args.get("batch_id")
        if batch_id is not None:
            try:
                from core import db as get_db
                swap_row = get_db().execute(
                    "SELECT session_id FROM duty_swap_requests WHERE batch_id=? LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if swap_row:
                    topics.add(_topic("session", swap_row["session_id"]))
            except Exception:
                pass
        topics.add("dashboard:all")
    elif endpoint.startswith("admin") or endpoint in {
        "add_building",
        "rename_building",
        "delete_building",
        "add_user",
        "edit_user",
        "delete_user",
        "auth_callback",
        "onboarding",
    }:
        topics.update({"dashboard:all", "session:all"})
    elif not topics:
        topics.update({"dashboard:all", "session:all"})

    return topics


@app.before_request
def track_live_commit_boundary():
    should_track = request.method in _MUTATING_METHODS or request.endpoint == "auth_callback"
    if not should_track:
        return

    user = current_user()
    g.live_request_user_id = user["id"] if user else session.get("uid")
    g.live_commits = 0
    connection = db()

    def trace(statement):
        command = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if command in {"COMMIT", "END"}:
            g.live_commits += 1

    connection.set_trace_callback(trace)


def _publish_committed_change(response):
    connection = g.get("db")
    if connection is not None:
        connection.set_trace_callback(None)
    # The COMMIT is the source of truth. If response construction fails after
    # the commit, clients still need to hear that the database changed.
    if getattr(g, "live_commits", 0):
        publish_live_topics(_topics_for_committed_request())
    return response


def _preserve_sse_headers(response):
    if response.mimetype == "text/event-stream":
        response.headers["Cache-Control"] = "private, no-cache, no-store, no-transform"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["Content-Encoding"] = "identity"
        response.vary.add("Cookie")
    return response


app.after_request_funcs.setdefault(None, []).insert(0, _preserve_sse_headers)
app.after_request_funcs.setdefault(None, []).insert(0, _publish_committed_change)


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
    session_columns = (
        "id",
        "name",
        "building_id",
        "building_name",
        "start_date",
        "end_date",
        "capacity",
        "date_order",
        "status",
        "created_at",
    )
    if user["role"] == "ADMIN":
        sessions = _rows(
            "SELECT s.id,s.name,s.building_id,b.name building_name,"
            "s.start_date,s.end_date,s.capacity,s.date_order,s.status,s.created_at "
            "FROM draft_sessions s JOIN buildings b ON b.id=s.building_id "
            "ORDER BY s.id",
            (),
            session_columns,
        )
    elif user["building_id"] is not None:
        sessions = _rows(
            "SELECT s.id,s.name,s.building_id,b.name building_name,"
            "s.start_date,s.end_date,s.capacity,s.date_order,s.status,s.created_at "
            "FROM draft_sessions s JOIN buildings b ON b.id=s.building_id "
            "WHERE s.building_id=? ORDER BY s.id",
            (user["building_id"],),
            session_columns,
        )
    else:
        sessions = []

    participant_columns = (
        "id",
        "name",
        "email",
        "role",
        "building_id",
        "building_name",
    )
    if user["role"] == "ADMIN":
        participants = _rows(
            "SELECT u.id,u.name,u.email,u.role,u.building_id,b.name building_name "
            "FROM users u JOIN buildings b ON b.id=u.building_id ORDER BY u.id",
            (),
            participant_columns,
        )
        buildings = _rows(
            "SELECT id,name FROM buildings ORDER BY id",
            (),
            ("id", "name"),
        )
    elif user["role"] == "HRA" and user["building_id"] is not None:
        participants = _rows(
            "SELECT u.id,u.name,u.email,u.role,u.building_id,b.name building_name "
            "FROM users u JOIN buildings b ON b.id=u.building_id "
            "WHERE u.building_id=? ORDER BY u.id",
            (user["building_id"],),
            participant_columns,
        )
        buildings = []
    else:
        participants = []
        buildings = []

    return _digest(
        {
            "viewer": [
                user["id"],
                user["name"],
                user["email"],
                user["role"],
                user["building_id"],
                user["building_name"],
            ],
            "sessions": sessions,
            "participants": participants,
            "buildings": buildings,
        }
    )


def session_state_version(row, viewer):
    """Return the viewer-aware session fingerprint used by HTML and SSE."""

    session_id = row["id"]
    people = _rows(
        "SELECT u.id,u.name,u.email,u.role,o.position,"
        "(SELECT COUNT(*) FROM assignments a "
        " WHERE a.session_id=o.session_id AND a.user_id=o.user_id) assignment_count,"
        "CASE WHEN EXISTS(SELECT 1 FROM session_deferrals d "
        " WHERE d.session_id=o.session_id AND d.user_id=o.user_id) THEN 1 ELSE 0 END deferred "
        "FROM session_order o JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? ORDER BY o.position",
        (session_id,),
        ("id", "name", "email", "role", "position", "assignment_count", "deferred"),
    )
    assignments = _rows(
        "SELECT a.id,a.user_id,u.name user_name,u.role user_role,"
        "a.duty_date,a.created_by,a.created_at FROM assignments a "
        "JOIN users u ON u.id=a.user_id WHERE a.session_id=? ORDER BY a.id",
        (session_id,),
        (
            "id",
            "user_id",
            "user_name",
            "user_role",
            "duty_date",
            "created_by",
            "created_at",
        ),
    )
    viewer_state = [
        viewer["id"],
        viewer["name"],
        viewer["email"],
        viewer["role"],
        viewer["building_id"],
        viewer["building_name"],
    ]

    return _digest(
        {
            "viewer": viewer_state,
            "session": [
                row["id"],
                row["name"],
                row["building_id"],
                row["building_name"],
                row["start_date"],
                row["end_date"],
                row["capacity"],
                row["date_order"],
                row["current_position"],
                row["status"],
                row["created_by"],
                row["creator_name"],
                row["created_at"],
            ],
            "people": people,
            "assignments": assignments,
            "capacities": _rows(
                "SELECT duty_date,capacity FROM session_date_capacities "
                "WHERE session_id=? ORDER BY duty_date",
                (session_id,),
                ("duty_date", "capacity"),
            ),
            "date_treatments": _rows(
                "SELECT duty_date,date_kind FROM session_date_overrides "
                "WHERE session_id=? ORDER BY duty_date",
                (session_id,),
                ("duty_date", "date_kind"),
            ),
        }
    )


def _read_snapshot(callback):
    """Run a live-state calculation on one SQLite read snapshot."""

    connection = db()
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        value = callback()
    except BaseException:
        if owns_transaction and connection.in_transaction:
            connection.rollback()
        raise
    if owns_transaction:
        connection.commit()
    return value


def _parse_session_id():
    raw_session_id = request.args.get("session_id")
    if raw_session_id is None:
        return None
    try:
        return int(raw_session_id)
    except (TypeError, ValueError):
        abort(400)


def _authorized_version(session_id):
    def calculate():
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
        return session_state_version(row, user)

    return _read_snapshot(calculate)


def _stream_version(session_id):
    def calculate():
        user = current_user()
        if not user:
            return None
        if session_id is None:
            return dashboard_state_version(user)

        row = session_row(session_id)
        if not row or not can_view_session(user, row):
            return None
        return session_state_version(row, user)

    return _read_snapshot(calculate)


def _subscription_topics(user, row):
    topics = {_topic("logout", user["id"])}
    if row is None:
        topics.add("dashboard:all")
    else:
        topics.update({"session:all", _topic("session", row["id"])})
    return topics


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
    session_id = _parse_session_id()
    return {"version": _authorized_version(session_id)}


@app.route("/live-events")
def live_events():
    session_id = _parse_session_id()
    user = current_user()
    if not user:
        abort(401)

    row = None
    if session_id is not None:
        row = session_row(session_id)
        if not row:
            abort(404)
        if not can_view_session(user, row):
            abort(403)

    subscriber = live_event_broker.subscribe(_subscription_topics(user, row))
    try:
        initial_version = _authorized_version(session_id)
    except Exception:
        live_event_broker.unsubscribe(subscriber)
        raise
    viewer_id = user["id"]

    @stream_with_context
    def generate():
        version = initial_version
        started_at = time.monotonic()
        try:
            yield _event("state", {"version": version}, retry=1500)
            while True:
                remaining = SSE_MAX_CONNECTION_SECONDS - (time.monotonic() - started_at)
                if remaining <= 0:
                    yield _event("reconnect", {"version": version})
                    return

                try:
                    changed_topics = subscriber.get(
                        timeout=min(SSE_HEARTBEAT_SECONDS, remaining)
                    )
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue

                if _topic("logout", viewer_id) in changed_topics:
                    yield _event("reload", {"reason": "signed-out"})
                    return

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

    response = Response(
        generate(),
        content_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "private, no-cache, no-store, no-transform",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )
    response.call_on_close(lambda: live_event_broker.unsubscribe(subscriber))
    return response
