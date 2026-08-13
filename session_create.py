from flask import abort, flash, redirect, request, url_for
from core import app, current_user, db, require_csrf, roles

@app.route("/sessions", methods=["POST"])
@roles("HRA", "ADMIN")
def create_session():
    require_csrf()
    user = current_user()
    try:
        building_id = int(request.form.get("building_id") or user["building_id"] or 0)
    except (TypeError, ValueError):
        building_id = 0
    if not building_id:
        flash("A building must be assigned or selected before creating a session.", "error")
        return redirect(url_for("dashboard"))
    if user["role"] == "HRA" and building_id != user["building_id"]:
        abort(403)
    if not db().execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        abort(400)
    start, end = request.form["start_date"], request.form["end_date"]
    if end < start:
        flash("End date must be on or after start date.", "error")
        return redirect(url_for("dashboard"))
    try:
        capacity = max(1, int(request.form.get("capacity", 2)))
    except (TypeError, ValueError):
        capacity = 2

    selected = []
    seen = set()
    for fallback_order, raw_uid in enumerate(request.form.getlist("ra_ids"), start=1):
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        if not db().execute(
            "SELECT 1 FROM users WHERE id=? AND building_id=? AND role='RA'",
            (uid, building_id),
        ).fetchone():
            continue
        try:
            order = int(request.form.get(f"order_{uid}") or fallback_order)
        except (TypeError, ValueError):
            order = fallback_order
        selected.append((order, fallback_order, uid))

    if not selected:
        flash("Select at least one RA for the draft session.", "error")
        return redirect(url_for("dashboard"))
    selected.sort(key=lambda item: (item[0], item[1]))
    cur = db().execute(
        "INSERT INTO draft_sessions(name,building_id,start_date,end_date,shift_start,shift_end,capacity,created_by) VALUES(?,?,?,?,?,?,?,?)",
        (request.form["name"].strip(), building_id, start, end, request.form.get("shift_start", "19:00"), request.form.get("shift_end", "07:00"), capacity, user["id"]),
    )
    session_id = cur.lastrowid
    for position, (_order, _fallback, uid) in enumerate(selected, start=1):
        db().execute(
            "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,?)",
            (session_id, uid, position),
        )
    db().commit()
    return redirect(url_for("view_session", session_id=session_id))
