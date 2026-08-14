import secrets

from authlib.integrations.base_client.errors import OAuthError
from flask import flash, redirect, render_template, session, url_for
from requests.exceptions import RequestException

from core import (
    ADMIN_EMAILS,
    app,
    audit,
    current_user,
    db,
    google_identity_allowed,
    login_required,
    oauth,
    require_csrf,
    safe_display_name,
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
    return oauth.google.authorize_redirect(
        url_for("auth_callback", _external=True),
        hd="*",
        nonce=secrets.token_urlsafe(32),
    )


def oauth_failure(message):
    session.clear()
    flash(message, "error")
    return redirect(url_for("index"))


@app.route("/auth/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
        info = token.get("userinfo")
        if not isinstance(info, dict):
            raise ValueError("Google did not return a valid OpenID profile.")
    except (OAuthError, RequestException, TypeError, ValueError, KeyError):
        return oauth_failure(
            "Google sign-in could not be completed. Return to the portal and try again."
        )

    email = (info.get("email") or "").strip().lower()
    google_sub = str(info.get("sub") or "").strip()
    display_name = safe_display_name(info.get("name"), email)

    if not google_identity_allowed(info):
        return oauth_failure(
            "Sign in with a verified @g.rwu.edu or @rwu.edu Google Workspace account."
        )

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    user = conn.execute(
        "SELECT * FROM users WHERE google_sub=?",
        (google_sub,),
    ).fetchone()
    is_new = user is None

    if user:
        email_owner = conn.execute(
            "SELECT id FROM users WHERE email=? AND id<>?",
            (email, user["id"]),
        ).fetchone()
        if email_owner:
            conn.rollback()
            return oauth_failure(
                "This RWU email is already linked to another account. Ask an admin to resolve it."
            )
        conn.execute(
            "UPDATE users SET email=?, name=? WHERE id=?",
            (email, display_name, user["id"]),
        )
        uid = user["id"]
    else:
        email_owner = conn.execute(
            "SELECT id FROM users WHERE email=?",
            (email,),
        ).fetchone()
        if email_owner:
            conn.rollback()
            return oauth_failure(
                "This RWU email is already linked to another account. Ask an admin to resolve it."
            )

        role = "ADMIN" if email in ADMIN_EMAILS else "RA"
        cur = conn.execute(
            "INSERT INTO users(google_sub,email,name,role) VALUES(?,?,?,?)",
            (google_sub, email, display_name, role),
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
    audit(
        "auth.login",
        "user",
        uid,
        {"new_user": is_new},
        actor_user_id=uid,
    )
    conn.commit()
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    require_csrf()
    uid = session.get("uid")
    if isinstance(uid, int):
        audit("auth.logout", "user", uid, actor_user_id=uid)
        db().commit()
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "ADMIN":
        sessions = db().execute(
            "SELECT s.*,b.name building_name FROM draft_sessions s "
            "JOIN buildings b ON b.id=s.building_id "
            "ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END, s.created_at DESC"
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
                "ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END, s.created_at DESC",
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
    return render_template(
        "dashboard_v2.html",
        sessions=sessions,
        buildings=buildings,
        ras=ras,
    )


import admin_routes  # noqa: E402,F401
import calendar_routes  # noqa: E402,F401
import hra_assign  # noqa: E402,F401
import hra_pause  # noqa: E402,F401
import session_choose  # noqa: E402,F401
import session_create  # noqa: E402,F401
import session_status  # noqa: E402,F401
import session_view  # noqa: E402,F401
