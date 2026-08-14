import calendar as calendar_module
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from werkzeug.exceptions import SecurityError
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "ra_draft.db"))
ALLOWED_EMAIL_DOMAINS = {"g.rwu.edu", "rwu.edu"}
ALLOWED_HOSTED_DOMAINS = {"g.rwu.edu", "rwu.edu"}
ADMIN_EMAILS = {
    item.strip().lower()
    for item in os.environ.get("ADMIN_EMAILS", "").split(",")
    if item.strip()
}

DATE_ORDER_WEEKDAYS_FIRST = "WEEKDAYS_FIRST"
DATE_ORDER_CHRONOLOGICAL = "CHRONOLOGICAL"
DATE_ORDER_WEEKENDS_FIRST = "WEEKENDS_FIRST"
DATE_ORDER_CHOICES = {
    DATE_ORDER_WEEKDAYS_FIRST,
    DATE_ORDER_CHRONOLOGICAL,
    DATE_ORDER_WEEKENDS_FIRST,
}
DATE_ORDER_LABELS = {
    DATE_ORDER_WEEKDAYS_FIRST: "Weekdays first",
    DATE_ORDER_CHRONOLOGICAL: "Any open date",
    DATE_ORDER_WEEKENDS_FIRST: "Weekends first",
}

SECRET_KEY = os.environ.get("SECRET_KEY", "")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip().lower()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

if len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY must be set to at least 32 characters.")
if not re.fullmatch(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", PUBLIC_HOST):
    raise RuntimeError(
        "PUBLIC_HOST must be the public hostname only, for example duty.example.edu."
    )
if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise RuntimeError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must both be set.")

try:
    PROXY_HOPS = int(os.environ.get("PROXY_HOPS", "1"))
except ValueError as exc:
    raise RuntimeError("PROXY_HOPS must be 0, 1, or 2.") from exc
if PROXY_HOPS not in (0, 1, 2):
    raise RuntimeError("PROXY_HOPS must be 0, 1, or 2.")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_NAME="ra_draft_session",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    SESSION_REFRESH_EACH_REQUEST=False,
    TRUSTED_HOSTS=[PUBLIC_HOST],
    PREFERRED_URL_SCHEME="https",
    MAX_CONTENT_LENGTH=1024 * 1024,
    MAX_FORM_MEMORY_SIZE=256 * 1024,
    MAX_FORM_PARTS=500,
)

# Trust only the explicitly configured Pangolin/reverse-proxy hops. The app
# port must not be exposed directly to the Internet when this is non-zero.
if PROXY_HOPS:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_proto=PROXY_HOPS,
        x_host=PROXY_HOPS,
    )

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

ASSIGNMENTS_TABLE_SQL = """
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  duty_date TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(session_id, user_id, duty_date)
)
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  google_sub TEXT UNIQUE NOT NULL,
  email TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'RA' CHECK(role IN ('RA','HRA','ADMIN')),
  building_id INTEGER REFERENCES buildings(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS draft_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  building_id INTEGER NOT NULL REFERENCES buildings(id),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  shift_start TEXT NOT NULL DEFAULT '19:00',
  shift_end TEXT NOT NULL DEFAULT '07:00',
  capacity INTEGER NOT NULL DEFAULT 2,
  date_order TEXT NOT NULL DEFAULT 'WEEKDAYS_FIRST'
    CHECK(date_order IN ('WEEKDAYS_FIRST','CHRONOLOGICAL','WEEKENDS_FIRST')),
  current_position INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS session_order (
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  position INTEGER NOT NULL,
  PRIMARY KEY(session_id, user_id),
  UNIQUE(session_id, position)
);
CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  duty_date TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(session_id, user_id, duty_date)
);
CREATE TABLE IF NOT EXISTS session_deferrals (
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  deferred_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(session_id, user_id)
);
CREATE TABLE IF NOT EXISTS session_date_capacities (
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  duty_date TEXT NOT NULL,
  capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 50),
  updated_by INTEGER NOT NULL REFERENCES users(id),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(session_id, duty_date)
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_user_id INTEGER REFERENCES users(id),
  action TEXT NOT NULL,
  target_type TEXT,
  target_id INTEGER,
  details TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def configure_connection(conn):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA trusted_schema = OFF")
    return conn


def db():
    if "db" not in g:
        g.db = configure_connection(sqlite3.connect(DB_PATH, timeout=10))
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def _assignments_use_legacy_unique_constraint(conn):
    for index_row in conn.execute("PRAGMA index_list(assignments)").fetchall():
        if not index_row["unique"]:
            continue
        columns = [
            item["name"]
            for item in conn.execute(
                "SELECT name FROM pragma_index_info(?)",
                (index_row["name"],),
            ).fetchall()
        ]
        if columns == ["session_id", "user_id"]:
            return True
    return False


def _migrate_assignments_for_multiple_picks(conn):
    if not _assignments_use_legacy_unique_constraint(conn):
        return

    conn.execute("ALTER TABLE assignments RENAME TO assignments_legacy")
    conn.execute(ASSIGNMENTS_TABLE_SQL)
    conn.execute(
        "INSERT INTO assignments(id,session_id,user_id,duty_date,created_by,created_at) "
        "SELECT id,session_id,user_id,duty_date,created_by,created_at "
        "FROM assignments_legacy"
    )
    conn.execute("DROP TABLE assignments_legacy")


def _initialize_existing_turn_positions(conn):
    sessions = conn.execute("SELECT id FROM draft_sessions").fetchall()
    for session_item in sessions:
        session_id = session_item["id"]
        maximum = conn.execute(
            "SELECT MAX(position) AS position FROM session_order WHERE session_id=?",
            (session_id,),
        ).fetchone()["position"]
        if not maximum:
            continue
        latest = conn.execute(
            "SELECT o.position FROM assignments a "
            "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
            "WHERE a.session_id=? ORDER BY a.id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        position = 1 if not latest else (latest["position"] % maximum) + 1
        conn.execute(
            "UPDATE draft_sessions SET current_position=? WHERE id=?",
            (position, session_id),
        )


def _normalize_existing_capacities(conn):
    sessions = conn.execute("SELECT id,capacity FROM draft_sessions").fetchall()
    for session_item in sessions:
        session_id = session_item["id"]
        participants = conn.execute(
            "SELECT COUNT(*) AS n FROM session_order WHERE session_id=?",
            (session_id,),
        ).fetchone()["n"]
        if not participants:
            continue
        if session_item["capacity"] > participants:
            conn.execute(
                "UPDATE draft_sessions SET capacity=? WHERE id=?",
                (participants, session_id),
            )
        conn.execute(
            "UPDATE session_date_capacities SET capacity=? "
            "WHERE session_id=? AND capacity>?",
            (participants, session_id, participants),
        )


def migrate_schema(conn):
    session_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(draft_sessions)")
    }
    if "date_order" not in session_columns:
        conn.execute(
            "ALTER TABLE draft_sessions ADD COLUMN date_order TEXT NOT NULL "
            "DEFAULT 'WEEKDAYS_FIRST' "
            "CHECK(date_order IN ('WEEKDAYS_FIRST','CHRONOLOGICAL','WEEKENDS_FIRST'))"
        )
    added_turn_position = "current_position" not in session_columns
    if added_turn_position:
        conn.execute(
            "ALTER TABLE draft_sessions ADD COLUMN current_position INTEGER NOT NULL DEFAULT 1"
        )

    _migrate_assignments_for_multiple_picks(conn)
    _normalize_existing_capacities(conn)
    if added_turn_position:
        _initialize_existing_turn_positions(conn)


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = configure_connection(sqlite3.connect(DB_PATH, timeout=10))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    migrate_schema(conn)
    conn.commit()
    conn.close()


init_db()


@app.after_request
def security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "img-src 'self' data:; "
        "style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if not request.path.startswith("/static/") and request.path != "/healthz":
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.errorhandler(SecurityError)
def invalid_host(_error):
    return (
        "Invalid request host.",
        400,
        {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"},
    )


@app.errorhandler(400)
def bad_request(_error):
    return render_template(
        "error.html",
        status=400,
        title="Invalid request",
        message="The request could not be processed. Return to the portal and try again.",
    ), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template(
        "error.html",
        status=403,
        title="Access denied",
        message="You do not have permission to perform this action.",
    ), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template(
        "error.html",
        status=404,
        title="Page not found",
        message="The requested page or duty session does not exist.",
    ), 404


@app.errorhandler(413)
def request_too_large(_error):
    return render_template(
        "error.html",
        status=413,
        title="Request too large",
        message="The submitted form was larger than the application allows.",
    ), 413


def clean_single_line(value, *, min_length=1, max_length=120):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not min_length <= len(text) <= max_length:
        raise ValueError("Text length is outside the allowed range.")
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError("Control characters are not allowed.")
    return text


def safe_display_name(value, fallback):
    raw = unicodedata.normalize("NFKC", str(value or ""))
    without_controls = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in raw
    )
    cleaned = " ".join(without_controls.split())[:120]
    return cleaned or fallback


def normalize_time(value):
    text = str(value or "")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("Time must use 24-hour HH:MM format.")
    return text


def normalize_date_order(value):
    text = str(value or DATE_ORDER_WEEKDAYS_FIRST).strip().upper()
    if text not in DATE_ORDER_CHOICES:
        raise ValueError("Unsupported date selection mode.")
    return text


def csrf_token():
    session.setdefault("csrf", secrets.token_hex(32))
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.template_filter("date_label")
def date_label(value):
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return value
    return f"{parsed.strftime('%a, %b')} {parsed.day}, {parsed.year}"


@app.template_filter("time_label")
def time_label(value):
    try:
        parsed = datetime.strptime(str(value), "%H:%M")
    except ValueError:
        return value
    return parsed.strftime("%I:%M %p").lstrip("0")


@app.template_filter("day_type")
def day_type(value):
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return "Date"
    return "Weekend" if parsed.weekday() >= 5 else "Weekday"


@app.template_filter("date_order_label")
def date_order_label(value):
    return DATE_ORDER_LABELS.get(str(value), DATE_ORDER_LABELS[DATE_ORDER_WEEKDAYS_FIRST])


def require_csrf():
    supplied = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        abort(400)


def current_user():
    uid = session.get("uid")
    if not isinstance(uid, int):
        if uid is not None:
            session.clear()
        return None
    user = db().execute(
        "SELECT users.*, buildings.name AS building_name FROM users "
        "LEFT JOIN buildings ON buildings.id=users.building_id WHERE users.id=?",
        (uid,),
    ).fetchone()
    if user is None:
        session.clear()
    return user


@app.context_processor
def inject_user():
    return {"me": current_user()}


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapped


def roles(*allowed):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user["role"] not in allowed:
                abort(403)
            return fn(*args, **kwargs)

        return wrapped

    return deco


def allowed_email(email):
    parts = email.lower().rsplit("@", 1)
    return len(parts) == 2 and parts[1] in ALLOWED_EMAIL_DOMAINS


def google_identity_allowed(info):
    email = (info.get("email") or "").strip().lower()
    hosted_domain = (info.get("hd") or "").strip().lower()
    subject = str(info.get("sub") or "").strip()
    return bool(
        subject
        and len(subject) <= 255
        and subject.isascii()
        and info.get("email_verified") is True
        and allowed_email(email)
        and hosted_domain in ALLOWED_HOSTED_DOMAINS
    )


def audit(action, target_type=None, target_id=None, details=None, actor_user_id=None):
    actor = actor_user_id if actor_user_id is not None else session.get("uid")
    serialized = None
    if details is not None:
        serialized = json.dumps(details, sort_keys=True, separators=(",", ":"))
    db().execute(
        "INSERT INTO audit_log(actor_user_id,action,target_type,target_id,details) "
        "VALUES(?,?,?,?,?)",
        (actor, action, target_type, target_id, serialized),
    )


def session_row(session_id):
    return db().execute(
        "SELECT s.*, b.name building_name, u.name creator_name FROM draft_sessions s "
        "JOIN buildings b ON b.id=s.building_id "
        "JOIN users u ON u.id=s.created_by WHERE s.id=?",
        (session_id,),
    ).fetchone()


def can_view_session(user, row):
    return bool(user and (user["role"] == "ADMIN" or user["building_id"] == row["building_id"]))


def can_manage(user, row):
    return bool(
        user
        and (
            user["role"] == "ADMIN"
            or (user["role"] == "HRA" and user["building_id"] == row["building_id"])
        )
    )


def ordered_people(session_id):
    return db().execute(
        "SELECT u.id,u.name,u.email,u.role,o.position,"
        "(SELECT COUNT(*) FROM assignments a "
        " WHERE a.session_id=o.session_id AND a.user_id=o.user_id) AS assignment_count,"
        "CASE WHEN EXISTS(SELECT 1 FROM session_deferrals d "
        " WHERE d.session_id=o.session_id AND d.user_id=o.user_id) THEN 1 ELSE 0 END "
        "AS deferred "
        "FROM session_order o JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? ORDER BY o.position",
        (session_id,),
    ).fetchall()


def participant_count(session_id):
    return db().execute(
        "SELECT COUNT(*) AS n FROM session_order WHERE session_id=?",
        (session_id,),
    ).fetchone()["n"]


def is_participant(session_id, user_id):
    return bool(
        db().execute(
            "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
    )


def calendar_dates(row):
    start = date.fromisoformat(row["start_date"])
    end = date.fromisoformat(row["end_date"])
    result = []
    day = start
    while day <= end:
        result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def dates_for(row):
    days = [date.fromisoformat(value) for value in calendar_dates(row)]
    try:
        order = normalize_date_order(row["date_order"])
    except (IndexError, KeyError, TypeError, ValueError):
        order = DATE_ORDER_WEEKDAYS_FIRST

    if order == DATE_ORDER_WEEKDAYS_FIRST:
        days.sort(key=lambda item: (item.weekday() >= 5, item))
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        days.sort(key=lambda item: (item.weekday() < 5, item))
    return [item.isoformat() for item in days]


def calendar_months(row):
    start = date.fromisoformat(row["start_date"])
    end = date.fromisoformat(row["end_date"])
    calendar = calendar_module.Calendar(firstweekday=0)
    months = []
    cursor = start.replace(day=1)
    while cursor <= end:
        weeks = []
        for week in calendar.monthdatescalendar(cursor.year, cursor.month):
            rendered_week = []
            for day in week:
                if day.month != cursor.month or day < start or day > end:
                    rendered_week.append(None)
                else:
                    rendered_week.append(day.isoformat())
            weeks.append(rendered_week)
        months.append(
            {
                "label": f"{calendar_module.month_name[cursor.month]} {cursor.year}",
                "weeks": weeks,
            }
        )
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return months


def capacity_overrides(session_id):
    return {
        row["duty_date"]: row["capacity"]
        for row in db().execute(
            "SELECT duty_date,capacity FROM session_date_capacities "
            "WHERE session_id=? ORDER BY duty_date",
            (session_id,),
        ).fetchall()
    }


def effective_capacity(row, duty_date):
    override = db().execute(
        "SELECT capacity FROM session_date_capacities "
        "WHERE session_id=? AND duty_date=?",
        (row["id"], duty_date),
    ).fetchone()
    return override["capacity"] if override else row["capacity"]


def capacities_for(row):
    overrides = capacity_overrides(row["id"])
    return {
        duty_date: overrides.get(duty_date, row["capacity"])
        for duty_date in calendar_dates(row)
    }


def assignment_counts(session_id):
    return {
        row["duty_date"]: row["n"]
        for row in db().execute(
            "SELECT duty_date,COUNT(*) AS n FROM assignments "
            "WHERE session_id=? GROUP BY duty_date",
            (session_id,),
        ).fetchall()
    }


def total_slots(row):
    return sum(capacities_for(row).values())


def filled_slots(session_id):
    return db().execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE session_id=?",
        (session_id,),
    ).fetchone()["n"]


def session_complete(row):
    counts = assignment_counts(row["id"])
    capacities = capacities_for(row)
    return all(counts.get(duty_date, 0) >= capacity for duty_date, capacity in capacities.items())


def user_assignment_dates(session_id, user_id):
    return {
        row["duty_date"]
        for row in db().execute(
            "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        ).fetchall()
    }


def selectable_dates(row, user_id):
    counts = assignment_counts(row["id"])
    capacities = capacities_for(row)
    already_assigned = user_assignment_dates(row["id"], user_id)
    open_dates = [
        duty_date
        for duty_date in dates_for(row)
        if counts.get(duty_date, 0) < capacities[duty_date]
        and duty_date not in already_assigned
    ]

    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        weekdays = [
            duty_date
            for duty_date in open_dates
            if date.fromisoformat(duty_date).weekday() < 5
        ]
        return weekdays or open_dates
    if order == DATE_ORDER_WEEKENDS_FIRST:
        weekends = [
            duty_date
            for duty_date in open_dates
            if date.fromisoformat(duty_date).weekday() >= 5
        ]
        return weekends or open_dates
    return open_dates


def selection_phase_label(row):
    counts = assignment_counts(row["id"])
    capacities = capacities_for(row)
    open_dates = [
        duty_date
        for duty_date in calendar_dates(row)
        if counts.get(duty_date, 0) < capacities[duty_date]
    ]
    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        if any(date.fromisoformat(value).weekday() < 5 for value in open_dates):
            return "Weekday dates are open; weekends unlock after weekday slots fill."
        return "Weekend dates are now open."
    if order == DATE_ORDER_WEEKENDS_FIRST:
        if any(date.fromisoformat(value).weekday() >= 5 for value in open_dates):
            return "Weekend dates are open; weekdays unlock after weekend slots fill."
        return "Weekday dates are now open."
    return "Any date with an open slot can be selected."


def next_picker(session_id):
    row = session_row(session_id)
    if not row or session_complete(row):
        return None

    active = db().execute(
        "SELECT u.*,o.position FROM session_order o "
        "JOIN users u ON u.id=o.user_id "
        "LEFT JOIN session_deferrals d "
        "ON d.session_id=o.session_id AND d.user_id=o.user_id "
        "WHERE o.session_id=? AND d.user_id IS NULL ORDER BY o.position",
        (session_id,),
    ).fetchall()
    if not active:
        return None

    start_position = row["current_position"] or 1
    rotated = [item for item in active if item["position"] >= start_position]
    rotated.extend(item for item in active if item["position"] < start_position)
    for participant in rotated:
        if selectable_dates(row, participant["id"]):
            return participant
    return None


def advance_turn(session_id, after_user_id):
    current = db().execute(
        "SELECT position FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, after_user_id),
    ).fetchone()
    if not current:
        raise ValueError("User is not in the session order.")

    active = db().execute(
        "SELECT o.position FROM session_order o "
        "LEFT JOIN session_deferrals d "
        "ON d.session_id=o.session_id AND d.user_id=o.user_id "
        "WHERE o.session_id=? AND d.user_id IS NULL ORDER BY o.position",
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
    db().execute(
        "UPDATE draft_sessions SET current_position=? WHERE id=?",
        (next_position, session_id),
    )
