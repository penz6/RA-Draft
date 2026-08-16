from datetime import date

from flask import abort, flash, redirect, request, url_for

from core import (
    app,
    audit,
    clean_single_line,
    current_user,
    db,
    normalize_date_order,
    require_csrf,
    roles,
)


@app.route("/sessions", methods=["POST"])
@roles("HRA", "ADMIN")
def create_session():
    require_csrf()
    initial_user = current_user()
    if not initial_user:
        return redirect(url_for("login"))

    try:
        building_id = int(request.form.get("building_id") or initial_user["building_id"] or 0)
    except (TypeError, ValueError):
        building_id = 0
    if not building_id:
        flash("A building must be assigned or selected before creating a session.", "error")
        return redirect(url_for("dashboard"))

    try:
        name = clean_single_line(request.form.get("name"), max_length=120)
    except ValueError:
        flash("Session name must be 1 to 120 characters with no control characters.", "error")
        return redirect(url_for("dashboard"))

    try:
        start_date = date.fromisoformat(request.form["start_date"])
        end_date = date.fromisoformat(request.form["end_date"])
    except (KeyError, ValueError):
        abort(400)
    if end_date < start_date or (end_date - start_date).days > 400:
        flash("Choose a valid date range of 400 days or less.", "error")
        return redirect(url_for("dashboard"))

    try:
        requested_capacity = int(request.form.get("capacity", 2))
    except (TypeError, ValueError):
        requested_capacity = 2
    if requested_capacity < 1 or requested_capacity > 50:
        flash("People per date must be between 1 and 50.", "error")
        return redirect(url_for("dashboard"))

    try:
        date_order = normalize_date_order(request.form.get("date_order"))
    except ValueError:
        flash("Choose a valid date selection rule.", "error")
        return redirect(url_for("dashboard"))

    raw_participants = request.form.getlist("participant_ids")
    if not raw_participants:
        raw_participants = request.form.getlist("ra_ids")

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    user = current_user()
    if not user or user["role"] not in ("HRA", "ADMIN"):
        conn.rollback()
        abort(403)
    if user["role"] == "HRA" and building_id != user["building_id"]:
        conn.rollback()
        abort(403)
    if not conn.execute(
        "SELECT 1 FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone():
        conn.rollback()
        abort(400)

    selected = []
    seen = set()
    for fallback_order, raw_uid in enumerate(raw_participants, start=1):
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        if uid in seen:
            continue
        seen.add(uid)
        if not conn.execute(
            "SELECT 1 FROM users WHERE id=? AND building_id=? AND disabled=0 "
            "AND role IN ('RA','HRA','ADMIN')",
            (uid, building_id),
        ).fetchone():
            continue
        try:
            order = int(request.form.get(f"order_{uid}") or fallback_order)
        except (TypeError, ValueError):
            order = fallback_order
        if not 1 <= order <= 10000:
            order = fallback_order
        selected.append((order, fallback_order, uid))

    if not selected:
        conn.rollback()
        flash("Select at least one enabled participant for the duty session.", "error")
        return redirect(url_for("dashboard"))

    capacity = min(requested_capacity, len(selected))
    selected.sort(key=lambda item: (item[0], item[1]))

    cur = conn.execute(
        "INSERT INTO draft_sessions("
        "name,building_id,start_date,end_date,capacity,date_order,current_position,created_by"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            name,
            building_id,
            start_date.isoformat(),
            end_date.isoformat(),
            capacity,
            date_order,
            1,
            user["id"],
        ),
    )
    session_id = cur.lastrowid
    participant_ids = []
    for position, (_order, _fallback, uid) in enumerate(selected, start=1):
        participant_ids.append(uid)
        conn.execute(
            "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,?)",
            (session_id, uid, position),
        )
    audit(
        "draft.session.create",
        "session",
        session_id,
        {
            "building_id": building_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "requested_capacity": requested_capacity,
            "capacity": capacity,
            "date_order": date_order,
            "participant_ids": participant_ids,
        },
    )
    conn.commit()
    return redirect(url_for("view_session", session_id=session_id))
