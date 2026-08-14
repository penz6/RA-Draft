from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from flask import Response, abort

from core import (
    PUBLIC_HOST,
    app,
    can_view_session,
    current_user,
    db,
    login_required,
    session_row,
)


def ics_escape(text):
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return (
        normalized.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def format_names(names):
    cleaned = [str(name) for name in names if str(name).strip()]
    if not cleaned:
        return "Unassigned"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def assignment_row(assignment_id):
    return db().execute(
        "SELECT a.*,s.name session_name,b.name building_name,u.name user_name "
        "FROM assignments a JOIN draft_sessions s ON s.id=a.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "JOIN users u ON u.id=a.user_id WHERE a.id=?",
        (assignment_id,),
    ).fetchone()


def event_lines(*, uid, duty_date, summary, location, generated_at, description=None):
    start = date.fromisoformat(duty_date)
    end = start + timedelta(days=1)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{ics_escape(uid)}",
        f"DTSTAMP:{generated_at}",
        f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
        f"SUMMARY:{ics_escape(summary)}",
        f"LOCATION:{ics_escape(location)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{ics_escape(description)}")
    lines.extend(["STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"])
    return lines


def calendar_response(events, filename):
    body = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "PRODID:-//RA Draft//Duty Scheduler//EN",
            *events,
            "END:VCALENDAR",
            "",
        ]
    )
    return Response(
        body,
        content_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/calendar/<int:assignment_id>.ics")
@login_required
def calendar_ics(assignment_id):
    row = assignment_row(assignment_id)
    user = current_user()
    if not row or (user["role"] != "ADMIN" and row["user_id"] != user["id"]):
        abort(403)

    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    events = event_lines(
        uid=f"ra-draft-{assignment_id}@{PUBLIC_HOST}",
        duty_date=row["duty_date"],
        summary=f"{row['building_name']}: {row['user_name']}",
        location=row["building_name"],
        generated_at=generated_at,
        description=row["session_name"],
    )
    return calendar_response(events, f"duty-{row['duty_date']}.ics")


@app.route("/calendar/session/<int:session_id>.ics")
@login_required
def session_calendar_ics(session_id):
    row = session_row(session_id)
    user = current_user()
    if not row or not can_view_session(user, row):
        abort(403)

    assignments = db().execute(
        "SELECT a.duty_date,u.name,o.position FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
        "WHERE a.session_id=? ORDER BY a.duty_date,o.position,a.id",
        (session_id,),
    ).fetchall()
    names_by_date = defaultdict(list)
    for assignment in assignments:
        names_by_date[assignment["duty_date"]].append(assignment["name"])

    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    events = []
    for duty_date in sorted(names_by_date):
        events.extend(
            event_lines(
                uid=f"ra-draft-session-{session_id}-{duty_date}@{PUBLIC_HOST}",
                duty_date=duty_date,
                summary=f"{row['building_name']}: {format_names(names_by_date[duty_date])}",
                location=row["building_name"],
                generated_at=generated_at,
            )
        )

    return calendar_response(events, f"duty-session-{session_id}.ics")
