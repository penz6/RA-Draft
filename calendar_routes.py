from datetime import date, timedelta

from flask import Response, abort
from core import app, current_user, db, login_required


def ics_escape(text):
    return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def assignment_row(assignment_id):
    return db().execute(
        "SELECT a.*,s.name session_name,b.name building_name "
        "FROM assignments a JOIN draft_sessions s ON s.id=a.session_id "
        "JOIN buildings b ON b.id=s.building_id WHERE a.id=?",
        (assignment_id,),
    ).fetchone()


@app.route("/calendar/<int:assignment_id>.ics")
@login_required
def calendar_ics(assignment_id):
    row = assignment_row(assignment_id)
    user = current_user()
    if not row or (user["role"] != "ADMIN" and row["user_id"] != user["id"]):
        abort(403)

    duty_date = date.fromisoformat(row["duty_date"])
    end_date = duty_date + timedelta(days=1)
    body = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "PRODID:-//RA Draft//Duty Scheduler//EN",
        "BEGIN:VEVENT",
        f"UID:ra-draft-{assignment_id}@local",
        f"DTSTART;VALUE=DATE:{duty_date.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}",
        f"SUMMARY:{ics_escape(row['session_name'])} Duty",
        f"LOCATION:{ics_escape(row['building_name'])}",
        "DESCRIPTION:RA duty shift",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    return Response(
        body,
        mimetype="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=duty-{row['duty_date']}.ics"},
    )
