# Install the additive account-status migration and current_user enforcement
# before importing current_user into this module or any route modules.
import core  # noqa: F401
import account_status  # noqa: F401
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
    user_upcoming_shifts,
)
import secrets

from authlib.integrations.base_client.errors import OAuthError
from flask import abort, flash, redirect, render_template, request, session, url_for
from requests.exceptions import RequestException


@app.route("/")
def index():
    """Render the public landing page."""
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    """Health check endpoint for container orchestrators and VPS monitors."""
    db().execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.route("/login")
def login():
    """Initiate Google OpenID Connect OAuth login flow."""
    if current_user():
        return redirect(url_for("dashboard"))
    return oauth.google.authorize_redirect(
        url_for("auth_callback", _external=True),
        hd="*",
        nonce=secrets.token_urlsafe(32),
    )


def oauth_failure(message):
    """Clear session and redirect to landing page with an error notification."""
    session.clear()
    flash(message, "error")
    return redirect(url_for("index"))


def disabled_account_failure(conn):
    """Roll back transaction and notify user that their account is disabled."""
    conn.rollback()
    return oauth_failure(
        "This Duty Picking account is disabled. Contact an administrator if access should be restored."
    )


@app.route("/auth/callback")
def auth_callback():
    """Handle Google OAuth return callback, authenticate user, and sync profile."""
    try:
        token = oauth.google.authorize_access_token()
        info = token.get("userinfo")
        if not info:
            info = oauth.google.userinfo(token=token)
    except (OAuthError, RequestException, ValueError):
        return oauth_failure("Google login failed. Try again.")

    if not google_identity_allowed(info):
        return oauth_failure("Please sign in with a verified RWU Google account (@g.rwu.edu or @rwu.edu).")

    email = info["email"].lower()
    subject = str(info["sub"])
    name = safe_display_name(
        info.get("name") or info.get("given_name"),
        email.split("@")[0],
    )
    bootstrap_admin = email in ADMIN_EMAILS

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    user = conn.execute(
        "SELECT * FROM users WHERE google_sub=?",
        (subject,),
    ).fetchone()

    if user:
        if bool(user["disabled"]):
            return disabled_account_failure(conn)
        updates = []
        parameters = []
        if name and name != user["name"]:
            updates.append("name=?")
            parameters.append(name)
        if email != user["email"]:
            updates.append("email=?")
            parameters.append(email)
        if bootstrap_admin and user["role"] != "ADMIN":
            updates.append("role='ADMIN'")
        if updates:
            parameters.append(user["id"])
            conn.execute(
                f"UPDATE users SET {','.join(updates)} WHERE id=?",
                tuple(parameters),
            )
            audit(
                "auth.profile_synced",
                "user",
                user["id"],
                {"updated_fields": updates},
                actor_user_id=user["id"],
            )
            conn.commit()
            user = conn.execute(
                "SELECT * FROM users WHERE id=?",
                (user["id"],),
            ).fetchone()
        else:
            conn.commit()
        session["uid"] = user["id"]
        return redirect(url_for("dashboard"))

    precreated = conn.execute(
        "SELECT * FROM users WHERE email=? COLLATE NOCASE AND google_sub LIKE 'manual:%'",
        (email,),
    ).fetchone()
    if precreated:
        if bool(precreated["disabled"]):
            return disabled_account_failure(conn)
        new_role = "ADMIN" if bootstrap_admin else precreated["role"]
        conn.execute(
            "UPDATE users SET google_sub=?,name=?,role=? WHERE id=?",
            (subject, name, new_role, precreated["id"]),
        )
        audit(
            "auth.google_linked",
            "user",
            precreated["id"],
            {
                "previous_placeholder": precreated["google_sub"],
                "role": new_role,
                "building_id": precreated["building_id"],
            },
            actor_user_id=precreated["id"],
        )
        conn.commit()
        session["uid"] = precreated["id"]
        return redirect(url_for("dashboard"))

    conflict = conn.execute(
        "SELECT id FROM users WHERE email=? COLLATE NOCASE",
        (email,),
    ).fetchone()
    if conflict:
        conn.rollback()
        return oauth_failure(
            "An account with this email already exists under a different identity."
        )

    initial_role = "ADMIN" if bootstrap_admin else "RA"
    cur = conn.execute(
        "INSERT INTO users(google_sub,email,name,role) VALUES(?,?,?,?)",
        (subject, email, name, initial_role),
    )
    user_id = cur.lastrowid
    audit(
        "auth.user_created",
        "user",
        user_id,
        {"email": email, "role": initial_role, "bootstrap_admin": bootstrap_admin},
        actor_user_id=user_id,
    )
    conn.commit()

    session["uid"] = user_id
    session["show_role_help"] = True
    if initial_role == "RA":
        return redirect(url_for("onboarding"))
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    """Log out the current user and invalidate the session."""
    require_csrf()
    uid = session.get("uid")
    if isinstance(uid, int):
        audit("auth.logout", "user", uid, actor_user_id=uid)
    session.clear()
    return redirect(url_for("index"))


@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    """Handle first-time building assignment for newly signed up Resident Assistants."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["role"] != "RA" or user["building_id"] is not None:
        return redirect(url_for("dashboard"))

    conn = db()
    buildings = conn.execute("SELECT * FROM buildings ORDER BY name").fetchall()

    if request.method == "POST":
        require_csrf()
        try:
            building_id = int(request.form.get("building_id", 0))
        except (TypeError, ValueError):
            building_id = 0

        conn.execute("BEGIN IMMEDIATE")
        user = current_user()
        if not user:
            conn.rollback()
            return redirect(url_for("login"))
        if user["role"] != "RA" or user["building_id"] is not None:
            conn.rollback()
            return redirect(url_for("dashboard"))

        target = conn.execute(
            "SELECT id,name FROM buildings WHERE id=?",
            (building_id,),
        ).fetchone()
        if not target:
            conn.rollback()
            flash("Please choose a valid building.", "error")
            return render_template(
                "onboarding.html",
                buildings=buildings,
                user=user,
            )

        conn.execute(
            "UPDATE users SET building_id=? WHERE id=?",
            (building_id, user["id"]),
        )
        audit(
            "user.onboard_building",
            "building",
            building_id,
            {"building_name": target["name"]},
            actor_user_id=user["id"],
        )
        conn.commit()
        flash(f"You're set for {target['name']}.", "success")
        return redirect(url_for("dashboard"))

    return render_template("onboarding.html", buildings=buildings, user=user)


@app.route("/dashboard")
@login_required
def dashboard():
    """Render the authenticated dashboard showing building draft sessions and actions."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["role"] == "RA" and user["building_id"] is None:
        return redirect(url_for("onboarding"))

    conn = db()
    conn.execute("BEGIN")
    user = current_user()
    if not user:
        conn.rollback()
        return redirect(url_for("login"))
    if user["role"] == "RA" and user["building_id"] is None:
        conn.rollback()
        return redirect(url_for("onboarding"))

    if user["role"] == "ADMIN":
        sessions = conn.execute(
            "SELECT s.*,b.name building_name FROM draft_sessions s "
            "JOIN buildings b ON b.id=s.building_id "
            "ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END, s.created_at DESC"
        ).fetchall()
        participants = conn.execute(
            "SELECT u.*,b.name building_name FROM users u "
            "LEFT JOIN buildings b ON b.id=u.building_id "
            "WHERE u.disabled=0 ORDER BY b.name,u.name"
        ).fetchall()
    elif user["role"] == "HRA":
        sessions = (
            conn.execute(
                "SELECT s.*,b.name building_name FROM draft_sessions s "
                "JOIN buildings b ON b.id=s.building_id WHERE s.building_id=? "
                "ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END, s.created_at DESC",
                (user["building_id"],),
            ).fetchall()
            if user["building_id"]
            else []
        )
        participants = (
            conn.execute(
                "SELECT u.*,b.name building_name FROM users u "
                "JOIN buildings b ON b.id=u.building_id "
                "WHERE u.building_id=? AND u.disabled=0 ORDER BY u.name",
                (user["building_id"],),
            ).fetchall()
            if user["building_id"]
            else []
        )
    else:
        sessions = (
            conn.execute(
                "SELECT s.*,b.name building_name FROM draft_sessions s "
                "JOIN buildings b ON b.id=s.building_id WHERE s.building_id=? "
                "ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END, s.created_at DESC",
                (user["building_id"],),
            ).fetchall()
            if user["building_id"]
            else []
        )
        participants = []

    buildings = conn.execute("SELECT * FROM buildings ORDER BY name").fetchall()
    live_version = dashboard_state_version(user)
    upcoming_shifts = user_upcoming_shifts(user["id"])
    conn.commit()

    return render_template(
        "dashboard_v2.html",
        sessions=sessions,
        buildings=buildings,
        participants=participants,
        upcoming_shifts=upcoming_shifts,
        live_version=live_version,
        auto_open_help=bool(session.pop("show_role_help", False)),
    )


@app.route("/dashboard/live-fragments")
@login_required
def dashboard_live_fragments():
    """Return updated dashboard HTML fragments for background SSE live updates."""
    user = current_user()
    if not user:
        abort(401)
    if user["role"] == "RA" and user["building_id"] is None:
        return redirect(url_for("onboarding"))

    conn = db()
    conn.execute("BEGIN")
    user = current_user()
    if not user:
        conn.rollback()
        abort(401)

    if user["role"] == "ADMIN":
        sessions = conn.execute(
            "SELECT s.*,b.name building_name FROM draft_sessions s "
            "JOIN buildings b ON b.id=s.building_id "
            "ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END, s.created_at DESC"
        ).fetchall()
    else:
        sessions = (
            conn.execute(
                "SELECT s.*,b.name building_name FROM draft_sessions s "
                "JOIN buildings b ON b.id=s.building_id WHERE s.building_id=? "
                "ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END, s.created_at DESC",
                (user["building_id"],),
            ).fetchall()
            if user["building_id"]
            else []
        )

    version = dashboard_state_version(user)
    try:
        fragments = {
            "sessions": render_template(
                "dashboard_sessions_fragment.html",
                me=user,
                sessions=sessions,
            ),
        }
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return {"version": version, "fragments": fragments}


import round_robin  # noqa: E402,F401
from live_updates import dashboard_state_version  # noqa: E402
import admin_routes  # noqa: E402,F401
import calendar_routes  # noqa: E402,F401
import hra_assign  # noqa: E402,F401
import hra_pause  # noqa: E402,F401
import session_choose  # noqa: E402,F401
import session_create  # noqa: E402,F401
import session_status  # noqa: E402,F401
import session_view  # noqa: E402,F401
import session_routes  # noqa: E402,F401
