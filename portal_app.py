from flask import render_template

from core import app, current_user, db, login_required


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    db().execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "ADMIN":
        sessions = db().execute(
            "SELECT s.*,b.name building_name FROM draft_sessions s "
            "JOIN buildings b ON b.id=s.building_id ORDER BY s.created_at DESC"
        ).fetchall()
        ras = db().execute(
            "SELECT u.*,b.name building_name FROM users u "
            "JOIN buildings b ON b.id=u.building_id WHERE u.role='RA' "
            "ORDER BY b.name,u.name"
        ).fetchall()
    else:
        sessions = (
            db().execute(
                "SELECT s.*,b.name building_name FROM draft_sessions s "
                "JOIN buildings b ON b.id=s.building_id WHERE s.building_id=? "
                "ORDER BY s.created_at DESC",
                (user["building_id"],),
            ).fetchall()
            if user["building_id"]
            else []
        )
        ras = (
            db().execute(
                "SELECT * FROM users WHERE building_id=? AND role='RA' ORDER BY name",
                (user["building_id"],),
            ).fetchall()
            if user["role"] == "HRA" and user["building_id"]
            else []
        )
    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()
    return render_template("dashboard_v2.html", sessions=sessions, buildings=buildings, ras=ras)


import admin_routes  # noqa: E402,F401
import calendar_routes  # noqa: E402,F401
import hra_assign  # noqa: E402,F401
import hra_pause  # noqa: E402,F401
import route_alias  # noqa: E402,F401
import session_choose  # noqa: E402,F401
import session_create  # noqa: E402,F401
import session_status  # noqa: E402,F401
import session_view  # noqa: E402,F401
