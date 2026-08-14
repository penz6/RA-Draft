from flask import abort, flash, redirect, render_template, request, url_for

import date_exceptions  # noqa: F401
from core import app, audit, clean_single_line, db, require_csrf, roles


@app.route("/admin")
@roles("ADMIN")
def admin():
    users = db().execute(
        "SELECT u.*,b.name building_name FROM users u "
        "LEFT JOIN buildings b ON b.id=u.building_id ORDER BY u.name"
    ).fetchall()
    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()
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


@app.route("/admin/users/<int:user_id>", methods=["POST"])
@roles("ADMIN")
def edit_user(user_id):
    require_csrf()
    role = request.form.get("role", "")
    if role not in ("RA", "HRA", "ADMIN"):
        abort(400)

    existing = db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not existing:
        abort(404)

    raw_building = request.form.get("building_id") or ""
    if raw_building:
        try:
            building_id = int(raw_building)
        except ValueError:
            abort(400)
        if not db().execute(
            "SELECT 1 FROM buildings WHERE id=?",
            (building_id,),
        ).fetchone():
            abort(400)
    else:
        building_id = None

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    if existing["role"] == "ADMIN" and role != "ADMIN":
        admin_count = conn.execute(
            "SELECT COUNT(*) n FROM users WHERE role='ADMIN'"
        ).fetchone()["n"]
        if admin_count <= 1:
            conn.rollback()
            flash("You cannot demote the last admin.", "error")
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
