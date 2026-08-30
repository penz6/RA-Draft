from flask import abort, flash, redirect, request, url_for

from core import (
    app,
    audit,
    calendar_dates,
    can_manage,
    current_user,
    db,
    normalize_date_order,
    participant_count,
    require_csrf,
    roles,
    session_row,
)
from date_exceptions import (
    DATE_KIND_AUTO,
    DATE_KIND_FORM_CHOICES,
    DATE_KIND_LABELS,
    DATE_KIND_NO_DUTY,
    effective_date_kind,
)


def _locked_manager_session(conn, session_id):
    user = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_manage(user, row):
        conn.rollback()
        abort(403)
    return user, row


@app.route("/sessions/<int:session_id>/date-order", methods=["POST"])
@roles("HRA", "ADMIN")
def update_date_order(session_id):
    require_csrf()
    try:
        date_order = normalize_date_order(request.form.get("date_order"))
    except ValueError:
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    _user, row = _locked_manager_session(conn, session_id)
    if date_order == row["date_order"]:
        conn.rollback()
        return redirect(url_for("view_session", session_id=session_id))

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
    flash("Date selection rule updated.", "success")
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/date-kind", methods=["POST"])
@roles("HRA", "ADMIN")
def update_date_kind(session_id):
    require_csrf()
    duty_date = request.form.get("duty_date", "")
    date_kind = request.form.get("date_kind", "").strip().upper()
    if date_kind not in DATE_KIND_FORM_CHOICES:
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    user, row = _locked_manager_session(conn, session_id)
    if duty_date not in calendar_dates(row):
        conn.rollback()
        abort(400)

    existing = conn.execute(
        "SELECT date_kind FROM session_date_overrides "
        "WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()
    old_kind = existing["date_kind"] if existing else DATE_KIND_AUTO

    if date_kind == old_kind:
        conn.rollback()
        return redirect(url_for("view_session", session_id=session_id))

    assigned = conn.execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()["n"]
    if date_kind == DATE_KIND_NO_DUTY and assigned:
        conn.rollback()
        flash(
            f"Remove the {assigned} existing assignment"
            f"{'s' if assigned != 1 else ''} before marking this date as no one needed.",
            "error",
        )
        return redirect(url_for("view_session", session_id=session_id))

    removed_capacity = None
    if date_kind == DATE_KIND_AUTO:
        conn.execute(
            "DELETE FROM session_date_overrides WHERE session_id=? AND duty_date=?",
            (session_id, duty_date),
        )
    else:
        conn.execute(
            "INSERT INTO session_date_overrides(session_id,duty_date,date_kind,updated_by) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(session_id,duty_date) DO UPDATE SET "
            "date_kind=excluded.date_kind,updated_by=excluded.updated_by,"
            "updated_at=CURRENT_TIMESTAMP",
            (session_id, duty_date, date_kind, user["id"]),
        )
        if date_kind == DATE_KIND_NO_DUTY:
            capacity_row = conn.execute(
                "SELECT capacity FROM session_date_capacities "
                "WHERE session_id=? AND duty_date=?",
                (session_id, duty_date),
            ).fetchone()
            removed_capacity = capacity_row["capacity"] if capacity_row else None
            conn.execute(
                "DELETE FROM session_date_capacities "
                "WHERE session_id=? AND duty_date=?",
                (session_id, duty_date),
            )

    audit(
        "draft.session.date_kind",
        "session",
        session_id,
        {
            "duty_date": duty_date,
            "old_date_kind": old_kind,
            "new_date_kind": date_kind,
            "removed_capacity_override": removed_capacity,
        },
    )
    conn.commit()

    if date_kind == DATE_KIND_AUTO:
        message = "That date now follows its normal calendar weekday/weekend type."
    elif date_kind == DATE_KIND_NO_DUTY:
        message = "That date is now marked as no one needed."
    else:
        message = f"That date is now treated as a {DATE_KIND_LABELS[date_kind].lower()}."
    flash(message, "success")
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/date-capacity", methods=["POST"])
@roles("HRA", "ADMIN")
def update_date_capacity(session_id):
    require_csrf()
    duty_date = request.form.get("duty_date", "")
    raw_capacity = request.form.get("capacity", "").strip()

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    user, row = _locked_manager_session(conn, session_id)
    if duty_date not in calendar_dates(row):
        conn.rollback()
        abort(400)
    if effective_date_kind(row, duty_date) == DATE_KIND_NO_DUTY:
        conn.rollback()
        flash(
            "Mark this date as a weekday, weekend, or calendar default before setting capacity.",
            "error",
        )
        return redirect(url_for("view_session", session_id=session_id))

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

    participants = participant_count(session_id)
    if capacity > participants:
        conn.rollback()
        flash(
            "Date capacity cannot exceed the number of session participants.",
            "error",
        )
        return redirect(url_for("view_session", session_id=session_id))

    assigned = conn.execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()["n"]
    if capacity < assigned:
        conn.rollback()
        flash(
            f"That date already has {assigned} assigned; its capacity cannot be lower.",
            "error",
        )
        return redirect(url_for("view_session", session_id=session_id))

    # Storing an override equal to the session default is misleading and can
    # make the UI label a normal date as overridden. Normalize it to no row.
    if capacity == row["capacity"]:
        if existing:
            conn.execute(
                "DELETE FROM session_date_capacities WHERE session_id=? AND duty_date=?",
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
        else:
            conn.rollback()
        flash("That date now uses the session default capacity.", "success")
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
            "is_override": True,
        },
    )
    conn.commit()
    flash("Date capacity updated.", "success")
    return redirect(url_for("view_session", session_id=session_id))
