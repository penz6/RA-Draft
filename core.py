import calendar as calendar_module
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
import urllib.parse

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

DATE_KIND_AUTO = "AUTO"
DATE_KIND_WEEKDAY = "WEEKDAY"
DATE_KIND_WEEKEND = "WEEKEND"
DATE_KIND_NO_DUTY = "NO_DUTY"
DATE_KIND_OVERRIDE_CHOICES = {
    DATE_KIND_WEEKDAY,
    DATE_KIND_WEEKEND,
    DATE_KIND_NO_DUTY,
}
DATE_KIND_FORM_CHOICES = DATE_KIND_OVERRIDE_CHOICES | {DATE_KIND_AUTO}
DATE_KIND_LABELS = {
    DATE_KIND_AUTO: "Calendar default",
    DATE_KIND_WEEKDAY: "Weekday",
    DATE_KIND_WEEKEND: "Weekend",
    DATE_KIND_NO_DUTY: "No one needed",
}


def _audit_log_max_rows():
    raw = os.environ.get("AUDIT_LOG_MAX_ROWS", "5000").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("AUDIT_LOG_MAX_ROWS must be an integer.") from exc
    if not 100 <= value <= 100000:
        raise RuntimeError("AUDIT_LOG_MAX_ROWS must be between 100 and 100000.")
    return value


AUDIT_LOG_MAX_ROWS = _audit_log_max_rows()

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
CREATE TABLE IF NOT EXISTS buildings (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  name TEXT UNIQUE NOT NULL\n);\nCREATE TABLE IF NOT EXISTS users (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  google_sub TEXT UNIQUE NOT NULL,\n  email TEXT UNIQUE NOT NULL,\n  name TEXT NOT NULL,\n  role TEXT NOT NULL DEFAULT 'RA' CHECK(role IN ('RA','HRA','ADMIN')),\n  building_id INTEGER REFERENCES buildings(id),\n  disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),\n  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\nCREATE TABLE IF NOT EXISTS draft_sessions (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  name TEXT NOT NULL,\n  building_id INTEGER NOT NULL REFERENCES buildings(id),\n  start_date TEXT NOT NULL,\n  end_date TEXT NOT NULL,\n  shift_start TEXT NOT NULL DEFAULT '19:00',\n  shift_end TEXT NOT NULL DEFAULT '07:00',\n  capacity INTEGER NOT NULL DEFAULT 2,\n  date_order TEXT NOT NULL DEFAULT 'WEEKDAYS_FIRST'\n    CHECK(date_order IN ('WEEKDAYS_FIRST','CHRONOLOGICAL','WEEKENDS_FIRST')),\n  current_position INTEGER NOT NULL DEFAULT 1,\n  picking_paused INTEGER NOT NULL DEFAULT 0 CHECK(picking_paused IN (0,1)),\n  created_by INTEGER NOT NULL REFERENCES users(id),\n  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED')),\n  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\nCREATE TABLE IF NOT EXISTS session_order (\n  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,\n  user_id INTEGER NOT NULL REFERENCES users(id),\n  position INTEGER NOT NULL,\n  PRIMARY KEY(session_id, user_id),\n  UNIQUE(session_id, position)\n);\nCREATE TABLE IF NOT EXISTS assignments (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,\n  user_id INTEGER NOT NULL REFERENCES users(id),\n  duty_date TEXT NOT NULL,\n  created_by INTEGER NOT NULL REFERENCES users(id),\n  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n  UNIQUE(session_id, user_id, duty_date)\n);\nCREATE TABLE IF NOT EXISTS session_deferrals (\n  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,\n  user_id INTEGER NOT NULL REFERENCES users(id),\n  deferred_by INTEGER NOT NULL REFERENCES users(id),\n  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n  PRIMARY KEY(session_id, user_id)\n);\nCREATE TABLE IF NOT EXISTS session_date_capacities (\n  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,\n  duty_date TEXT NOT NULL,\n  capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 50),\n  updated_by INTEGER NOT NULL REFERENCES users(id),\n  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n  PRIMARY KEY(session_id, duty_date)\n);\nCREATE TABLE IF NOT EXISTS session_date_overrides (\n  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,\n  duty_date TEXT NOT NULL,\n  date_kind TEXT NOT NULL\n    CHECK(date_kind IN ('WEEKDAY','WEEKEND','NO_DUTY')),\n  updated_by INTEGER NOT NULL REFERENCES users(id),\n  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n  PRIMARY KEY(session_id, duty_date)\n);\nCREATE TABLE IF NOT EXISTS duty_swap_requests (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,\n  requester_user_id INTEGER NOT NULL REFERENCES users(id),\n  requester_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,\n  target_user_id INTEGER NOT NULL REFERENCES users(id),\n  target_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,\n  status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','CANCELLED')),\n  reviewed_by INTEGER REFERENCES users(id),\n  reviewed_at TEXT,\n  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\nCREATE TABLE IF NOT EXISTS audit_log (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  actor_user_id INTEGER REFERENCES users(id),\n  action TEXT NOT NULL,\n  target_type TEXT,\n  target_id INTEGER,\n  details TEXT,\n  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\nCREATE INDEX IF NOT EXISTS idx_assignments_session_date ON assignments(session_id, duty_date);\nCREATE INDEX IF NOT EXISTS idx_users_building ON users(building_id);\nCREATE INDEX IF NOT EXISTS idx_sessions_building ON draft_sessions(building_id);\nCREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_user_id);\nCREATE INDEX IF NOT EXISTS idx_swaps_session_status ON duty_swap_requests(session_id, status);\n"""


def configure_connection(conn):
    """Configure SQLite connection pragma parameters and row factory."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA trusted_schema = OFF")
    return conn


def db():
    """Retrieve or create the per-request SQLite connection."""
    if "db" not in g:
        g.db = configure_connection(sqlite3.connect(DB_PATH, timeout=10))
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    """Close the active database connection at the end of the request context."""
    conn = g.pop("db", None)
    if conn:
        conn.close()


def _assignments_use_legacy_unique_constraint(conn):
    """Check if the assignments table still uses the legacy (session_id, user_id) constraint."""
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
    """Migrate assignments table to allow participants to select multiple duty dates per session."""
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
    """Initialize turn position pointer for pre-existing draft sessions."""
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
    """Ensure session capacity values do not exceed total registered participants."""
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


def _table_exists(conn, table_name):
    """Check if a database table exists in the sqlite catalog."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _ensure_performance_indexes(conn):
    """Ensure database query indexes exist for query optimization across all tables."""
    if _table_exists(conn, "audit_log"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at)")
    if _table_exists(conn, "assignments"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assignments_user_session ON assignments(user_id, session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assignments_session_date ON assignments(session_id, duty_date)")
    if _table_exists(conn, "session_order"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_order_lookup ON session_order(session_id, user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_order_position ON session_order(session_id, position)")
    if _table_exists(conn, "session_date_capacities"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_capacities_lookup ON session_date_capacities(session_id, duty_date)")
    if _table_exists(conn, "session_date_overrides"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_overrides_lookup ON session_date_overrides(session_id, duty_date)")
    if _table_exists(conn, "users"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_building ON users(building_id)")
    if _table_exists(conn, "draft_sessions"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_draft_sessions_building ON draft_sessions(building_id)")
    if _table_exists(conn, "duty_swap_requests"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_duty_swaps_session_status ON duty_swap_requests(session_id, status)")


def _install_audit_retention(conn):
    """Install SQLite trigger and prune old rows to enforce audit log row limit."""
    if not _table_exists(conn, "audit_log"):
        return
    offset = AUDIT_LOG_MAX_ROWS - 1
    conn.execute(
        "DELETE FROM audit_log WHERE id < ("
        "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?"
        ")",
        (offset,),
    )
    trigger_sql = f"""
        CREATE TRIGGER IF NOT EXISTS audit_log_retention
        AFTER INSERT ON audit_log
        BEGIN
          DELETE FROM audit_log
          WHERE id < (
            SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET {offset}
          );
        END;
    """
    conn.execute("DROP TRIGGER IF EXISTS audit_log_retention")
    conn.executescript(trigger_sql)


def migrate_schema(conn):
    """Apply schema migrations and table upgrades idempotently."""
    session_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(draft_sessions)")
    }
    if "date_order" not in session_columns:
        conn.execute(
            "ALTER TABLE draft_sessions ADD COLUMN date_order TEXT NOT NULL "
            "DEFAULT 'WEEKDAYS_FIRST' "
            "CHECK(date_order IN ('WEEKDAYS_FIRST','CHRONOLOGICAL','WEEKENDS_FIRST'))"
        )
    if "picking_paused" not in session_columns:
        conn.execute(
            "ALTER TABLE draft_sessions ADD COLUMN picking_paused INTEGER NOT NULL "
            "DEFAULT 0 CHECK(picking_paused IN (0,1))"
        )
    added_turn_position = "current_position" not in session_columns
    if added_turn_position:
        conn.execute(
            "ALTER TABLE draft_sessions ADD COLUMN current_position INTEGER NOT NULL DEFAULT 1"
        )

    user_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)")
    }
    if "disabled" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL "
            "DEFAULT 0 CHECK(disabled IN (0,1))"
        )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_date_overrides ("
        "session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,"
        "duty_date TEXT NOT NULL,"
        "date_kind TEXT NOT NULL CHECK(date_kind IN ('WEEKDAY','WEEKEND','NO_DUTY')),"
        "updated_by INTEGER NOT NULL REFERENCES users(id),"
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        "PRIMARY KEY(session_id, duty_date)"
        ")"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS duty_swap_requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,"
        "requester_user_id INTEGER NOT NULL REFERENCES users(id),"
        "requester_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,"
        "target_user_id INTEGER NOT NULL REFERENCES users(id),"
        "target_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,"
        "status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED','CANCELLED')),"
        "reviewed_by INTEGER REFERENCES users(id),"
        "reviewed_at TEXT,"
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_swaps_session_status ON duty_swap_requests(session_id, status)"
    )

    _migrate_assignments_for_multiple_picks(conn)
    _normalize_existing_capacities(conn)
    _ensure_performance_indexes(conn)
    _install_audit_retention(conn)
    if added_turn_position:
        _initialize_existing_turn_positions(conn)


def init_db():
    """Initialize database directory, execute baseline schema, and run migrations."""
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
    """Normalize text to single line NFKC string and validate character limits."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not min_length <= len(text) <= max_length:
        raise ValueError("Text length is outside the allowed range.")
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError("Control characters are not allowed.")
    return text


def safe_display_name(value, fallback):
    """Clean and truncate user display names, stripping control characters."""
    raw = unicodedata.normalize("NFKC", str(value or ""))
    without_controls = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in raw
    )
    cleaned = " ".join(without_controls.split())[:120]
    return cleaned or fallback


def normalize_time(value):
    """Validate and format a 24-hour HH:MM time string."""
    text = str(value or "")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("Time must use 24-hour HH:MM format.")
    return text


def normalize_date_order(value):
    """Validate and normalize a date ordering mode choice."""
    text = str(value or DATE_ORDER_WEEKDAYS_FIRST).strip().upper()
    if text not in DATE_ORDER_CHOICES:
        raise ValueError("Unsupported date selection mode.")
    return text


def csrf_token():
    """Retrieve or generate the active session CSRF protection token."""
    session.setdefault("csrf", secrets.token_hex(32))
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.template_filter("date_label")
def date_label(value):
    """Format an ISO date string into a user-friendly 'Mon, Jan 1, 2026' string."""
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return value
    return f"{parsed.strftime('%a, %b')} {parsed.day}, {parsed.year}"


@app.template_filter("time_label")
def time_label(value):
    """Format a 24-hour HH:MM time string into '7:00 PM' 12-hour format."""
    try:
        parsed = datetime.strptime(str(value), "%H:%M")
    except ValueError:
        return value
    return parsed.strftime("%I:%M %p").lstrip("0")


@app.template_filter("date_order_label")
def date_order_label(value):
    """Map date ordering mode constant to user-facing label."""
    return DATE_ORDER_LABELS.get(str(value), DATE_ORDER_LABELS[DATE_ORDER_WEEKDAYS_FIRST])


def require_csrf():
    """Verify CSRF token on modifying form submissions or abort with HTTP 400."""
    supplied = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        abort(400)


def current_user():
    """Fetch the currently authenticated, enabled user from the database session."""
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
        return None
    if bool(user["disabled"]):
        session.clear()
        return None
    return user


@app.context_processor
def inject_user():
    """Inject current user object into all Jinja templates under 'me'."""
    return {"me": current_user()}


def login_required(fn):
    """Route decorator requiring a signed-in user or redirecting to login."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapped


def roles(*allowed):
    """Route decorator requiring a user to hold at least one of the specified roles."""
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
    """Check if an email domain matches allowed university domain whitelist."""
    parts = email.lower().rsplit("@", 1)
    return len(parts) == 2 and parts[1] in ALLOWED_EMAIL_DOMAINS


def google_identity_allowed(info):
    """Validate Google OAuth token claims against university identity policy."""
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
    """Record an administrative or domain action to the immutable audit log table."""
    actor = actor_user_id if actor_user_id is not None else session.get("uid")
    serialized = None
    if details is not None:
        serialized = json.dumps(details, sort_keys=True, separators=(",", ":"))
    db().execute(
        "INSERT INTO audit_log(actor_user_id,action,target_type,target_id,details) "
        "VALUES(?,?,?,?,?)",
        (actor, action, target_type, target_id, serialized),
    )


def _session_id(row):
    """Safely extract integer session ID from a SQLite row, dict, or integer."""
    if isinstance(row, (sqlite3.Row, dict)):
        try:
            return row["id"]
        except (KeyError, IndexError):
            return None
    try:
        return int(row)
    except (TypeError, ValueError):
        return None


def session_row(session_id):
    """Retrieve full session record joined with building and creator details."""
    return db().execute(
        "SELECT s.*, b.name building_name, u.name creator_name FROM draft_sessions s "
        "JOIN buildings b ON b.id=s.building_id "
        "JOIN users u ON u.id=s.created_by WHERE s.id=?",
        (session_id,),
    ).fetchone()


def can_view_session(user, row):
    """Determine if a user has permission to view a given draft session."""
    return bool(user and (user["role"] == "ADMIN" or user["building_id"] == row["building_id"]))


def can_manage(user, row):
    """Determine if a user has management permissions (HRA or Admin) over a session."""
    return bool(
        user
        and (
            user["role"] == "ADMIN"
            or (user["role"] == "HRA" and user["building_id"] == row["building_id"])
        )
    )


def ordered_people(session_id):
    """List session participants ordered by active drafting rotation sequence."""
    return db().execute(
        "SELECT users.*, session_order.position, "
        "(SELECT COUNT(*) FROM assignments "
        " WHERE assignments.session_id=session_order.session_id "
        " AND assignments.user_id=users.id) AS assignment_count, "
        "CASE WHEN EXISTS("
        " SELECT 1 FROM session_deferrals "
        " WHERE session_deferrals.session_id=session_order.session_id "
        " AND session_deferrals.user_id=users.id"
        ") THEN 1 ELSE 0 END AS deferred "
        "FROM session_order JOIN users ON users.id=session_order.user_id "
        "WHERE session_order.session_id=? ORDER BY session_order.position",
        (session_id,),
    ).fetchall()


def active_picker_pool(session_id):
    """List active, non-disabled participants eligible for round-robin drafting."""
    return [person for person in ordered_people(session_id) if not person["disabled"]]


def is_participant(session_id, user_id):
    """Check if a given user ID is enrolled in the session participant pool."""
    return bool(
        db().execute(
            "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
    )


def participant_count(session_id):
    """Count total enrolled participants for a given draft session."""
    return db().execute(
        "SELECT COUNT(*) AS n FROM session_order WHERE session_id=?",
        (session_id,),
    ).fetchone()["n"]


def calendar_dates(row):
    """Generate chronological list of ISO date strings for the session date span."""
    start = date.fromisoformat(row["start_date"])
    end = date.fromisoformat(row["end_date"])
    days = (end - start).days
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days + 1)]


def effective_date_kind(row, duty_date):
    """Determine whether a date is treated as WEEKDAY, WEEKEND, or NO_DUTY."""
    override = db().execute(
        "SELECT date_kind FROM session_date_overrides WHERE session_id=? AND duty_date=?",
        (row["id"], duty_date),
    ).fetchone()
    if override:
        return override["date_kind"]
    return (
        DATE_KIND_WEEKEND
        if date.fromisoformat(duty_date).weekday() in (4, 5)
        else DATE_KIND_WEEKDAY
    )


def is_weekend(row, duty_date):
    """Determine if a date functions as weekend duty in selection phases."""
    return effective_date_kind(row, duty_date) == DATE_KIND_WEEKEND


def is_weekday(row, duty_date):
    """Determine if a date functions as weekday duty in selection phases."""
    return effective_date_kind(row, duty_date) == DATE_KIND_WEEKDAY


def is_no_duty(row, duty_date):
    """Check if a date is configured with zero duty requirement."""
    return effective_date_kind(row, duty_date) == DATE_KIND_NO_DUTY


def date_kinds_for(row):
    """Map every date in the session span to its effective duty date kind."""
    return {
        duty_date: effective_date_kind(row, duty_date)
        for duty_date in calendar_dates(row)
    }


def date_kind_overrides(session_id):
    """Fetch custom date kind overrides explicitly set for a session."""
    return {
        item["duty_date"]: item["date_kind"]
        for item in db().execute(
            "SELECT duty_date, date_kind FROM session_date_overrides WHERE session_id=?",
            (session_id,),
        ).fetchall()
    }


def effective_capacity(row, duty_date):
    """Return maximum RA capacity for a specific date considering overrides."""
    if is_no_duty(row, duty_date):
        return 0
    override = db().execute(
        "SELECT capacity FROM session_date_capacities WHERE session_id=? AND duty_date=?",
        (row["id"], duty_date),
    ).fetchone()
    return override["capacity"] if override else row["capacity"]


def capacities_for(row):
    """Map every date in the session span to its effective staffing capacity."""
    return {
        duty_date: effective_capacity(row, duty_date)
        for duty_date in calendar_dates(row)
    }


def capacity_overrides(session_id):
    """Fetch all explicit per-date capacity override integers for a session."""
    return {
        item["duty_date"]: item["capacity"]
        for item in db().execute(
            "SELECT duty_date, capacity FROM session_date_capacities WHERE session_id=?",
            (session_id,),
        ).fetchall()
    }


def total_slots(row):
    """Sum the total required RA assignments across all dates in the session."""
    return sum(capacities_for(row).values())


def assignment_counts(session_id):
    """Count existing confirmed duty assignments per date string for a session."""
    counts = defaultdict(int)
    for item in db().execute(
        "SELECT duty_date, COUNT(*) AS n FROM assignments WHERE session_id=? GROUP BY duty_date",
        (session_id,),
    ).fetchall():
        counts[item["duty_date"]] = item["n"]
    return counts


def filled_slots(session_id):
    """Calculate the total count of assignments committed for a session."""
    return db().execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE session_id=?",
        (session_id,),
    ).fetchone()["n"]


def session_complete(row):
    """Check if all calendar dates meet or exceed their configured duty capacities."""
    counts = assignment_counts(row["id"])
    return all(
        counts.get(duty_date, 0) >= capacity
        for duty_date, capacity in capacities_for(row).items()
    )


def dates_for(row):
    """Return calendar dates sorted according to the session date selection rule."""
    all_dates = calendar_dates(row)
    mode = row["date_order"] if "date_order" in row.keys() else DATE_ORDER_WEEKDAYS_FIRST
    if mode == DATE_ORDER_CHRONOLOGICAL:
        return all_dates

    kinds = date_kinds_for(row)
    primary_kind = (
        DATE_KIND_WEEKDAY
        if mode == DATE_ORDER_WEEKDAYS_FIRST
        else DATE_KIND_WEEKEND
    )
    secondary_kind = (
        DATE_KIND_WEEKEND
        if mode == DATE_ORDER_WEEKDAYS_FIRST
        else DATE_KIND_WEEKDAY
    )

    primary_dates = [
        item for item in all_dates if kinds.get(item) == primary_kind
    ]
    secondary_dates = [
        item for item in all_dates if kinds.get(item) == secondary_kind
    ]
    no_duty_dates = [
        item for item in all_dates if kinds.get(item) == DATE_KIND_NO_DUTY
    ]
    return primary_dates + secondary_dates + no_duty_dates


def selectable_dates(row, user_id):
    """Return available dates that the specified user can select on their turn."""
    if row["status"] != "OPEN" or row["picking_paused"]:
        return []

    assigned = {
        item["duty_date"]
        for item in db().execute(
            "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=?",
            (row["id"], user_id),
        ).fetchall()
    }
    counts = assignment_counts(row["id"])
    capacities = capacities_for(row)
    open_dates = [
        duty_date
        for duty_date in calendar_dates(row)
        if capacities[duty_date] > 0
        and counts.get(duty_date, 0) < capacities[duty_date]
        and duty_date not in assigned
    ]
    if not open_dates:
        return []

    mode = row["date_order"] if "date_order" in row.keys() else DATE_ORDER_WEEKDAYS_FIRST
    if mode == DATE_ORDER_CHRONOLOGICAL:
        return open_dates

    kinds = date_kinds_for(row)
    primary_kind = (
        DATE_KIND_WEEKDAY
        if mode == DATE_ORDER_WEEKDAYS_FIRST
        else DATE_KIND_WEEKEND
    )
    primary_open = [
        duty_date
        for duty_date in calendar_dates(row)
        if kinds.get(duty_date) == primary_kind
        and counts.get(duty_date, 0) < capacities[duty_date]
        and capacities[duty_date] > 0
    ]
    if primary_open:
        return [item for item in open_dates if kinds.get(item) == primary_kind]
    return open_dates


def selection_phase_label(row):
    """Generate user-facing text indicating current draft phase constraints."""
    mode = row["date_order"] if "date_order" in row.keys() else DATE_ORDER_WEEKDAYS_FIRST
    if mode == DATE_ORDER_CHRONOLOGICAL:
        return "Any open date can be picked"

    counts = assignment_counts(row["id"])
    capacities = capacities_for(row)
    kinds = date_kinds_for(row)
    primary_kind = (
        DATE_KIND_WEEKDAY
        if mode == DATE_ORDER_WEEKDAYS_FIRST
        else DATE_KIND_WEEKEND
    )
    has_primary_open = any(
        kinds.get(duty_date) == primary_kind
        and counts.get(duty_date, 0) < capacities[duty_date]
        and capacities[duty_date] > 0
        for duty_date in calendar_dates(row)
    )
    if mode == DATE_ORDER_WEEKDAYS_FIRST:
        return (
            "Weekdays only until all weekdays are filled"
            if has_primary_open
            else "Weekdays filled \u2014 weekends are now open"
        )
    return (
        "Weekends only until all weekends are filled"
        if has_primary_open
        else "Weekends filled \u2014 weekdays are now open"
    )


def next_picker(session_id):
    """Determine which active participant currently holds the drafting turn."""
    row = session_row(session_id)
    if not row or row["status"] != "OPEN":
        return None

    people = active_picker_pool(session_id)
    if not people or session_complete(row):
        return None

    people_by_position = {person["position"]: person for person in people}
    max_position = max(people_by_position)
    position = row["current_position"]

    for _ in range(max_position):
        candidate = people_by_position.get(position)
        if candidate:
            available = selectable_dates(row, candidate["id"])
            if available:
                return candidate
        position = (position % max_position) + 1

    return None


def advance_turn(session_id, current_user_id):
    """Advance the current turn pointer to the next participant in order."""
    row = db().execute(
        "SELECT current_position FROM draft_sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    people = ordered_people(session_id)
    if not row or not people:
        return
    max_position = max(person["position"] for person in people)
    user_position = next(
        (person["position"] for person in people if person["id"] == current_user_id),
        row["current_position"],
    )
    next_position = (user_position % max_position) + 1
    db().execute(
        "UPDATE draft_sessions SET current_position=? WHERE id=?",
        (next_position, session_id),
    )


def calendar_months(row):
    """Assemble structured month grids containing day metadata for UI rendering."""
    dates = calendar_dates(row)
    capacities = capacities_for(row)
    kinds = date_kinds_for(row)
    counts = assignment_counts(row["id"])

    assignments_by_date = defaultdict(list)
    assignment_user_ids = defaultdict(list)
    for assignment in db().execute(
        "SELECT a.*, u.name, u.role, o.position FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
        "WHERE a.session_id=? ORDER BY o.position, a.id",
        (row["id"],),
    ).fetchall():
        assignments_by_date[assignment["duty_date"]].append(assignment)
        assignment_user_ids[assignment["duty_date"]].append(str(assignment["user_id"]))

    cal = calendar_module.Calendar(firstweekday=6)  # Sunday first
    months = []
    seen = set()

    for duty_date_str in dates:
        dt = date.fromisoformat(duty_date_str)
        month_key = (dt.year, dt.month)
        if month_key in seen:
            continue
        seen.add(month_key)

        year, month = month_key
        month_days = []
        for week in cal.monthdatescalendar(year, month):
            for day in week:
                iso = day.isoformat()
                is_current_month = day.month == month
                in_session = iso in capacities
                capacity = capacities.get(iso, 0)
                assigned_list = assignments_by_date.get(iso, [])
                assigned_count = len(assigned_list)
                is_full = capacity > 0 and assigned_count >= capacity
                kind = kinds.get(iso, DATE_KIND_AUTO)
                is_no_duty_date = kind == DATE_KIND_NO_DUTY

                month_days.append({
                    "date": iso,
                    "day_number": day.day,
                    "is_current_month": is_current_month,
                    "in_session": in_session,
                    "capacity": capacity,
                    "assigned_count": assigned_count,
                    "assigned_list": assigned_list,
                    "assigned_user_ids": ",".join(assignment_user_ids.get(iso, [])),
                    "is_full": is_full,
                    "kind": kind,
                    "is_no_duty": is_no_duty_date,
                    "date_label": f"{day.strftime('%a, %b')} {day.day}, {day.year}",
                })

        months.append({
            "name": dt.strftime("%B %Y"),
            "year": year,
            "month": month,
            "days": month_days,
        })

    return months


def google_calendar_url(building_name, duty_date_str, session_name="Duty"):
    """Generate a 1-click Google Calendar add-event template URL (7:00 PM to 8:00 AM next day)."""
    try:
        start_d = date.fromisoformat(duty_date_str)
        end_d = start_d + timedelta(days=1)
        # RWU is US Eastern Time (America/New_York)
        start_ts = f"{start_d.strftime('%Y%m%d')}T190000"
        end_ts = f"{end_d.strftime('%Y%m%d')}T080000"
        title = f"RA Duty - {building_name}"
        details = f"RA Duty Shift for {building_name} ({session_name}). Hours: 7:00 PM to 8:00 AM next morning."
        location = f"{building_name}, Roger Williams University"
        params = {
            "action": "TEMPLATE",
            "text": title,
            "dates": f"{start_ts}/{end_ts}",
            "details": details,
            "location": location,
            "ctz": "America/New_York",
        }
        return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"
    except Exception:
        return "#"


app.jinja_env.globals["google_calendar_url"] = google_calendar_url


def user_upcoming_shifts(user_id):
    """Retrieve upcoming active duty shifts for a specific participant with partner information."""
    today_iso = date.today().isoformat()
    rows = db().execute(
        "SELECT a.id, a.duty_date, a.session_id, s.name session_name, b.name building_name "
        "FROM assignments a "
        "JOIN draft_sessions s ON s.id=a.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "WHERE a.user_id=? AND a.duty_date >= ? "
        "ORDER BY a.duty_date ASC LIMIT 10",
        (user_id, today_iso),
    ).fetchall()

    shifts = []
    for r in rows:
        partners = db().execute(
            "SELECT u.name FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "WHERE a.session_id=? AND a.duty_date=? AND a.user_id<>?",
            (r["session_id"], r["duty_date"], user_id),
        ).fetchall()
        partner_names = [p["name"] for p in partners]
        shifts.append({
            "id": r["id"],
            "duty_date": r["duty_date"],
            "session_id": r["session_id"],
            "session_name": r["session_name"],
            "building_name": r["building_name"],
            "partner_names": partner_names,
            "google_url": google_calendar_url(r["building_name"], r["duty_date"], r["session_name"]),
        })
    return shifts


def session_swap_requests(session_id):
    """Fetch all swap requests recorded for a draft session."""
    return db().execute(
        "SELECT sw.*, u1.name requester_name, u2.name target_name, "
        "a1.duty_date requester_date, a2.duty_date target_date "
        "FROM duty_swap_requests sw "
        "JOIN users u1 ON u1.id=sw.requester_user_id "
        "JOIN users u2 ON u2.id=sw.target_user_id "
        "JOIN assignments a1 ON a1.id=sw.requester_assignment_id "
        "JOIN assignments a2 ON a2.id=sw.target_assignment_id "
        "WHERE sw.session_id=? ORDER BY sw.id DESC",
        (session_id,),
    ).fetchall()
