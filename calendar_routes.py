"""Routes and generators for RFC 5545 iCalendar (.ics) exports."""

from datetime import date, datetime, timedelta
import secrets

from flask import Response, abort

from core import (
    app,
    can_view_session,
    current_user,
    db,
    login_required,
    session_row,
)


def _ics_escape(text):
    """Escape text for iCalendar properties."""
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _generate_session_ics(session_id):
    """Generate RFC 5545 iCalendar content for an entire draft session (aggregate)."""
    row = session_row(session_id)
    if not row:
        return None

    assignments = (
        db()
        .execute(
            "SELECT a.duty_date, u.name, u.email FROM assignments a "
            "JOIN users u ON u.id=a.user_id "
            "WHERE a.session_id=? ORDER BY a.duty_date, u.name",
            (session_id,),
        )
        .fetchall()
    )

    by_date = {}
    for a in assignments:
        by_date.setdefault(a["duty_date"], []).append(a)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RA Duty Picking//Duty Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(row['name'])} - Duty Schedule",
        f"X-WR-CALDESC:Duty assignments for {_ics_escape(row['building_name'])}",
    ]

    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for duty_date_str, ras in by_date.items():
        dt = date.fromisoformat(duty_date_str)
        next_day = dt + timedelta(days=1)
        dtstart = dt.strftime("%Y%m%d")
        dtend = next_day.strftime("%Y%m%d")
        ra_names = ", ".join(ra["name"] for ra in ras)
        uid = f"session-{session_id}-{dtstart}@raduty"

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now_stamp}",
                f"DTSTART;VALUE=DATE:{dtstart}",
                f"DTEND;VALUE=DATE:{dtend}",
                f"SUMMARY:{_ics_escape(row['building_name'])} RA Duty: {_ics_escape(ra_names)}",
                f"DESCRIPTION:Assigned RAs:\\n"
                + "\\n".join(f"- {_ics_escape(ra['name'])} ({_ics_escape(ra['email'])})" for ra in ras),
                f"LOCATION:{_ics_escape(row['building_name'])}",
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _generate_user_ics(session_id, user_id):
    """Generate RFC 5545 iCalendar content for a specific user's assigned duty shifts (7pm to 8am next day)."""
    row = session_row(session_id)
    if not row:
        return None

    target_user = (
        db()
        .execute("SELECT * FROM users WHERE id=?", (user_id,))
        .fetchone()
    )
    if not target_user:
        return None

    assignments = (
        db()
        .execute(
            "SELECT a.id, a.duty_date FROM assignments a "
            "WHERE a.session_id=? AND a.user_id=? ORDER BY a.duty_date",
            (session_id, user_id),
        )
        .fetchall()
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RA Duty Picking//My Duty Shifts//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(target_user['name'])} - {_ics_escape(row['name'])} Duty",
        f"X-WR-CALDESC:Personal duty shifts for {_ics_escape(target_user['name'])} in {_ics_escape(row['building_name'])}",
    ]

    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for a in assignments:
        duty_date_str = a["duty_date"]
        start_date = date.fromisoformat(duty_date_str)
        end_date = start_date + timedelta(days=1)
        dtstart = f"{start_date.strftime('%Y%m%d')}T190000"
        dtend = f"{end_date.strftime('%Y%m%d')}T080000"
        uid = f"shift-{session_id}-{a['id']}-{start_date.strftime('%Y%m%d')}@raduty"

        co_workers = (
            db()
            .execute(
                "SELECT u.name, u.email FROM assignments a2 "
                "JOIN users u ON u.id=a2.user_id "
                "WHERE a2.session_id=? AND a2.duty_date=? AND a2.user_id!=? "
                "ORDER BY u.name",
                (session_id, duty_date_str, user_id),
            )
            .fetchall()
        )
        if co_workers:
            partner_desc = "\\nCo-duty partners:\\n" + "\\n".join(
                f"- {_ics_escape(cw['name'])} ({_ics_escape(cw['email'])})" for cw in co_workers
            )
        else:
            partner_desc = "\\nSole RA on duty"

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now_stamp}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend}",
                f"SUMMARY:{_ics_escape(row['building_name'])} RA Duty",
                f"DESCRIPTION:RA Duty Shift for {_ics_escape(target_user['name'])} (7:00 PM - 8:00 AM).{partner_desc}",
                f"LOCATION:{_ics_escape(row['building_name'])}",
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.route("/calendar/session/<int:session_id>.ics")
@login_required
def session_calendar_ics(session_id):
    """Serve full-session calendar export in .ics format."""
    user = current_user()
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)

    ics_content = _generate_session_ics(session_id)
    if not ics_content:
        abort(404)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in row["name"])
    filename = f"{safe_name}_duty_schedule.ics"

    return Response(
        ics_content,
        mimetype="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/calendar/session/<int:session_id>/my-calendar.ics")
@login_required
def my_session_calendar_ics(session_id):
    """Serve personal 7pm-8am timed shifts export for authenticated user."""
    user = current_user()
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)

    ics_content = _generate_user_ics(session_id, user["id"])
    if not ics_content:
        abort(404)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in row["name"])
    filename = f"my_{safe_name}_shifts.ics"

    return Response(
        ics_content,
        mimetype="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
