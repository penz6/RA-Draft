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
  disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
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
  picking_paused INTEGER NOT NULL DEFAULT 0 CHECK(picking_paused IN (0,1)),
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
CREATE TABLE IF NOT EXISTS session_date_overrides (
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  duty_date TEXT NOT NULL,
  date_kind TEXT NOT NULL
    CHECK(date_kind IN ('WEEKDAY','WEEKEND','NO_DUTY')),
  updated_by INTEGER NOT NULL REFERENCES users(id),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(session_id, duty_date)
);
CREATE TABLE IF NOT EXISTS duty_swap_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  requester_user_id INTEGER NOT NULL REFERENCES users(id),
  requester_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  target_user_id INTEGER NOT NULL REFERENCES users(id),
  target_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','TARGET_APPROVED','APPROVED','REJECTED','CANCELLED')),
  batch_id TEXT,
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  target_reviewed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
CREATE INDEX IF NOT EXISTS idx_assignments_session_date ON assignments(session_id, duty_date);
CREATE INDEX IF NOT EXISTS idx_users_building ON users(building_id);
CREATE INDEX IF NOT EXISTS idx_sessions_building ON draft_sessions(building_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_user_id);
CREATE INDEX IF NOT EXISTS idx_swaps_session_status ON duty_swap_requests(session_id, status);
"""


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
        "status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','TARGET_APPROVED','APPROVED','REJECTED','CANCELLED')),"
        "batch_id TEXT,"
        "reviewed_by INTEGER REFERENCES users(id),"
        "reviewed_at TEXT,"
        "target_reviewed_at TEXT,"
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    # Migrate existing duty_swap_requests tables to add new columns and updated CHECK constraint
    if _table_exists(conn, "duty_swap_requests"):
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='duty_swap_requests'"
        ).fetchone()
        table_sql = table_sql_row["sql"] if table_sql_row else ""
        if "TARGET_APPROVED" not in table_sql:
            conn.execute("ALTER TABLE duty_swap_requests RENAME TO duty_swap_requests_old")
            conn.execute(
                "CREATE TABLE duty_swap_requests ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,"
                "requester_user_id INTEGER NOT NULL REFERENCES users(id),"
                "requester_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,"
                "target_user_id INTEGER NOT NULL REFERENCES users(id),"
                "target_assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,"
                "status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','TARGET_APPROVED','APPROVED','REJECTED','CANCELLED')),"
                "batch_id TEXT,"
                "reviewed_by INTEGER REFERENCES users(id),"
                "reviewed_at TEXT,"
                "target_reviewed_at TEXT,"
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            old_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(duty_swap_requests_old)")
            }
            if "batch_id" in old_cols and "target_reviewed_at" in old_cols:
                conn.execute(
                    "INSERT INTO duty_swap_requests "
                    "(id, session_id, requester_user_id, requester_assignment_id, target_user_id, target_assignment_id, status, batch_id, reviewed_by, reviewed_at, target_reviewed_at, created_at) "
                    "SELECT id, session_id, requester_user_id, requester_assignment_id, target_user_id, target_assignment_id, status, batch_id, reviewed_by, reviewed_at, target_reviewed_at, created_at FROM duty_swap_requests_old"
                )
            else:
                conn.execute(
                    "INSERT INTO duty_swap_requests "
                    "(id, session_id, requester_user_id, requester_assignment_id, target_user_id, target_assignment_id, status, batch_id, reviewed_by, reviewed_at, target_reviewed_at, created_at) "
                    "SELECT id, session_id, requester_user_id, requester_assignment_id, target_user_id, target_assignment_id, status, NULL, reviewed_by, reviewed_at, NULL, created_at FROM duty_swap_requests_old"
                )
            conn.execute("DROP TABLE duty_swap_requests_old")

        else:
            swap_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(duty_swap_requests)")
            }
            if "batch_id" not in swap_columns:
                conn.execute("ALTER TABLE duty_swap_requests ADD COLUMN batch_id TEXT")
            if "target_reviewed_at" not in swap_columns:
                conn.execute("ALTER TABLE duty_swap_requests ADD COLUMN target_reviewed_at TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_swaps_session_status ON duty_swap_requests(session_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_swaps_batch ON duty_swap_requests(batch_id)"
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
    """Retrieve ordered list of participants and their current assignment counts."""
    return db().execute(
        "SELECT u.id,u.name,u.email,u.role,u.disabled,o.position,"
        "(SELECT COUNT(*) FROM assignments a "
        " WHERE a.session_id=o.session_id AND a.user_id=o.user_id) AS assignment_count,"
        "0 AS deferred "
        "FROM session_order o JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? ORDER BY o.position",
        (session_id,),
    ).fetchall()


def participant_count(session_id):
    """Count the number of participants registered in a draft session."""
    return db().execute(
        "SELECT COUNT(*) AS n FROM session_order WHERE session_id=?",
        (session_id,),
    ).fetchone()["n"]


def is_participant(session_id, user_id):
    """Check if a specific user is registered in the session turn order."""
    return bool(
        db().execute(
            "SELECT 1 FROM session_order WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
    )


def calendar_dates(row):
    """Generate list of ISO date strings for the inclusive date span of a session."""
    start = date.fromisoformat(row["start_date"])
    end = date.fromisoformat(row["end_date"])
    result = []
    day = start
    while day <= end:
        result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def calendar_months(row):
    """Generate structured monthly calendar grid weeks for template rendering."""
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


def natural_date_kind(date_string):
    """Determine default calendar kind (WEEKDAY or WEEKEND) based on day of week."""
    try:
        parsed = date.fromisoformat(str(date_string))
    except ValueError:
        return DATE_KIND_WEEKDAY
    return DATE_KIND_WEEKEND if parsed.weekday() in (4, 5) else DATE_KIND_WEEKDAY


def date_kind_overrides(session_id):
    """Retrieve explicit weekday/weekend/no-duty overrides for a session."""
    rows = db().execute(
        "SELECT duty_date, date_kind FROM session_date_overrides WHERE session_id=?",
        (session_id,),
    ).fetchall()
    return {row["duty_date"]: row["date_kind"] for row in rows}


def effective_date_kind(row, duty_date, overrides=None):
    """Calculate effective date kind taking explicit overrides and natural weekday into account."""
    if overrides is None:
        session_id = _session_id(row)
        overrides = date_kind_overrides(session_id) if session_id is not None else {}
    override = overrides.get(duty_date)
    if override in DATE_KIND_OVERRIDE_CHOICES:
        return override
    return natural_date_kind(duty_date)


def date_kinds_for(row):
    """Map each date in session to its effective date kind."""
    return {
        duty_date: effective_date_kind(row, duty_date)
        for duty_date in calendar_dates(row)
    }


def dates_for(row):
    """Return selectable dates sorted according to the session date ordering rules."""
    kinds = date_kinds_for(row)
    days = [date.fromisoformat(v) for v in calendar_dates(row) if kinds[v] != DATE_KIND_NO_DUTY]
    try:
        order = normalize_date_order(row["date_order"])
    except (IndexError, KeyError, TypeError, ValueError):
        order = DATE_ORDER_WEEKDAYS_FIRST

    if order == DATE_ORDER_WEEKDAYS_FIRST:
        days.sort(key=lambda item: (kinds[item.isoformat()] != DATE_KIND_WEEKDAY, item))
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        days.sort(key=lambda item: (kinds[item.isoformat()] != DATE_KIND_WEEKEND, item))
    return [item.isoformat() for item in days]


def capacity_overrides(session_id):
    """Retrieve explicit per-date staffing capacity overrides for a session."""
    return {
        row["duty_date"]: row["capacity"]
        for row in db().execute(
            "SELECT duty_date,capacity FROM session_date_capacities "
            "WHERE session_id=? ORDER BY duty_date",
            (session_id,),
        ).fetchall()
    }


def capacities_for(row):
    """Compute effective staffing capacity for every date in a draft session."""
    session_id = _session_id(row)
    overrides = capacity_overrides(session_id) if session_id is not None else {}
    kinds = date_kinds_for(row)
    base_capacity = int(row["capacity"])
    capacities = {}
    for duty_date in calendar_dates(row):
        if kinds[duty_date] == DATE_KIND_NO_DUTY:
            capacities[duty_date] = 0
        elif duty_date in overrides:
            capacities[duty_date] = overrides[duty_date]
        else:
            capacities[duty_date] = base_capacity
    return capacities


def effective_capacity(row, duty_date):
    """Get staffing capacity for a specific date in a draft session."""
    return capacities_for(row).get(duty_date, int(row["capacity"]))


def assignment_counts(session_id):
    """Count existing assignments grouped by duty date for a session."""
    return {
        row["duty_date"]: row["n"]
        for row in db().execute(
            "SELECT duty_date,COUNT(*) AS n FROM assignments "
            "WHERE session_id=? GROUP BY duty_date",
            (session_id,),
        ).fetchall()
    }


def total_slots(row):
    """Calculate the total number of duty slots required across the session."""
    return sum(capacities_for(row).values())


def filled_slots(session_id):
    """Count total filled duty slots in a session."""
    return db().execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE session_id=?",
        (session_id,),
    ).fetchone()["n"]


def session_complete(row):
    """Check if all required slots on all active duty dates have been filled."""
    session_id = _session_id(row)
    if session_id is None:
        return True
    counts = assignment_counts(session_id)
    capacities = capacities_for(row)
    return all(counts.get(duty_date, 0) >= capacity for duty_date, capacity in capacities.items())


def user_assignment_dates(session_id, user_id):
    """Get the set of duty dates already assigned to a specific user in a session."""
    return {
        row["duty_date"]
        for row in db().execute(
            "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        ).fetchall()
    }


def _precompute_session_selection_context(row):
    """Precompute session dates, capacities, and assignment state to avoid N+1 queries."""
    session_id = _session_id(row)
    if session_id is None:
        return None
    counts = assignment_counts(session_id)
    capacities = capacities_for(row)
    kinds = date_kinds_for(row)

    rows = db().execute(
        "SELECT user_id, duty_date FROM assignments WHERE session_id=?",
        (session_id,),
    ).fetchall()
    user_assignments = defaultdict(set)
    for r in rows:
        user_assignments[r["user_id"]].add(r["duty_date"])

    globally_open = [
        duty_date
        for duty_date in dates_for(row)
        if capacities[duty_date] > 0
        and counts.get(duty_date, 0) < capacities[duty_date]
    ]

    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        weekday_phase = [
            duty_date
            for duty_date in globally_open
            if kinds[duty_date] == DATE_KIND_WEEKDAY
        ]
        if weekday_phase:
            globally_open = weekday_phase
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        weekend_phase = [
            duty_date
            for duty_date in globally_open
            if kinds[duty_date] == DATE_KIND_WEEKEND
        ]
        if weekend_phase:
            globally_open = weekend_phase

    is_complete = all(
        counts.get(duty_date, 0) >= capacity
        for duty_date, capacity in capacities.items()
    )

    return {
        "counts": counts,
        "capacities": capacities,
        "kinds": kinds,
        "user_assignments": user_assignments,
        "globally_open": globally_open,
        "is_complete": is_complete,
    }


def selectable_dates(row, user_id, *, _precomputed=None):
    """Return dates open and eligible for selection by a specific participant."""
    session_id = _session_id(row)
    if session_id is None:
        return []

    if _precomputed is not None:
        globally_open = _precomputed["globally_open"]
        user_assignments = _precomputed["user_assignments"].get(user_id, set())
    else:
        counts = assignment_counts(session_id)
        capacities = capacities_for(row)
        kinds = date_kinds_for(row)
        user_assignments = user_assignment_dates(session_id, user_id)

        globally_open = [
            duty_date
            for duty_date in dates_for(row)
            if capacities[duty_date] > 0
            and counts.get(duty_date, 0) < capacities[duty_date]
        ]

        order = normalize_date_order(row["date_order"])
        if order == DATE_ORDER_WEEKDAYS_FIRST:
            weekday_phase = [
                duty_date
                for duty_date in globally_open
                if kinds[duty_date] == DATE_KIND_WEEKDAY
            ]
            if weekday_phase:
                globally_open = weekday_phase
        elif order == DATE_ORDER_WEEKENDS_FIRST:
            weekend_phase = [
                duty_date
                for duty_date in globally_open
                if kinds[duty_date] == DATE_KIND_WEEKEND
            ]
            if weekend_phase:
                globally_open = weekend_phase

    return [
        duty_date
        for duty_date in globally_open
        if duty_date not in user_assignments
    ]


def selection_phase_label(row):
    """Generate status description string explaining which dates are currently open."""
    session_id = _session_id(row)
    if session_id is None:
        return ""
    counts = assignment_counts(session_id)
    capacities = capacities_for(row)
    kinds = date_kinds_for(row)
    open_dates = [
        duty_date
        for duty_date in calendar_dates(row)
        if capacities[duty_date] > 0
        and counts.get(duty_date, 0) < capacities[duty_date]
    ]
    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        if any(kinds[value] == DATE_KIND_WEEKDAY for value in open_dates):
            return "Weekday dates are open; weekends unlock after weekday slots fill."
        if open_dates:
            return "Weekend dates are now open."
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        if any(kinds[value] == DATE_KIND_WEEKEND for value in open_dates):
            return "Weekend dates are open; weekdays unlock after weekend slots fill."
        if open_dates:
            return "Weekday dates are now open."
    elif open_dates:
        return "Any required date with an open slot can be selected."
    return "Every required duty slot is filled."


def next_picker(session_id):
    """Identify the participant whose turn it is to pick a shift in the round robin."""
    row = session_row(session_id)
    if not row:
        return None

    precomputed = _precompute_session_selection_context(row)
    if not precomputed or precomputed["is_complete"] or not precomputed["globally_open"]:
        return None

    active = db().execute(
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
        if selectable_dates(row, participant["id"]):
            return participant
    return None


def advance_turn(session_id, after_user_id):
    """Advance session current turn position pointer to the next active participant."""
    current = db().execute(
        "SELECT position FROM session_order WHERE session_id=? AND user_id=?",
        (session_id, after_user_id),
    ).fetchone()
    if not current:
        raise ValueError("User is not in the session order.")

    active = db().execute(
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
    db().execute(
        "UPDATE draft_sessions SET current_position=? WHERE id=?",
        (next_position, session_id),
    )




def user_upcoming_shifts(user_id):
    """Retrieve upcoming or active assigned duty shifts for a user across all sessions."""
    today_str = date.today().isoformat()
    rows = db().execute(
        "SELECT a.id AS assignment_id, a.duty_date, s.id AS session_id, s.name AS session_name, "
        "s.status AS session_status, b.name AS building_name "
        "FROM assignments a "
        "JOIN draft_sessions s ON s.id=a.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "WHERE a.user_id=? AND a.duty_date >= ? "
        "ORDER BY a.duty_date ASC, s.id ASC",
        (user_id, today_str),
    ).fetchall()

    shifts = []
    for r in rows:
        partners = db().execute(
            "SELECT u.name FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "WHERE a.session_id=? AND a.duty_date=? AND a.user_id<>? "
            "ORDER BY a.id",
            (r["session_id"], r["duty_date"], user_id),
        ).fetchall()
        shifts.append({
            "assignment_id": r["assignment_id"],
            "duty_date": r["duty_date"],
            "session_id": r["session_id"],
            "session_name": r["session_name"],
            "session_status": r["session_status"],
            "building_name": r["building_name"],
            "partner_names": [p["name"] for p in partners],
        })
    return shifts


def session_swap_requests(session_id):
    """Retrieve all swap requests for a session with requester and target participant details."""
    return db().execute(
        "SELECT sr.*, "
        "u1.name AS requester_name, a1.duty_date AS requester_date, "
        "u2.name AS target_name, a2.duty_date AS target_date, "
        "ur.name AS reviewer_name "
        "FROM duty_swap_requests sr "
        "JOIN users u1 ON u1.id=sr.requester_user_id "
        "JOIN assignments a1 ON a1.id=sr.requester_assignment_id "
        "JOIN users u2 ON u2.id=sr.target_user_id "
        "JOIN assignments a2 ON a2.id=sr.target_assignment_id "
        "LEFT JOIN users ur ON ur.id=sr.reviewed_by "
        "WHERE sr.session_id=? "
        "ORDER BY CASE sr.status WHEN 'PENDING' THEN 0 WHEN 'TARGET_APPROVED' THEN 1 ELSE 2 END, sr.created_at DESC",
        (session_id,),
    ).fetchall()


def pending_target_swaps(user_id):
    """Retrieve swap batches awaiting target user approval."""
    return db().execute(
        "SELECT sr.*, "
        "u1.name AS requester_name, a1.duty_date AS requester_date, "
        "u2.name AS target_name, a2.duty_date AS target_date, "
        "s.name AS session_name, b.name AS building_name "
        "FROM duty_swap_requests sr "
        "JOIN users u1 ON u1.id=sr.requester_user_id "
        "JOIN assignments a1 ON a1.id=sr.requester_assignment_id "
        "JOIN users u2 ON u2.id=sr.target_user_id "
        "JOIN assignments a2 ON a2.id=sr.target_assignment_id "
        "JOIN draft_sessions s ON s.id=sr.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "WHERE sr.target_user_id=? AND sr.status='PENDING' "
        "ORDER BY sr.created_at DESC",
        (user_id,),
    ).fetchall()


def hra_pending_swaps(building_id):
    """Retrieve swap batches awaiting HRA approval for a building."""
    return db().execute(
        "SELECT sr.*, "
        "u1.name AS requester_name, a1.duty_date AS requester_date, "
        "u2.name AS target_name, a2.duty_date AS target_date, "
        "s.name AS session_name "
        "FROM duty_swap_requests sr "
        "JOIN users u1 ON u1.id=sr.requester_user_id "
        "JOIN assignments a1 ON a1.id=sr.requester_assignment_id "
        "JOIN users u2 ON u2.id=sr.target_user_id "
        "JOIN assignments a2 ON a2.id=sr.target_assignment_id "
        "JOIN draft_sessions s ON s.id=sr.session_id "
        "WHERE s.building_id=? AND sr.status='TARGET_APPROVED' "
        "ORDER BY sr.created_at DESC",
        (building_id,),
    ).fetchall()


def swap_batch_details(batch_id):
    """Retrieve all swap request rows belonging to a batch."""
    return db().execute(
        "SELECT sr.*, "
        "u1.name AS requester_name, a1.duty_date AS requester_date, "
        "u2.name AS target_name, a2.duty_date AS target_date "
        "FROM duty_swap_requests sr "
        "JOIN users u1 ON u1.id=sr.requester_user_id "
        "JOIN assignments a1 ON a1.id=sr.requester_assignment_id "
        "JOIN users u2 ON u2.id=sr.target_user_id "
        "JOIN assignments a2 ON a2.id=sr.target_assignment_id "
        "WHERE sr.batch_id=? "
        "ORDER BY a1.duty_date",
        (batch_id,),
    ).fetchall()


def user_pending_swap_count(user_id):
    """Count pending incoming swap requests for a user (for badge display)."""
    row = db().execute(
        "SELECT COUNT(DISTINCT batch_id) AS n FROM duty_swap_requests "
        "WHERE target_user_id=? AND status='PENDING'",
        (user_id,),
    ).fetchone()
    return row["n"] if row else 0

