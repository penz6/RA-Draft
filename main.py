import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "ra_draft.db"))

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
  capacity INTEGER NOT NULL DEFAULT 1,
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
    conn = sqlite3.connect(DB_PATH)
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
        "SELECT users.*, buildings.name AS building_name FROM users LEFT JOIN buildings ON buildings.id=users.building_id WHERE users.id=?",
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


def session_row(session_id):
    return db().execute(
        "SELECT s.*, b.name building_name, u.name creator_name FROM draft_sessions s JOIN buildings b ON b.id=s.building_id JOIN users u ON u.id=s.created_by WHERE s.id=?",
        (session_id,),
    ).fetchone()


def can_manage(user, row):
    return user and (user["role"] == "ADMIN" or (user["role"] == "HRA" and user["building_id"] == row["building_id"]))


def ordered_people(session_id):
    return db().execute(
        "SELECT u.id,u.name,u.email,o.position,a.duty_date FROM session_order o JOIN users u ON u.id=o.user_id LEFT JOIN assignments a ON a.session_id=o.session_id AND a.user_id=u.id WHERE o.session_id=? ORDER BY o.position",
        (session_id,),
    ).fetchall()


def next_picker(session_id):
    return db().execute(
        "SELECT u.* FROM session_order o JOIN users u ON u.id=o.user_id LEFT JOIN assignments a ON a.session_id=o.session_id AND a.user_id=o.user_id WHERE o.session_id=? AND a.id IS NULL ORDER BY o.position LIMIT 1",
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if not getattr(oauth, "google", None):
        flash("Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", "error")
        return render_template("index.html")
    return oauth.google.authorize_redirect(url_for("auth_callback", _external=True))


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    info = token.get("userinfo") or oauth.google.userinfo(token=token)
    email = info["email"].lower()
    user = db().execute("SELECT * FROM users WHERE google_sub=? OR email=?", (info["sub"], email)).fetchone()
    if user:
        db().execute("UPDATE users SET google_sub=?, email=?, name=? WHERE id=?", (info["sub"], email, info.get("name") or email, user["id"]))
        uid = user["id"]
    else:
        admin_emails = {x.strip().lower() for x in os.environ.get("ADMIN_EMAILS", "").split(",") if x.strip()}
        role = "ADMIN" if email in admin_emails else "RA"
        cur = db().execute("INSERT INTO users(google_sub,email,name,role) VALUES(?,?,?,?)", (info["sub"], email, info.get("name") or email, role))
        uid = cur.lastrowid
    db().commit()
    session["uid"] = uid
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    require_csrf()
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "ADMIN":
        sessions = db().execute("SELECT s.*,b.name building_name FROM draft_sessions s JOIN buildings b ON b.id=s.building_id ORDER BY s.created_at DESC").fetchall()
    else:
        sessions = db().execute("SELECT s.*,b.name building_name FROM draft_sessions s JOIN buildings b ON b.id=s.building_id WHERE s.building_id=? ORDER BY s.created_at DESC", (user["building_id"],)).fetchall() if user["building_id"] else []
    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()
    ras = db().execute("SELECT * FROM users WHERE building_id=? AND role='RA' ORDER BY name", (user["building_id"],)).fetchall() if user["building_id"] else []
    return render_template("dashboard.html", sessions=sessions, buildings=buildings, ras=ras)


@app.route("/sessions", methods=["POST"])
@roles("HRA", "ADMIN")
def create_session():
    require_csrf()
    user = current_user()
    building_id = int(request.form.get("building_id") or user["building_id"] or 0)
    if user["role"] == "HRA" and building_id != user["building_id"]:
        abort(403)
    start, end = request.form["start_date"], request.form["end_date"]
    if end < start:
        flash("End date must be on or after start date.", "error")
        return redirect(url_for("dashboard"))
    cur = db().execute(
        "INSERT INTO draft_sessions(name,building_id,start_date,end_date,shift_start,shift_end,capacity,created_by) VALUES(?,?,?,?,?,?,?,?)",
        (request.form["name"].strip(), building_id, start, end, request.form.get("shift_start", "19:00"), request.form.get("shift_end", "07:00"), max(1, int(request.form.get("capacity", 1))), user["id"]),
    )
    sid = cur.lastrowid
    ids = [int(x) for x in request.form.getlist("ra_ids")]
    for pos, uid in enumerate(ids, start=1):
        allowed = db().execute("SELECT 1 FROM users WHERE id=? AND building_id=? AND role='RA'", (uid, building_id)).fetchone()
        if allowed:
            db().execute("INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,?)", (sid, uid, pos))
    db().commit()
    return redirect(url_for("view_session", session_id=sid))


@app.route("/sessions/<int:session_id>")
@login_required
def view_session(session_id):
    row = session_row(session_id)
    if not row:
        abort(404)
    user = current_user()
    if user["role"] != "ADMIN" and user["building_id"] != row["building_id"]:
        abort(403)
    people = ordered_people(session_id)
    picks = db().execute("SELECT a.*,u.name FROM assignments a JOIN users u ON u.id=a.user_id WHERE a.session_id=? ORDER BY a.duty_date,u.name", (session_id,)).fetchall()
    counts = {r["duty_date"]: r["n"] for r in db().execute("SELECT duty_date,COUNT(*) n FROM assignments WHERE session_id=? GROUP BY duty_date", (session_id,)).fetchall()}
    return render_template("session.html", draft=row, people=people, picks=picks, counts=counts, dates=dates_for(row), next=next_picker(session_id), can_manage=can_manage(user, row))


@app.route("/sessions/<int:session_id>/pick", methods=["POST"])
@login_required
def pick_shift(session_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or row["status"] != "OPEN":
        abort(400)
    picker = next_picker(session_id)
    if not picker or picker["id"] != user["id"]:
        abort(403)
    duty_date = request.form["duty_date"]
    if duty_date not in dates_for(row):
        abort(400)
    count = db().execute("SELECT COUNT(*) n FROM assignments WHERE session_id=? AND duty_date=?", (session_id, duty_date)).fetchone()["n"]
    if count >= row["capacity"]:
        flash("That duty date is full.", "error")
    else:
        db().execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (session_id, user["id"], duty_date, user["id"]))
        db().commit()
        flash("Duty shift selected.", "success")
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/assign", methods=["POST"])
@roles("HRA", "ADMIN")
def manual_assign(session_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or not can_manage(user, row):
        abort(403)
    uid = int(request.form["user_id"])
    duty_date = request.form.get("duty_date", "")
    allowed = db().execute("SELECT 1 FROM session_order WHERE session_id=? AND user_id=?", (session_id, uid)).fetchone()
    if not allowed:
        abort(400)
    db().execute("DELETE FROM assignments WHERE session_id=? AND user_id=?", (session_id, uid))
    if duty_date:
        if duty_date not in dates_for(row):
            abort(400)
        count = db().execute("SELECT COUNT(*) n FROM assignments WHERE session_id=? AND duty_date=?", (session_id, duty_date)).fetchone()["n"]
        if count >= row["capacity"]:
            flash("That date is already at capacity.", "error")
            db().commit()
            return redirect(url_for("view_session", session_id=session_id))
        db().execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (session_id, uid, duty_date, user["id"]))
    db().commit()
    flash("Assignment updated.", "success")
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/status", methods=["POST"])
@roles("HRA", "ADMIN")
def session_status(session_id):
    require_csrf()
    row = session_row(session_id)
    if not row or not can_manage(current_user(), row):
        abort(403)
    status = request.form.get("status")
    if status not in ("OPEN", "CLOSED"):
        abort(400)
    db().execute("UPDATE draft_sessions SET status=? WHERE id=?", (status, session_id))
    db().commit()
    return redirect(url_for("view_session", session_id=session_id))


def ics_escape(text):
    return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


@app.route("/calendar/<int:assignment_id>.ics")
@login_required
def calendar_ics(assignment_id):
    a = db().execute("SELECT a.*,s.name session_name,s.shift_start,s.shift_end,b.name building_name FROM assignments a JOIN draft_sessions s ON s.id=a.session_id JOIN buildings b ON b.id=s.building_id WHERE a.id=?", (assignment_id,)).fetchone()
    if not a or (current_user()["role"] != "ADMIN" and a["user_id"] != current_user()["id"]):
        abort(403)
    start = datetime.fromisoformat(f"{a['duty_date']}T{a['shift_start']}")
    end = datetime.fromisoformat(f"{a['duty_date']}T{a['shift_end']}")
    if end <= start:
        end += timedelta(days=1)
    body = "\r\n".join(["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//RA Draft//Duty Scheduler//EN","BEGIN:VEVENT",f"UID:ra-draft-{assignment_id}@local",f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",f"SUMMARY:{ics_escape(a['session_name'])} Duty",f"LOCATION:{ics_escape(a['building_name'])}","END:VEVENT","END:VCALENDAR",""])
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": f"attachment; filename=duty-{a['duty_date']}.ics"})


@app.route("/calendar/<int:assignment_id>/google")
@login_required
def calendar_google(assignment_id):
    a = db().execute("SELECT a.*,s.name session_name,s.shift_start,s.shift_end,b.name building_name FROM assignments a JOIN draft_sessions s ON s.id=a.session_id JOIN buildings b ON b.id=s.building_id WHERE a.id=?", (assignment_id,)).fetchone()
    if not a or (current_user()["role"] != "ADMIN" and a["user_id"] != current_user()["id"]):
        abort(403)
    start = datetime.fromisoformat(f"{a['duty_date']}T{a['shift_start']}")
    end = datetime.fromisoformat(f"{a['duty_date']}T{a['shift_end']}")
    if end <= start:
        end += timedelta(days=1)
    params = {"action":"TEMPLATE","text":f"{a['session_name']} Duty","dates":f"{start.strftime('%Y%m%dT%H%M%S')}/{end.strftime('%Y%m%dT%H%M%S')}","location":a["building_name"],"details":"RA duty shift"}
    return redirect("https://calendar.google.com/calendar/render?" + urlencode(params))


@app.route("/admin")
@roles("ADMIN")
def admin():
    users = db().execute("SELECT u.*,b.name building_name FROM users u LEFT JOIN buildings b ON b.id=u.building_id ORDER BY u.name").fetchall()
    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()
    return render_template("admin.html", users=users, buildings=buildings)


@app.route("/admin/buildings", methods=["POST"])
@roles("ADMIN")
def add_building():
    require_csrf()
    name = request.form["name"].strip()
    if name:
        db().execute("INSERT OR IGNORE INTO buildings(name) VALUES(?)", (name,))
        db().commit()
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>", methods=["POST"])
@roles("ADMIN")
def edit_user(user_id):
    require_csrf()
    role = request.form["role"]
    building_id = request.form.get("building_id") or None
    if role not in ("RA", "HRA", "ADMIN"):
        abort(400)
    db().execute("UPDATE users SET role=?,building_id=? WHERE id=?", (role, building_id, user_id))
    db().commit()
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
