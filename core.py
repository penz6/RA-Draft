import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import jwt
from flask import Flask, abort, g, request, session
from jwt import PyJWKClient
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "ra_draft.db"))
ALLOWED_EMAIL_DOMAINS = {"g.rwu.edu", "rwu.edu"}
ADMIN_EMAILS = {
    item.strip().lower()
    for item in os.environ.get("ADMIN_EMAILS", "").split(",")
    if item.strip()
}

SECRET_KEY = os.environ.get("SECRET_KEY", "")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip().lower()
CF_ACCESS_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
CF_ACCESS_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()

if len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY must be set to at least 32 characters.")
if not PUBLIC_HOST or "://" in PUBLIC_HOST or "/" in PUBLIC_HOST:
    raise RuntimeError("PUBLIC_HOST must be set to the public hostname only, for example duty.example.edu.")

team_url = urlparse(CF_ACCESS_TEAM_DOMAIN)
if (
    team_url.scheme != "https"
    or not team_url.hostname
    or not team_url.hostname.endswith(".cloudflareaccess.com")
    or team_url.path not in ("", "/")
):
    raise RuntimeError(
        "CF_ACCESS_TEAM_DOMAIN must be an HTTPS Cloudflare Access team domain, "
        "for example https://example.cloudflareaccess.com."
    )
if len(CF_ACCESS_AUD) < 16:
    raise RuntimeError("CF_ACCESS_AUD must be set to the Cloudflare Access Application Audience (AUD) tag.")

CF_ACCESS_CERTS_URL = f"{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"
_access_jwks = PyJWKClient(CF_ACCESS_CERTS_URL, cache_jwk_set=True, lifespan=300, timeout=5)

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
    MAX_CONTENT_LENGTH=1024 * 1024,
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  access_sub TEXT UNIQUE NOT NULL,
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
  UNIQUE(session_id, user_id)
);
CREATE TABLE IF NOT EXISTS session_deferrals (
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  deferred_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(session_id, user_id)
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
CREATE TABLE IF NOT EXISTS pending_identity_claims (
  email TEXT PRIMARY KEY,
  access_sub TEXT UNIQUE NOT NULL,
  seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 10000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def migrate_users_identity(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "google_sub" in columns and "access_sub" not in columns:
        conn.execute("ALTER TABLE users RENAME COLUMN google_sub TO access_sub")
        conn.execute(
            "UPDATE users SET access_sub='legacy-google:' || access_sub "
            "WHERE access_sub NOT LIKE 'legacy-google:%'"
        )


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(SCHEMA)
    migrate_users_identity(conn)
    conn.commit()
    conn.close()


init_db()


@app.after_request
def security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"
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
        response.headers["Cache-Control"] = "no-store"
    return response


def csrf_token():
    session.setdefault("csrf", secrets.token_hex(32))
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def require_csrf():
    supplied = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        abort(400)


def allowed_email(email):
    parts = email.lower().rsplit("@", 1)
    return len(parts) == 2 and parts[1] in ALLOWED_EMAIL_DOMAINS


def verify_access_token(token, signing_key=None):
    key = signing_key if signing_key is not None else _access_jwks.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=CF_ACCESS_AUD,
        issuer=CF_ACCESS_TEAM_DOMAIN,
        options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub"]},
    )


def access_identity_allowed(claims):
    email = (claims.get("email") or "").strip().lower()
    return bool(claims.get("type") == "app" and claims.get("sub") and allowed_email(email))


def access_claims():
    if "access_claims" in g:
        return g.access_claims
    token = request.headers.get("Cf-Access-Jwt-Assertion", "").strip()
    if not token:
        g.access_claims = None
        return None
    try:
        claims = verify_access_token(token)
    except Exception as exc:
        app.logger.warning("Cloudflare Access JWT validation failed: %s", type(exc).__name__)
        abort(403)
    if not access_identity_allowed(claims):
        abort(403)
    g.access_claims = claims
    return claims


def audit(action, target_type=None, target_id=None, details=None, actor_user_id=None):
    actor = actor_user_id
    if actor is None:
        cached = g.get("current_user")
        if cached:
            actor = cached["id"]
    serialized = json.dumps(details, sort_keys=True, separators=(",", ":")) if details is not None else None
    db().execute(
        "INSERT INTO audit_log(actor_user_id,action,target_type,target_id,details) VALUES(?,?,?,?,?)",
        (actor, action, target_type, target_id, serialized),
    )


def current_user():
    if "current_user" in g:
        return g.current_user
    claims = access_claims()
    if not claims:
        g.current_user = None
        return None

    access_sub = str(claims["sub"]).strip()
    email = (claims.get("email") or "").strip().lower()
    display_name = (claims.get("name") or email).strip()
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE access_sub=?", (access_sub,)).fetchone()

    if user:
        email_owner = conn.execute("SELECT id FROM users WHERE email=? AND id<>?", (email, user["id"])).fetchone()
        if email_owner:
            abort(403)
        if user["email"] != email or user["name"] != display_name:
            conn.execute("UPDATE users SET email=?,name=? WHERE id=?", (email, display_name, user["id"]))
            audit("auth.profile_update", "user", user["id"], {"email": email}, actor_user_id=user["id"])
            conn.commit()
    else:
        email_owner = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if email_owner:
            if (
                email in ADMIN_EMAILS
                and email_owner["role"] == "ADMIN"
                and str(email_owner["access_sub"]).startswith("legacy-google:")
            ):
                conn.execute("UPDATE users SET access_sub=?,name=? WHERE id=?", (access_sub, display_name, email_owner["id"]))
                audit("auth.legacy_admin_rebind", "user", email_owner["id"], {"email": email}, actor_user_id=email_owner["id"])
                conn.commit()
                user = conn.execute("SELECT * FROM users WHERE id=?", (email_owner["id"],)).fetchone()
            else:
                conn.execute(
                    "INSERT INTO pending_identity_claims(email,access_sub) VALUES(?,?) "
                    "ON CONFLICT(email) DO UPDATE SET access_sub=excluded.access_sub,seen_at=CURRENT_TIMESTAMP",
                    (email, access_sub),
                )
                conn.commit()
                abort(403, description="Your verified Cloudflare identity does not match the stored account identity. An RA Draft admin must approve the identity change.")
        else:
            role = "ADMIN" if email in ADMIN_EMAILS else "RA"
            cur = conn.execute("INSERT INTO users(access_sub,email,name,role) VALUES(?,?,?,?)", (access_sub, email, display_name, role))
            uid = cur.lastrowid
            audit("auth.user_created", "user", uid, {"role": role}, actor_user_id=uid)
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    g.current_user = conn.execute(
        "SELECT users.*,buildings.name AS building_name FROM users "
        "LEFT JOIN buildings ON buildings.id=users.building_id WHERE users.id=?",
        (user["id"],),
    ).fetchone()
    return g.current_user


@app.before_request
def require_cloudflare_access():
    if request.path == "/healthz" or request.path.startswith("/static/"):
        return None
    if not current_user():
        abort(403)
    return None


@app.context_processor
def inject_user():
    return {"me": current_user()}


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def roles(*allowed):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or user["role"] not in allowed:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return deco


def session_row(session_id):
    return db().execute(
        "SELECT s.*, b.name building_name, u.name creator_name FROM draft_sessions s "
        "JOIN buildings b ON b.id=s.building_id JOIN users u ON u.id=s.created_by WHERE s.id=?",
        (session_id,),
    ).fetchone()


def can_manage(user, row):
    return user and (user["role"] == "ADMIN" or (user["role"] == "HRA" and user["building_id"] == row["building_id"]))


def ordered_people(session_id):
    return db().execute(
        "SELECT u.id,u.name,u.email,o.position,a.duty_date,"
        "CASE WHEN d.user_id IS NULL THEN 0 ELSE 1 END AS deferred "
        "FROM session_order o JOIN users u ON u.id=o.user_id "
        "LEFT JOIN assignments a ON a.session_id=o.session_id AND a.user_id=u.id "
        "LEFT JOIN session_deferrals d ON d.session_id=o.session_id AND d.user_id=u.id "
        "WHERE o.session_id=? ORDER BY o.position",
        (session_id,),
    ).fetchall()


def next_picker(session_id):
    return db().execute(
        "SELECT u.* FROM session_order o JOIN users u ON u.id=o.user_id "
        "LEFT JOIN assignments a ON a.session_id=o.session_id AND a.user_id=o.user_id "
        "LEFT JOIN session_deferrals d ON d.session_id=o.session_id AND d.user_id=o.user_id "
        "WHERE o.session_id=? AND a.id IS NULL AND d.user_id IS NULL "
        "ORDER BY o.position LIMIT 1",
        (session_id,),
    ).fetchone()


def dates_for(row):
    start = datetime.fromisoformat(row["start_date"]).date()
    end = datetime.fromisoformat(row["end_date"]).date()
    out = []
    day = start
    while day <= end:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out
