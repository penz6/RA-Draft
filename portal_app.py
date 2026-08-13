import os

from flask import flash, redirect, render_template, session, url_for

from core import (
    app,
    audit,
    current_user,
    db,
    google_identity_allowed,
    login_required,
    oauth,
    require_csrf,
)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    db().execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.route("/login")
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if not getattr(oauth, "google", None):
        flash("Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", "error")
        return render_template("index.html")
    return oauth.google.authorize_redirect(
        url_for("auth_callback", _external=True),
        hd="*",
    )


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    info = token.get("userinfo") or oauth.google.userinfo(token=token)
    email = (info.get("email") or "").strip().lower()
    google_sub = (info.get("sub") or "").strip()

    if not google_identity_allowed(info):
        session.clear()
        flash("Sign in with a verified RWU Google Workspace account.", "error")
        return redirect(url_for("index"))

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE google_sub=?", (google_sub,)).fetchone()
    is_new = user is None

    if user:
        email_owner = conn.execute(
            "SELECT id FROM users WHERE email=? AND id<>?",
            (email, user["id"]),
        ).fetchone()
        if email_owner:
            session.clear()
            flash("This RWU email is already linked to another account. Ask an admin to resolve it.", "error")
            return redirect(url_for("index"))
        conn.execute(
            "UPDATE users SET email=?, name=? WHERE id=?",
            (email, info.get("name") or email, user["id"]),
        )
        uid = user["id"]
    else:
        email_owner = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if email_owner:
            session.clear()
            flash("This RWU email is already linked to another account. Ask an admin to resolve it.", "error")
            return redirect(url_for("index"))

        admin_emails = {
            item.strip().lower()
            for item in os.environ.get("ADMIN_EMAILS", "").split(",")
            if item.strip()
        }
        role = "ADMIN" if email in admin_emails else "RA"
        cur = conn.execute(
            "INSERT INTO users(google_sub,email,name,role) VALUES(?,?,?,?)",
            (google_sub, email, info.get("name") or email, role),
        )
        uid = cur.lastrowid
        audit(
            "auth.user_created",
            "user",
            uid,
            {"role": role},
            actor_user_id=uid,
        )

    session.clear()
    session["uid"] = uid
    session.permanent = True
    audit("auth.login", "user", uid, {"new_user": is_new}, actor_user_id=uid)
    conn.commit()
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
