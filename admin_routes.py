"""Administrative management routes for users, buildings, and audit logs."""

import secrets

from flask import abort, flash, redirect, render_template, request, url_for

from core import (
    ADMIN_EMAILS,
    ROLES,
    app,
    audit,
    clean_single_line,
    current_user,
    db,
    require_csrf,
    roles,
)


@app.route("/admin")
@roles("ADMIN")
def admin():
    """Render the primary administrative control center."""
    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()
    users = (
        db()
        .execute(
            "SELECT u.*, b.name building_name FROM users u "
            "LEFT JOIN buildings b ON b.id=u.building_id "
            "ORDER BY u.disabled, CASE u.role WHEN 'ADMIN' THEN 0 WHEN 'HRA' THEN 1 ELSE 2 END, u.name"
        )
        .fetchall()
    )
    raw_logs = (
        db()
        .execute(
            "SELECT a.*, u.name actor_name, u.email actor_email FROM audit_log a "
            "LEFT JOIN users u ON u.id=a.actor_user_id "
            "ORDER BY a.id DESC LIMIT 40"
        )
        .fetchall()
    )
    return render_template(
        "admin.html",
        buildings=buildings,
        users=users,
        logs=raw_logs,
        roles=ROLES,
        admin_emails=ADMIN_EMAILS,
    )


@app.route("/admin/buildings/create", methods=["POST"])
@roles("ADMIN")
def admin_create_building():
    """Create a new residence building entity."""
    require_csrf()
    name = clean_single_line(request.form.get("name", ""), max_len=80)
    if not name:
        flash("Building name is required.", "error")
        return redirect(url_for("admin"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT id FROM buildings WHERE lower(name)=?",
        (name.lower(),),
    ).fetchone()
    if existing:
        conn.rollback()
        flash(f"Building '{name}' already exists.", "error")
        return redirect(url_for("admin"))

    cur = conn.execute("INSERT INTO buildings(name) VALUES(?)", (name,))
    audit(
        "admin.building.create",
        "building",
        cur.lastrowid,
        {"name": name},
    )
    conn.commit()
    flash(f"Building '{name}' created.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/buildings/<int:building_id>/rename", methods=["POST"])
@roles("ADMIN")
def admin_rename_building(building_id):
    """Rename an existing residence building entity."""
    require_csrf()
    name = clean_single_line(request.form.get("name", ""), max_len=80)
    if not name:
        flash("Building name is required.", "error")
        return redirect(url_for("admin"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    building = conn.execute(
        "SELECT * FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone()
    if not building:
        conn.rollback()
        abort(404)

    duplicate = conn.execute(
        "SELECT id FROM buildings WHERE lower(name)=? AND id!=?",
        (name.lower(), building_id),
    ).fetchone()
    if duplicate:
        conn.rollback()
        flash(f"Building name '{name}' is already in use.", "error")
        return redirect(url_for("admin"))

    old_name = building["name"]
    conn.execute(
        "UPDATE buildings SET name=? WHERE id=?",
        (name, building_id),
    )
    audit(
        "admin.building.rename",
        "building",
        building_id,
        {"old_name": old_name, "new_name": name},
    )
    conn.commit()
    flash(f"Renamed '{old_name}' to '{name}'.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/buildings/<int:building_id>/delete", methods=["POST"])
@roles("ADMIN")
def admin_delete_building(building_id):
    """Delete a building entity if no sessions are linked to it."""
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    building = conn.execute(
        "SELECT * FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone()
    if not building:
        conn.rollback()
        abort(404)

    session_count = conn.execute(
        "SELECT count(*) total FROM draft_sessions WHERE building_id=?",
        (building_id,),
    ).fetchone()["total"]
    if session_count > 0:
        conn.rollback()
        flash(
            f"Cannot delete '{building['name']}' because it has {session_count} draft session(s).",
            "error",
        )
        return redirect(url_for("admin"))

    user_count = conn.execute(
        "SELECT count(*) total FROM users WHERE building_id=?",
        (building_id,),
    ).fetchone()["total"]
    conn.execute(
        "UPDATE users SET building_id=NULL WHERE building_id=?",
        (building_id,),
    )
    conn.execute("DELETE FROM buildings WHERE id=?", (building_id,))
    audit(
        "admin.building.delete",
        "building",
        building_id,
        {
            "name": building["name"],
            "unassigned_users": user_count,
        },
    )
    conn.commit()
    flash(
        f"Building '{building['name']}' deleted. {user_count} user(s) need reassignment.",
        "success",
    )
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/update", methods=["POST"])
@roles("ADMIN")
def admin_update_user(user_id):
    """Update role, building assignment, and active status for a user."""
    require_csrf()
    target_role = request.form.get("role", "").strip().upper()
    building_raw = request.form.get("building_id", "").strip()
    disabled_raw = request.form.get("disabled", "").strip()

    if target_role not in ROLES:
        flash("Invalid role selection.", "error")
        return redirect(url_for("admin"))

    building_id = None
    if building_raw:
        try:
            building_id = int(building_raw)
        except ValueError:
            flash("Invalid building selected.", "error")
            return redirect(url_for("admin"))

    target_disabled = 1 if disabled_raw in ("1", "true", "on") else 0

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    target_user = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if not target_user:
        conn.rollback()
        abort(404)

    if building_id is not None:
        b_check = conn.execute(
            "SELECT id FROM buildings WHERE id=?",
            (building_id,),
        ).fetchone()
        if not b_check:
            conn.rollback()
            flash("Selected building does not exist.", "error")
            return redirect(url_for("admin"))

    admin = current_user()
    if target_user["id"] == admin["id"]:
        if target_role != "ADMIN":
            conn.rollback()
            flash("You cannot demote your own account.", "error")
            return redirect(url_for("admin"))
        if target_disabled:
            conn.rollback()
            flash("You cannot disable your own account.", "error")
            return redirect(url_for("admin"))

    email = target_user["email"].lower()
    if email in ADMIN_EMAILS and (target_role != "ADMIN" or target_disabled):
        conn.rollback()
        flash(
            f"{target_user['email']} is configured in ADMIN_EMAILS and must remain an active Admin.",
            "error",
        )
        return redirect(url_for("admin"))

    old_values = {
        "role": target_user["role"],
        "building_id": target_user["building_id"],
        "disabled": target_user["disabled"],
    }
    conn.execute(
        "UPDATE users SET role=?, building_id=?, disabled=? WHERE id=?",
        (target_role, building_id, target_disabled, user_id),
    )
    audit(
        "admin.user.update",
        "user",
        user_id,
        {
            "old": old_values,
            "new": {
                "role": target_role,
                "building_id": building_id,
                "disabled": target_disabled,
            },
        },
    )
    conn.commit()
    flash(f"User {target_user['name']} updated.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@roles("ADMIN")
def admin_delete_user(user_id):
    """Delete a user account and clean up draft participation records."""
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    target_user = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if not target_user:
        conn.rollback()
        abort(404)

    admin = current_user()
    if target_user["id"] == admin["id"]:
        conn.rollback()
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin"))

    email = target_user["email"].lower()
    if email in ADMIN_EMAILS:
        conn.rollback()
        flash(
            f"{target_user['email']} is configured in ADMIN_EMAILS and cannot be deleted.",
            "error",
        )
        return redirect(url_for("admin"))

    created_sessions = conn.execute(
        "SELECT count(*) total FROM draft_sessions WHERE created_by=?",
        (user_id,),
    ).fetchone()["total"]
    if created_sessions > 0:
        conn.rollback()
        flash(
            f"Cannot delete {target_user['name']} because they created {created_sessions} session(s). "
            "Disable their account instead.",
            "error",
        )
        return redirect(url_for("admin"))

    deleted_assignments = conn.execute(
        "SELECT count(*) total FROM assignments WHERE user_id=?",
        (user_id,),
    ).fetchone()["total"]
    conn.execute("DELETE FROM assignments WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM session_order WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    audit(
        "admin.user.delete",
        "user",
        user_id,
        {
            "name": target_user["name"],
            "email": target_user["email"],
            "role": target_user["role"],
            "removed_assignments": deleted_assignments,
        },
    )
    conn.commit()
    flash(
        f"User {target_user['name']} ({target_user['email']}) was deleted.",
        "success",
    )
    return redirect(url_for("admin"))
