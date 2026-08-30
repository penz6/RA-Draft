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
        return oauth_failure(
            "Please sign in with a verified RWU Google account (@g.rwu.edu or @rwu.edu)."
        )

    google_sub = str(info["sub"])
    email = info["email"].lower()
    display_name = safe_display_name(
        info.get("name"),
        fallback=info.get("given_name", email),
    )

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    account = conn.execute(
        "SELECT id,disabled FROM users WHERE google_sub=?",
        (google_sub,),
    ).fetchone()
    is_new = False
    claimed_precreated_user = False

    if account:
        if account["disabled"]:
            return disabled_account_failure(conn)
        uid = account["id"]
        conn.execute(
            "UPDATE users SET email=?,name=? WHERE id=?",
            (email, display_name, uid),
        )
    else:
        precreated = conn.execute(
            "SELECT id,disabled,role,building_id FROM users "
            "WHERE email=? COLLATE NOCASE AND google_sub LIKE 'manual:%'",
            (email,),
        ).fetchone()
        if precreated:
            if precreated["disabled"]:
                return disabled_account_failure(conn)
            uid = precreated["id"]
            claimed_precreated_user = True
            conn.execute(
                "UPDATE users SET google_sub=?,name=? WHERE id=?",
                (google_sub, display_name, uid),
            )
            audit(
                "auth.google_link",
                "user",
                uid,
                {"previous_role": precreated["role"]},
                actor_user_id=uid,
            )
        else:
            is_new = True
            existing_email = conn.execute(
                "SELECT id FROM users WHERE email=? COLLATE NOCASE",
                (email,),
            ).fetchone()
            if existing_email:
                conn.rollback()
                return oauth_failure(
                    "That email belongs to another registered account. Contact an administrator."
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
        {
            "new_user": is_new,
            "claimed_precreated_user": claimed_precreated_user,
        },
        actor_user_id=uid,
    )
    conn.commit()
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    """Log out the current user and invalidate the session."""
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
    """Handle first-time building assignment for newly signed up Resident Assistants."""
    user = current_user()
    if not user:
        return redirect(url_for("login"))
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
            "SELECT role,building_id,disabled FROM users WHERE id=?",
            (user["id"],),
        ).fetchone()
        building = conn.execute(
            "SELECT id,name FROM buildings WHERE id=?",
            (building_id,),
        ).fetchone()

        if not account or account["disabled"]:
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
            "WHERE id=? AND role='RA' AND building_id IS NULL AND disabled=0",
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
            "JOIN buildings b ON b.id=u.building_id WHERE u.disabled=0 "
            "ORDER BY b.name,CASE u.role WHEN 'RA' THEN 0 WHEN 'HRA' THEN 1 ELSE 2 END,u.name"
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
        participants = (
            conn.execute(
                "SELECT u.*,b.name building_name FROM users u "
                "JOIN buildings b ON b.id=u.building_id "
                "WHERE u.building_id=? AND u.disabled=0 "
                "ORDER BY CASE u.role WHEN 'RA' THEN 0 WHEN 'HRA' THEN 1 ELSE 2 END,u.name",
                (user["building_id"],),
            ).fetchall()
            if user["role"] == "HRA" and user["building_id"]
            else []
        )

    buildings = conn.execute("SELECT * FROM buildings ORDER BY name").fetchall()
    live_version = dashboard_state_version(user)
    upcoming_shifts = user_upcoming_shifts(user["id"])
    conn.commit()

    return render_template(
        "dashboard_v2.html",
        me=user,
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
