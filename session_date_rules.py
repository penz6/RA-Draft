from flask import abort, flash, redirect, request, url_for

from core import (
    app,
    audit,
    can_manage,
    current_user,
    dates_for,
    db,
    normalize_date_order,
    require_csrf,
    roles,
    session_row,
)


@app.route("/sessions/<int:session_id>/date-order", methods=["POST"])
@roles("HRA", "ADMIN")
def update_date_order(session_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or not can_manage(user, row):
        abort(403)

    try:
        date_order = normalize_date_order(request.form.get("date_order"))
    except ValueError:
        abort(400)

    if date_order != row["date_order"]:
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE draft_sessions SET date_order=? WHERE id=?",
            (date_order, session_id),
        )
        audit(
            "draft.session.date_order",
            "session",
            session_id,
            {"old_date_order": row["date_order"], "new_date_order": date_order},
        )
        conn.commit()
        flash("Date ordering updated.", "success")
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/date-capacity", methods=["POST"])
@roles("HRA", "ADMIN")
def update_date_capacity(session_id):
    require_csrf()
    row = session_row(session_id)
    user = current_user()
    if not row or not can_manage(user, row):
        abort(403)

    duty_date = request.form.get("duty_date", "")
    if duty_date not in dates_for(row):
        abort(400)

    raw_capacity = request.form.get("capacity", "").strip()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT capacity FROM session_date_capacities "
        "WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()

    if not raw_capacity:
        if existing:
            conn.execute(
                "DELETE FROM session_date_capacities "
                "WHERE session_id=? AND duty_date=?",
                (session_id, duty_date),
            )
            audit(
                "draft.session.date_capacity_reset",
                "session",
                session_id,
                {
                    "duty_date": duty_date,
                    "old_capacity": existing["capacity"],
                    "default_capacity": row["capacity"],
                },
            )
            conn.commit()
            flash("That date now uses the session default capacity.", "success")
        else:
            conn.rollback()
        return redirect(url_for("view_session", session_id=session_id))

    try:
        capacity = int(raw_capacity)
    except ValueError:
        conn.rollback()
        abort(400)
    if not 1 <= capacity <= 50:
        conn.rollback()
        flash("Date capacity must be between 1 and 50.", "error")
        return redirect(url_for("view_session", session_id=session_id))

    assigned = conn.execute(
        "SELECT COUNT(*) n FROM assignments WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()["n"]
    if capacity < assigned:
        conn.rollback()
        flash(
            f"That date already has {assigned} assigned; its capacity cannot be lower.",
            "error",
        )
        return redirect(url_for("view_session", session_id=session_id))

    conn.execute(
        "INSERT INTO session_date_capacities(session_id,duty_date,capacity,updated_by) "
        "VALUES(?,?,?,?) "
        "ON CONFLICT(session_id,duty_date) DO UPDATE SET "
        "capacity=excluded.capacity,updated_by=excluded.updated_by,"
        "updated_at=CURRENT_TIMESTAMP",
        (session_id, duty_date, capacity, user["id"]),
    )
    audit(
        "draft.session.date_capacity_set",
        "session",
        session_id,
        {
            "duty_date": duty_date,
            "old_capacity": existing["capacity"] if existing else row["capacity"],
            "new_capacity": capacity,
            "is_override": capacity != row["capacity"],
        },
    )
    conn.commit()
    flash("Date capacity updated.", "success")
    return redirect(url_for("view_session", session_id=session_id))
