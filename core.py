import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, g, redirect, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "ra_draft.db"))
ALLOWED_EMAIL_DOMAINS = {"g.rwu.edu", "rwu.edu"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

oauth = OAuth(app)
if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
    oauth.register(
        name="google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

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
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


init_db()


def csrf_token():
    session.setdefault("csrf", secrets.token_hex(16))
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def require_csrf():
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return db().execute(
        "SELECT users.*, buildings.name AS building_name FROM users "
        "LEFT JOIN buildings ON buildings.id=users.building_id WHERE users.id=?",
        (uid,),
    ).fetchone()


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


def session_row(session_id):
    return db().execute(
        "SELECT s.*, b.name building_name, u.name creator_name FROM draft_sessions s "
        "JOIN buildings b ON b.id=s.building_id JOIN users u ON u.id=s.created_by WHERE s.id=?",
        (session_id,),
    ).fetchone()


def can_manage(user, row):
    return user and (
        user["role"] == "ADMIN"
        or (user["role"] == "HRA" and user["building_id"] == row["building_id"])
    )


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
