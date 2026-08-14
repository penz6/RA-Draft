"""Round-robin turn selection rules.

This module is loaded before the route modules and installs the global-phase
implementations on ``core`` so existing imports remain compatible.
"""

from datetime import date

import core
from core import (
    DATE_ORDER_WEEKDAYS_FIRST,
    DATE_ORDER_WEEKENDS_FIRST,
    assignment_counts,
    capacities_for,
    dates_for,
    db,
    normalize_date_order,
    session_complete,
    session_row,
    user_assignment_dates,
)


def selectable_dates(row, user_id):
    """Return dates this user may select in the session's current phase."""
    counts = assignment_counts(row["id"])
    capacities = capacities_for(row)
    assigned_dates = user_assignment_dates(row["id"], user_id)
    ordered_dates = dates_for(row)

    globally_open = [
        duty_date
        for duty_date in ordered_dates
        if counts.get(duty_date, 0) < capacities[duty_date]
    ]

    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        weekday_phase = [
            duty_date
            for duty_date in globally_open
            if date.fromisoformat(duty_date).weekday() < 5
        ]
        if weekday_phase:
            globally_open = weekday_phase
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        weekend_phase = [
            duty_date
            for duty_date in globally_open
            if date.fromisoformat(duty_date).weekday() >= 5
        ]
        if weekend_phase:
            globally_open = weekend_phase

    return [
        duty_date for duty_date in globally_open if duty_date not in assigned_dates
    ]


def next_picker(session_id):
    """Find the next active participant who can pick in the global phase."""
    row = session_row(session_id)
    if not row or session_complete(row):
        return None

    active = db().execute(
        "SELECT u.*,o.position FROM session_order o "
        "JOIN users u ON u.id=o.user_id "
        "LEFT JOIN session_deferrals d "
        "ON d.session_id=o.session_id AND d.user_id=o.user_id "
        "WHERE o.session_id=? AND d.user_id IS NULL ORDER BY o.position",
        (session_id,),
    ).fetchall()
    if not active:
        return None

    start_position = row["current_position"] or 1
    rotated = [item for item in active if item["position"] >= start_position]
    rotated.extend(item for item in active if item["position"] < start_position)
    for participant in rotated:
        if selectable_dates(row, participant["id"]):
            return participant
    return None


# Compatibility aliases: route modules and older tests import these names from
# core. Installing them here keeps one effective implementation at runtime.
core.selectable_dates = selectable_dates
core.next_picker = next_picker
