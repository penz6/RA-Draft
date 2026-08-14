from flask import abort, render_template

from core import (
    app,
    can_manage,
    capacities_for,
    capacity_overrides,
    current_user,
    dates_for,
    db,
    login_required,
    next_picker,
    ordered_people,
    session_row,
)


@app.route("/sessions/<int:session_id>")
@login_required
def view_session(session_id):
    row = session_row(session_id)
    if not row:
        abort(404)
    user = current_user()
    if user["role"] != "ADMIN" and user["building_id"] != row["building_id"]:
        abort(403)

    people = ordered_people(session_id)
    picks = db().execute(
        "SELECT a.*,u.name,u.role FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.session_id=? ORDER BY a.duty_date,u.name",
        (session_id,),
    ).fetchall()
    counts = {
        record["duty_date"]: record["n"]
        for record in db().execute(
            "SELECT duty_date,COUNT(*) n FROM assignments "
            "WHERE session_id=? GROUP BY duty_date",
            (session_id,),
        ).fetchall()
    }
    dates = dates_for(row)
    capacities = capacities_for(row)
    overrides = capacity_overrides(session_id)
    current_picker = next_picker(session_id)
    mine = next((item for item in picks if item["user_id"] == user["id"]), None)
    available_dates = sum(
        1 for item in dates if counts.get(item, 0) < capacities[item]
    )

    return render_template(
        "session_v2.html",
        draft=row,
        people=people,
        picks=picks,
        counts=counts,
        dates=dates,
        capacities=capacities,
        capacity_overrides=overrides,
        next=current_picker,
        can_manage=can_manage(user, row),
        mine=mine,
        participant_count=len(people),
        assigned_count=len(picks),
        available_date_count=available_dates,
    )
