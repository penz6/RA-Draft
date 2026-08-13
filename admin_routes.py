from flask import abort, redirect, render_template, request, url_for
from core import app, db, require_csrf, roles

@app.route("/admin")
@roles("ADMIN")
def admin():
    users = db().execute(
        "SELECT u.*,b.name building_name FROM users u LEFT JOIN buildings b ON b.id=u.building_id ORDER BY u.name"
    ).fetchall()
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
    db().execute(
        "UPDATE users SET role=?,building_id=? WHERE id=?",
        (role, building_id, user_id),
    )
    db().commit()
    return redirect(url_for("admin"))
