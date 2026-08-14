from collections import defaultdict

from flask import abort, render_template

from core import (
    DATE_KIND_LABELS,
    app,
    assignment_counts,
    calendar_dates,
    calendar_months,
    can_manage,
    can_view_session,
    capacities_for,
    capacity_overrides,
    current_user,
    date_kind_overrides,
    date_kinds_for,
    dates_for,
    filled_slots,
    login_required,
    next_picker,
    ordered_people,
    participant_count,
    selectable_dates,
    selection_phase_label,
    session_complete,
    session_row,
    total_slots,
    db,
)


@app.route("/sessions/<int:session_id>")
@login_required
def view_session(session_id):
    row = session_row(session_id)
    if not row:
        abort(404)
    user = current_user()
    if not can_view_session(user, row):
        abort(403)

    people = ordered_people(session_id)
    picks = db().execute(
        "SELECT a.*,u.name,u.role,o.position FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
        "WHERE a.session_id=? ORDER BY a.duty_date,o.position,a.id",
        (session_id,),
    ).fetchall()

    assignments_by_date = defaultdict(list)
    assignment_user_ids = defaultdict(list)
    for assignment in picks:
        assignments_by_date[assignment["duty_date"]].append(assignment)
        assignment_user_ids[assignment["duty_date"]].append(str(assignment["user_id"]))

    counts = assignment_counts(session_id)
    dates = calendar_dates(row)
    ordered_dates = dates_for(row)
    capacities = capacities_for(row)
    overrides = capacity_overrides(session_id)
    kinds = date_kinds_for(row)
    kind_overrides = date_kind_overrides(session_id)
    current_picker = next_picker(session_id)
    self_selectable = (
        set(selectable_dates(row, user["id"]))
        if current_picker and current_picker["id"] == user["id"]
        else set()
    )
    complete = session_complete(row)
    filled = filled_slots(session_id)
    slots = total_slots(row)

    return render_template(
        "session_v2.html",
        draft=row,
        people=people,
        picks=picks,
        counts=counts,
        dates=dates,
        ordered_dates=ordered_dates,
        months=calendar_months(row),
        capacities=capacities,
        capacity_overrides=overrides,
        date_kinds=kinds,
        date_kind_overrides=kind_overrides,
        date_kind_labels=DATE_KIND_LABELS,
        assignments_by_date=dict(assignments_by_date),
        assignment_user_ids={
            duty_date: ",".join(user_ids)
            for duty_date, user_ids in assignment_user_ids.items()
        },
        selectable_dates=self_selectable,
        next=current_picker,
        can_manage=can_manage(user, row),
        participant_count=participant_count(session_id),
        assigned_count=filled,
        total_slot_count=slots,
        open_slot_count=max(slots - filled, 0),
        available_date_count=sum(
            1
            for duty_date in dates
            if capacities[duty_date] > 0
            and counts.get(duty_date, 0) < capacities[duty_date]
        ),
        schedule_complete=complete,
        selection_phase=selection_phase_label(row),
    )
