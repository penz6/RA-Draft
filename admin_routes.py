import secrets

from flask import abort, flash, redirect, render_template, request, url_for

import date_exceptions  # noqa: F401
from core import (
    ADMIN_EMAILS,
    allowed_email,
    app,
    audit,
    clean_single_line,
    current_user,
    db,
    require_csrf,
    roles,
)


def normalize_admin_email(value):
    email = str(value or "").strip().lower()
    if (
        not email
        or len(email) > 254
        or not email.isascii()
        or email.count("@") != 1
        or any(ord(character) < 33 or ord(character) > 126 for character in email)
        or not email.split("@", 1)[0]
        or not allowed_email(email)
    ):
        raise ValueError("Use a valid @g.rwu.edu or @rwu.edu email address.")
    return email


def form_building_id(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    try:
        building_id = int(raw)
    except ValueError as exc:
        raise ValueError("Invalid building.") from exc
    if not db().execute(
        "SELECT 1 FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone():
        raise ValueError("Invalid building.")
    return building_id


def _require_locked_admin(conn):
    actor = current_user()
    if not actor or actor["role"] != "ADMIN":
        conn.rollback()
        abort(403)
    return actor


def _building_still_exists(conn, building_id):
    return building_id is None or bool(
        conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone()
    )


@app.route("/admin")
@roles("ADMIN")
def admin():
    users = db().execute(
        "SELECT u.*,b.name building_name,"
        "CASE WHEN u.google_sub LIKE 'manual:%' THEN 1 ELSE 0 END pending_google "
        "FROM users u LEFT JOIN buildings b ON b.id=u.building_id "
        "ORDER BY u.name,u.email"
    ).fetchall()
    buildings = db().execute(
        "SELECT b.*,"
        "(SELECT COUNT(*) FROM users u WHERE u.building_id=b.id) user_count,"
        "(SELECT COUNT(*) FROM draft_sessions s WHERE s.building_id=b.id) session_count "
        "FROM buildings b ORDER BY b.name"
    ).fetchall()
    audit_rows = db().execute(
        "SELECT a.*,u.name actor_name FROM audit_log a "
        "LEFT JOIN users u ON u.id=a.actor_user_id ORDER BY a.id DESC LIMIT 100"
    ).fetchall()
    return render_template(
        "admin.html",
        users=users,
        buildings=buildings,
        audit_rows=audit_rows,
    )


@app.route("/admin/buildings", methods=["POST"])
@roles("ADMIN")
def add_building():
    require_csrf()
    try:
        name = clean_single_line(request.form.get("name"), max_length=80)
    except ValueError:
        flash("Building name must be 1 to 80 characters with no control characters.", "error")
        return redirect(url_for("admin"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    _require_locked_admin(conn)
    existing = conn.execute(
        "SELECT id FROM buildings WHERE name=? COLLATE NOCASE",
        (name,),
    ).fetchone()
    if existing:
        conn.rollback()
        flash("That building already exists.", "error")
        return redirect(url_for("admin"))

    cur = conn.execute("INSERT INTO buildings(name) VALUES(?)", (name,))
    audit("admin.building.create", "building", cur.lastrowid, {"name": name})
    conn.commit()
    flash("Building added.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/buildings/<int:building_id>/rename", methods=["POST"])
@roles("ADMIN")
def rename_building(building_id):
    require_csrf()
    try:
        name = clean_single_line(request.form.get("name"), max_length=80)
    except ValueError:
        flash("Building name must be 1 to 80 characters with no control characters.", "error")
        return redirect(url_for("admin"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    _require_locked_admin(conn)
    existing = conn.execute(
        "SELECT * FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone()
    if not existing:
        conn.rollback()
        abort(404)
    duplicate = conn.execute(
        "SELECT id FROM buildings WHERE name=? COLLATE NOCASE AND id<>?",
        (name, building_id),
    ).fetchone()
    if duplicate:
        conn.rollback()
        flash("Another building already uses that name.", "error")
        return redirect(url_for("admin"))

    if name != existing["name"]:
        conn.execute(
            "UPDATE buildings SET name=? WHERE id=?",
            (name, building_id),
        )
        audit(
            "admin.building.rename",
            "building",
            building_id,
            {"old_name": existing["name"], "new_name": name},
        )
        conn.commit()
        flash("Building renamed.", "success")
    else:
        conn.rollback()
    return redirect(url_for("admin"))


@app.route("/admin/buildings/<int:building_id>/delete", methods=["POST"])
@roles("ADMIN")
def delete_building(building_id):
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    _require_locked_admin(conn)
    existing = conn.execute(
        "SELECT * FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone()
    if not existing:
        conn.rollback()
        abort(404)

    session_count = conn.execute(
        "SELECT COUNT(*) n FROM draft_sessions WHERE building_id=?",
        (building_id,),
    ).fetchone()["n"]
    if session_count:
        conn.rollback()
        flash(
            "This building has duty-session history and cannot be deleted. Rename it instead.",
            "error",
        )
        return redirect(url_for("admin"))

    user_count = conn.execute(
        "SELECT COUNT(*) n FROM users WHERE building_id=?",
        (building_id,),
    ).fetchone()["n"]
    conn.execute(
        "UPDATE users SET building_id=NULL WHERE building_id=?",
        (building_id,),
    )
    conn.execute("DELETE FROM buildings WHERE id=?", (building_id,))
    audit(
        "admin.building.delete",
        "building",
        building_id,
        {"name": existing["name"], "users_unassigned": user_count},
    )
    conn.commit()
    flash(
        f"Building deleted. {user_count} user{'s were' if user_count != 1 else ' was'} unassigned.",
        "success",
    )
    return redirect(url_for("admin"))


@app.route("/admin/users", methods=["POST"])
@roles("ADMIN")
def add_user():
    require_csrf()
    try:
        name = clean_single_line(request.form.get("name"), max_length=120)
        email = normalize_admin_email(request.form.get("email"))
        building_id = form_building_id(request.form.get("building_id"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin"))

    role = request.form.get("role", "")
    if role not in ("RA", "HRA", "ADMIN"):
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    _require_locked_admin(conn)
    if not _building_still_exists(conn, building_id):
        conn.rollback()
        flash("That building no longer exists. Refresh and try again.", "error")
        return redirect(url_for("admin"))
    existing = conn.execute(
        "SELECT id FROM users WHERE email=? COLLATE NOCASE",
        (email,),
    ).fetchone()
    if existing:
        conn.rollback()
        flash("A user with that email already exists.", "error")
        return redirect(url_for("admin"))

    placeholder_sub = f"manual:{secrets.token_urlsafe(24)}"
    cur = conn.execute(
        "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
        (placeholder_sub, email, name, role, building_id),
    )
    audit(
        "admin.user.create",
        "user",
        cur.lastrowid,
        {
            "email": email,
            "role": role,
            "building_id": building_id,
            "awaiting_google_link": True,
        },
    )
    conn.commit()
    flash(
        "User created. Their verified RWU Google account will link automatically on first sign-in.",
        "success",
    )
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>", methods=["POST"])
@roles("ADMIN")
def edit_user(user_id):
    require_csrf()
    role = request.form.get("role", "")
    if role not in ("RA", "HRA", "ADMIN"):
        abort(400)

    try:
        building_id = form_building_id(request.form.get("building_id"))
    except ValueError:
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    _require_locked_admin(conn)
    existing = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not existing:
        conn.rollback()
        abort(404)
    if not _building_still_exists(conn, building_id):
        conn.rollback()
        flash("That building no longer exists. Refresh and try again.", "error")
        return redirect(url_for("admin"))

    if existing["role"] == "ADMIN" and role != "ADMIN":
        admin_count = conn.execute(
            "SELECT COUNT(*) n FROM users WHERE role='ADMIN'"
        ).fetchone()["n"]
        if admin_count <= 1:
            conn.rollback()
            flash("You cannot demote the last admin.", "error")
            return redirect(url_for("admin"))

    if role == existing["role"] and building_id == existing["building_id"]:
        conn.rollback()
        flash("No access changes were needed.", "success")
        return redirect(url_for("admin"))

    conn.execute(
        "UPDATE users SET role=?,building_id=? WHERE id=?",
        (role, building_id, user_id),
    )
    audit(
        "admin.user.update",
        "user",
        user_id,
        {
            "old_role": existing["role"],
            "new_role": role,
            "old_building_id": existing["building_id"],
            "new_building_id": building_id,
        },
    )
    conn.commit()
    flash("User access updated.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@roles("ADMIN")
def delete_user(user_id):
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    actor = _require_locked_admin(conn)
    existing = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not existing:
        conn.rollback()
        abort(404)
    if actor["id"] == user_id:
        conn.rollback()
        flash("You cannot delete your own signed-in account.", "error")
        return redirect(url_for("admin"))
    if existing["email"].lower() in ADMIN_EMAILS:
        conn.rollback()
        flash(
            "Remove this email from ADMIN_EMAILS before deleting the bootstrap account.",
            "error",
        )
        return redirect(url_for("admin"))

    if existing["role"] == "ADMIN":
        admin_count = conn.execute(
            "SELECT COUNT(*) n FROM users WHERE role='ADMIN'"
        ).fetchone()["n"]
        if admin_count <= 1:
            conn.rollback()
            flash("You cannot delete the last admin.", "error")
            return redirect(url_for("admin"))

    has_schedule_history = conn.execute(
        "SELECT ("
        "EXISTS(SELECT 1 FROM draft_sessions WHERE created_by=?) OR "
        "EXISTS(SELECT 1 FROM session_order WHERE user_id=?) OR "
        "EXISTS(SELECT 1 FROM assignments WHERE user_id=?) OR "
        "EXISTS(SELECT 1 FROM assignments WHERE created_by=?) OR "
        "EXISTS(SELECT 1 FROM session_deferrals WHERE user_id=?) OR "
        "EXISTS(SELECT 1 FROM session_deferrals WHERE deferred_by=?) OR "
        "EXISTS(SELECT 1 FROM session_date_capacities WHERE updated_by=?) OR "
        "EXISTS(SELECT 1 FROM session_date_overrides WHERE updated_by=?)"
        ") blocked",
        (user_id,) * 8,
    ).fetchone()["blocked"]
    if has_schedule_history:
        conn.rollback()
        flash(
            "This user is referenced by duty-session history and cannot be deleted without damaging the schedule.",
            "error",
        )
        return redirect(url_for("admin"))

    conn.execute(
        "UPDATE audit_log SET actor_user_id=NULL WHERE actor_user_id=?",
        (user_id,),
    )
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    audit(
        "admin.user.delete",
        "user",
        user_id,
        {"email": existing["email"], "name": existing["name"], "role": existing["role"]},
    )
    conn.commit()
    flash("User deleted.", "success")
    return redirect(url_for("admin"))
