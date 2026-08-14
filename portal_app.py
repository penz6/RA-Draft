import secrets

from authlib.integrations.base_client.errors import OAuthError
from flask import abort, flash, redirect, render_template, request, session, url_for
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
    session["show_role_help"] = True
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


@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    user = current_user()
    if user["role"] != "RA" or user["building_id"] is not None:
        return redirect(url_for("dashboard"))

    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()

    if request.method == "POST":
        require_csrf()
        try:
            building_id = int(request.form.get("building_id", ""))
        except (TypeError, ValueError):
            abort(400)

        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute(
            "SELECT role,building_id FROM users WHERE id=?",
            (user["id"],),
        ).fetchone()
        building = conn.execute(
            "SELECT id,name FROM buildings WHERE id=?",
            (building_id,),
        ).fetchone()

        if not account:
            conn.rollback()
            session.clear()
            return redirect(url_for("login"))
        if account["role"] != "RA" or account["building_id"] is not None:
            conn.rollback()
            return redirect(url_for("dashboard"))
        if not building:
            conn.rollback()
            abort(400)

        updated = conn.execute(
            "UPDATE users SET building_id=? "
            "WHERE id=? AND role='RA' AND building_id IS NULL",
            (building_id, user["id"]),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return redirect(url_for("dashboard"))

        audit(
            "profile.building.select",
            "user",
            user["id"],
            {"building_id": building_id},
        )
        conn.commit()
        flash(f"Building set to {building['name']}.", "success")
        return redirect(url_for("dashboard"))

    return render_template(
        "onboarding.html",
        buildings=buildings,
        auto_open_help=bool(session.pop("show_role_help", False)),
    )


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user["role"] == "RA" and user["building_id"] is None:
        return redirect(url_for("onboarding"))

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
        auto_open_help=bool(session.pop("show_role_help", False)),
    )


import admin_routes  # noqa: E402,F401
import calendar_routes  # noqa: E402,F401
import hra_assign  # noqa: E402,F401
import hra_pause  # noqa: E402,F401
import session_choose  # noqa: E402,F401
import session_create  # noqa: E402,F401
import session_status  # noqa: E402,F401
import session_view  # noqa: E402,F401
