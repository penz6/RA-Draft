"""Per-date weekday/weekend and staffing overrides.

This module is imported before the route modules. It creates the additive
SQLite table and installs date-aware helpers on ``core`` so every picking,
capacity, completion, and rendering path uses the same effective rules.
"""

from datetime import date
import sqlite3

import core
from core import (
    DATE_ORDER_WEEKDAYS_FIRST,
    DATE_ORDER_WEEKENDS_FIRST,
    DB_PATH,
    assignment_counts,
    calendar_dates,
    configure_connection,
    db,
    normalize_date_order,
    session_row,
    user_assignment_dates,
)

DATE_KIND_AUTO = "AUTO"
DATE_KIND_WEEKDAY = "WEEKDAY"
DATE_KIND_WEEKEND = "WEEKEND"
DATE_KIND_NO_DUTY = "NO_DUTY"
DATE_KIND_OVERRIDE_CHOICES = {
    DATE_KIND_WEEKDAY,
    DATE_KIND_WEEKEND,
    DATE_KIND_NO_DUTY,
}
DATE_KIND_FORM_CHOICES = DATE_KIND_OVERRIDE_CHOICES | {DATE_KIND_AUTO}
DATE_KIND_LABELS = {
    DATE_KIND_AUTO: "Calendar default",
    DATE_KIND_WEEKDAY: "Weekday",
    DATE_KIND_WEEKEND: "Weekend",
    DATE_KIND_NO_DUTY: "No one needed",
}

DATE_OVERRIDE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_date_overrides (
  session_id INTEGER NOT NULL REFERENCES draft_sessions(id) ON DELETE CASCADE,
  duty_date TEXT NOT NULL,
  date_kind TEXT NOT NULL
    CHECK(date_kind IN ('WEEKDAY','WEEKEND','NO_DUTY')),
  updated_by INTEGER NOT NULL REFERENCES users(id),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(session_id, duty_date)
);
"""


def _initialize_table():
    conn = configure_connection(sqlite3.connect(DB_PATH, timeout=10))
    conn.executescript(DATE_OVERRIDE_SCHEMA)
    conn.commit()
    conn.close()


_initialize_table()


def _session_id(row):
    try:
        return row["id"]
    except (IndexError, KeyError, TypeError):
        return None


def natural_date_kind(duty_date):
    parsed = date.fromisoformat(str(duty_date))
    return DATE_KIND_WEEKEND if parsed.weekday() >= 5 else DATE_KIND_WEEKDAY


def date_kind_overrides(session_id):
    if session_id is None:
        return {}
    return {
        row["duty_date"]: row["date_kind"]
        for row in db().execute(
            "SELECT duty_date,date_kind FROM session_date_overrides "
            "WHERE session_id=? ORDER BY duty_date",
            (session_id,),
        ).fetchall()
    }


def effective_date_kind(row, duty_date):
    session_id = _session_id(row)
    if session_id is None:
        return natural_date_kind(duty_date)
    override = db().execute(
        "SELECT date_kind FROM session_date_overrides "
        "WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()
    return override["date_kind"] if override else natural_date_kind(duty_date)


def date_kinds_for(row):
    overrides = date_kind_overrides(_session_id(row))
    return {
        duty_date: overrides.get(duty_date, natural_date_kind(duty_date))
        for duty_date in calendar_dates(row)
    }


def dates_for(row):
    kinds = date_kinds_for(row)
    required = [
        date.fromisoformat(duty_date)
        for duty_date in calendar_dates(row)
        if kinds[duty_date] != DATE_KIND_NO_DUTY
    ]
    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        required.sort(
            key=lambda item: (
                kinds[item.isoformat()] != DATE_KIND_WEEKDAY,
                item,
            )
        )
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        required.sort(
            key=lambda item: (
                kinds[item.isoformat()] != DATE_KIND_WEEKEND,
                item,
            )
        )
    else:
        required.sort()
    return [item.isoformat() for item in required]


def capacities_for(row):
    session_id = _session_id(row)
    overrides = core.capacity_overrides(session_id) if session_id is not None else {}
    kinds = date_kinds_for(row)
    return {
        duty_date: (
            0
            if kinds[duty_date] == DATE_KIND_NO_DUTY
            else overrides.get(duty_date, row["capacity"])
        )
        for duty_date in calendar_dates(row)
    }


def effective_capacity(row, duty_date):
    if effective_date_kind(row, duty_date) == DATE_KIND_NO_DUTY:
        return 0
    session_id = _session_id(row)
    if session_id is None:
        return row["capacity"]
    override = db().execute(
        "SELECT capacity FROM session_date_capacities "
        "WHERE session_id=? AND duty_date=?",
        (session_id, duty_date),
    ).fetchone()
    return override["capacity"] if override else row["capacity"]


def total_slots(row):
    return sum(capacities_for(row).values())


def session_complete(row):
    session_id = _session_id(row)
    if session_id is None:
        return False
    counts = assignment_counts(session_id)
    capacities = capacities_for(row)
    return all(
        counts.get(duty_date, 0) >= capacity
        for duty_date, capacity in capacities.items()
    )


def selectable_dates(row, user_id):
    session_id = _session_id(row)
    if session_id is None:
        return []
    counts = assignment_counts(session_id)
    capacities = capacities_for(row)
    kinds = date_kinds_for(row)
    assigned_dates = user_assignment_dates(session_id, user_id)

    globally_open = [
        duty_date
        for duty_date in dates_for(row)
        if capacities[duty_date] > 0
        and counts.get(duty_date, 0) < capacities[duty_date]
    ]

    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        weekday_phase = [
            duty_date
            for duty_date in globally_open
            if kinds[duty_date] == DATE_KIND_WEEKDAY
        ]
        if weekday_phase:
            globally_open = weekday_phase
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        weekend_phase = [
            duty_date
            for duty_date in globally_open
            if kinds[duty_date] == DATE_KIND_WEEKEND
        ]
        if weekend_phase:
            globally_open = weekend_phase

    return [
        duty_date
        for duty_date in globally_open
        if duty_date not in assigned_dates
    ]


def selection_phase_label(row):
    session_id = _session_id(row)
    if session_id is None:
        return ""
    counts = assignment_counts(session_id)
    capacities = capacities_for(row)
    kinds = date_kinds_for(row)
    open_dates = [
        duty_date
        for duty_date in calendar_dates(row)
        if capacities[duty_date] > 0
        and counts.get(duty_date, 0) < capacities[duty_date]
    ]
    order = normalize_date_order(row["date_order"])
    if order == DATE_ORDER_WEEKDAYS_FIRST:
        if any(kinds[value] == DATE_KIND_WEEKDAY for value in open_dates):
            return "Weekday dates are open; weekends unlock after weekday slots fill."
        if open_dates:
            return "Weekend dates are now open."
    elif order == DATE_ORDER_WEEKENDS_FIRST:
        if any(kinds[value] == DATE_KIND_WEEKEND for value in open_dates):
            return "Weekend dates are open; weekdays unlock after weekend slots fill."
        if open_dates:
            return "Weekday dates are now open."
    elif open_dates:
        return "Any required date with an open slot can be selected."
    return "Every required duty slot is filled."


def next_picker(session_id):
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


# Install one effective implementation for every route and test that imports
# these helpers from core after portal_app begins loading route modules.
core.DATE_KIND_AUTO = DATE_KIND_AUTO
core.DATE_KIND_WEEKDAY = DATE_KIND_WEEKDAY
core.DATE_KIND_WEEKEND = DATE_KIND_WEEKEND
core.DATE_KIND_NO_DUTY = DATE_KIND_NO_DUTY
core.DATE_KIND_FORM_CHOICES = DATE_KIND_FORM_CHOICES
core.DATE_KIND_LABELS = DATE_KIND_LABELS
core.date_kind_overrides = date_kind_overrides
core.effective_date_kind = effective_date_kind
core.date_kinds_for = date_kinds_for
core.dates_for = dates_for
core.capacities_for = capacities_for
core.effective_capacity = effective_capacity
core.total_slots = total_slots
core.session_complete = session_complete
core.selectable_dates = selectable_dates
core.selection_phase_label = selection_phase_label
core.next_picker = next_picker
