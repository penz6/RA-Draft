"""iCalendar export routes for session schedules and individual duty assignments."""

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
    """Escape special characters in text according to RFC 5545 specifications."""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return (
        normalized.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold_ical_line(line):
    """Fold an iCalendar content line to RFC 5545's 75-octet line length limit."""
    text = str(line)
    if not text:
        return [""]
    parts = []
    current = ""
    first = True
    for character in text:
        limit = 75 if first else 74
        if current and len((current + character).encode("utf-8")) > limit:
            parts.append(current if first else f" {current}")
            current = character
            first = False
        else:
            current += character
    parts.append(current if first else f" {current}")
    return parts


def first_name(value):
    """Extract first name from full name or return 'Unassigned' fallback."""
    cleaned = " ".join(str(value or "").split())
    return cleaned.split(" ", 1)[0] if cleaned else "Unassigned"


def format_names(names):
    """Format a list of participant names as a human-readable string (e.g. 'Alice & Bob')."""
    cleaned = [first_name(name) for name in names if str(name).strip()]
    if not cleaned:
        return "Unassigned"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} & {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} & {cleaned[-1]}"


def calendar_summary(building_name, names):
    """Generate a calendar event title combining building name and scheduled assignees."""
    building = str(building_name).strip()
    prefix = building if building.endswith("*") else f"{building}*"
    return f"{prefix} {format_names(names)}"


def event_lines(*, uid, duty_date, summary, location, generated_at, description=None, shift_times=False):
    """Build VEVENT lines for an all-day or 7pm-8am timed shift duty event."""
    start = date.fromisoformat(duty_date)
    end = start + timedelta(days=1)
    if shift_times:
        # 7:00 PM (19:00) on duty date to 8:00 AM (08:00) on the following morning
        dtstart_line = f"DTSTART:{start.strftime('%Y%m%d')}T190000"
        dtend_line = f"DTEND:{end.strftime('%Y%m%d')}T080000"
    else:
        # All-day calendar event
        dtstart_line = f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}"
        dtend_line = f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{ics_escape(uid)}",
        f"DTSTAMP:{generated_at}",
        dtstart_line,
        dtend_line,
        f"SUMMARY:{ics_escape(summary)}",
        f"LOCATION:{ics_escape(location)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{ics_escape(description)}")
    lines.extend(["STATUS:CONFIRMED", "TRANSP:OPAQUE" if shift_times else "TRANSP:TRANSPARENT", "END:VEVENT"])
    return lines


def calendar_response(events, filename):
    """Wrap calendar VEVENT lines into a RFC 5545 compliant HTTP response."""
    raw_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "PRODID:-//RA Draft//Duty Scheduler//EN",
        *events,
        "END:VCALENDAR",
    ]
    folded_lines = [
        folded
        for line in raw_lines
        for folded in fold_ical_line(line)
    ]
    body = "\r\n".join([*folded_lines, ""])
    return Response(
        body,
        content_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )





@app.route("/calendar/session/<int:session_id>.ics")
@login_required
def session_calendar_ics(session_id):
    """Export the complete session schedule as an iCalendar feed."""
    row = session_row(session_id)
    if not row:
        abort(404)
    user = current_user()
    if not can_view_session(user, row):
        abort(403)

    assignments = db().execute(
        "SELECT a.duty_date,u.name,o.position FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "LEFT JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
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
                summary=calendar_summary(
                    row["building_name"],
                    names_by_date[duty_date],
                ),
                location=row["building_name"],
                generated_at=generated_at,
                description=f"Duty Session: {row['name']}",
                shift_times=False,
            )
        )

    return calendar_response(events, f"duty-session-{session_id}.ics")


@app.route("/calendar/session/<int:session_id>/my-calendar.ics")
@login_required
def my_session_calendar_ics(session_id):
    """Export only the signed-in user's assigned duty shifts (7pm-8am) for this session."""
    user = current_user()
    return user_session_calendar_ics(session_id, user["id"])


@app.route("/calendar/session/<int:session_id>/user/<int:user_id>.ics")
@login_required
def user_session_calendar_ics(session_id, user_id):
    """Export a specific participant's duty shifts (7pm to 8am next morning) for this session."""
    row = session_row(session_id)
    if not row:
        abort(404)
    viewer = current_user()
    if not can_view_session(viewer, row):
        abort(403)
    if viewer["role"] != "ADMIN" and viewer["id"] != user_id:
        if viewer["role"] != "HRA" or viewer["building_id"] != row["building_id"]:
            abort(403)

    target_user = db().execute(
        "SELECT id, name FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if not target_user:
        abort(404)

    assignments = db().execute(
        "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=? ORDER BY duty_date",
        (session_id, user_id),
    ).fetchall()

    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    events = []
    for assignment in assignments:
        duty_date = assignment["duty_date"]
        events.extend(
            event_lines(
                uid=f"ra-draft-user-{user_id}-session-{session_id}-{duty_date}@{PUBLIC_HOST}",
                duty_date=duty_date,
                summary=f"{row['building_name']} RA Duty",
                location=row["building_name"],
                generated_at=generated_at,
                description=f"Duty Shift for {target_user['name']} in {row['name']}",
                shift_times=True,
            )
        )

    return calendar_response(events, f"my-duty-session-{session_id}.ics")
