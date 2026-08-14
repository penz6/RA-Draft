"""Small polling endpoints used to keep dashboards and sessions current."""

import hashlib
import json

from flask import abort, request

from core import (
    app,
    can_view_session,
    current_user,
    db,
    login_required,
    session_row,
)


def _digest(payload):
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rows(query, parameters, columns):
    return [
        [row[column] for column in columns]
        for row in db().execute(query, parameters).fetchall()
    ]


def dashboard_state_version(user):
    columns = (
        "id",
        "name",
        "building_id",
        "start_date",
        "end_date",
        "capacity",
        "date_order",
        "current_position",
        "status",
        "created_at",
    )
    if user["role"] == "ADMIN":
        sessions = _rows(
            "SELECT id,name,building_id,start_date,end_date,capacity,date_order,"
            "current_position,status,created_at FROM draft_sessions ORDER BY id",
            (),
            columns,
        )
    elif user["building_id"] is not None:
        sessions = _rows(
            "SELECT id,name,building_id,start_date,end_date,capacity,date_order,"
            "current_position,status,created_at FROM draft_sessions "
            "WHERE building_id=? ORDER BY id",
            (user["building_id"],),
            columns,
        )
    else:
        sessions = []

    return _digest(
        {
            "user": [user["id"], user["role"], user["building_id"]],
            "sessions": sessions,
        }
    )


def session_state_version(row):
    session_id = row["id"]
    return _digest(
        {
            "session": [
                row["id"],
                row["name"],
                row["building_id"],
                row["start_date"],
                row["end_date"],
                row["capacity"],
                row["date_order"],
                row["current_position"],
                row["status"],
            ],
            "order": _rows(
                "SELECT user_id,position FROM session_order "
                "WHERE session_id=? ORDER BY position",
                (session_id,),
                ("user_id", "position"),
            ),
            "assignments": _rows(
                "SELECT id,user_id,duty_date,created_by,created_at FROM assignments "
                "WHERE session_id=? ORDER BY id",
                (session_id,),
                ("id", "user_id", "duty_date", "created_by", "created_at"),
            ),
            "deferrals": _rows(
                "SELECT user_id,deferred_by,created_at FROM session_deferrals "
                "WHERE session_id=? ORDER BY user_id",
                (session_id,),
                ("user_id", "deferred_by", "created_at"),
            ),
            "capacities": _rows(
                "SELECT duty_date,capacity,updated_by,updated_at "
                "FROM session_date_capacities WHERE session_id=? ORDER BY duty_date",
                (session_id,),
                ("duty_date", "capacity", "updated_by", "updated_at"),
            ),
            "date_treatments": _rows(
                "SELECT duty_date,date_kind,updated_by,updated_at "
                "FROM session_date_overrides WHERE session_id=? ORDER BY duty_date",
                (session_id,),
                ("duty_date", "date_kind", "updated_by", "updated_at"),
            ),
        }
    )


@app.route("/live-state")
@login_required
def live_state():
    user = current_user()
    raw_session_id = request.args.get("session_id")
    if raw_session_id is None:
        return {"version": dashboard_state_version(user)}

    try:
        session_id = int(raw_session_id)
    except (TypeError, ValueError):
        abort(400)

    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)
    return {"version": session_state_version(row)}
