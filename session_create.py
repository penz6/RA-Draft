from datetime import date

from flask import abort, flash, redirect, request, url_for

from core import (
    app,
    audit,
    clean_single_line,
    current_user,
    db,
    normalize_date_order,
    normalize_time,
    require_csrf,
    roles,
)


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
    if not db().execute(
        "SELECT 1 FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone():
        abort(400)

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
        capacity = int(request.form.get("capacity", 2))
    except (TypeError, ValueError):
        capacity = 2
    if capacity < 1 or capacity > 50:
        flash("Participants per date must be between 1 and 50.", "error")
        return redirect(url_for("dashboard"))

    try:
        shift_start = normalize_time(request.form.get("shift_start", "19:00"))
        shift_end = normalize_time(request.form.get("shift_end", "07:00"))
        date_order = normalize_date_order(request.form.get("date_order"))
    except ValueError:
        flash("Duty hours or date ordering were invalid.", "error")
        return redirect(url_for("dashboard"))

    raw_participants = request.form.getlist("participant_ids")
    if not raw_participants:
        raw_participants = request.form.getlist("ra_ids")

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
        if not db().execute(
            "SELECT 1 FROM users WHERE id=? AND building_id=? "
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
        flash("Select at least one participant for the duty session.", "error")
        return redirect(url_for("dashboard"))

    selected.sort(key=lambda item: (item[0], item[1]))
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "INSERT INTO draft_sessions("
        "name,building_id,start_date,end_date,shift_start,shift_end,capacity,date_order,created_by"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            name,
            building_id,
            start_date.isoformat(),
            end_date.isoformat(),
            shift_start,
            shift_end,
            capacity,
            date_order,
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
            "shift_start": shift_start,
            "shift_end": shift_end,
            "capacity": capacity,
            "date_order": date_order,
            "participant_ids": participant_ids,
        },
    )
    conn.commit()
    return redirect(url_for("view_session", session_id=session_id))
