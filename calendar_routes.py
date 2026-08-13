from datetime import datetime, timedelta
from urllib.parse import urlencode

from flask import Response, abort, redirect
from core import app, current_user, db, login_required


def ics_escape(text):
    return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def assignment_row(assignment_id):
    return db().execute(
        "SELECT a.*,s.name session_name,s.shift_start,s.shift_end,b.name building_name "
        "FROM assignments a JOIN draft_sessions s ON s.id=a.session_id "
        "JOIN buildings b ON b.id=s.building_id WHERE a.id=?",
        (assignment_id,),
    ).fetchone()


def assignment_times(row):
    start = datetime.fromisoformat(f"{row['duty_date']}T{row['shift_start']}")
    end = datetime.fromisoformat(f"{row['duty_date']}T{row['shift_end']}")
    if end <= start:
        end += timedelta(days=1)
    return start, end


@app.route("/calendar/<int:assignment_id>.ics")
@login_required
def calendar_ics(assignment_id):
    row = assignment_row(assignment_id)
    user = current_user()
    if not row or (user["role"] != "ADMIN" and row["user_id"] != user["id"]):
        abort(403)
    start, end = assignment_times(row)
    body = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RA Draft//Duty Scheduler//EN",
        "BEGIN:VEVENT",
        f"UID:ra-draft-{assignment_id}@local",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{ics_escape(row['session_name'])} Duty",
        f"LOCATION:{ics_escape(row['building_name'])}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    return Response(
        body,
        mimetype="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=duty-{row['duty_date']}.ics"},
    )


@app.route("/calendar/<int:assignment_id>/google")
@login_required
def calendar_google(assignment_id):
    row = assignment_row(assignment_id)
    user = current_user()
    if not row or (user["role"] != "ADMIN" and row["user_id"] != user["id"]):
        abort(403)
    start, end = assignment_times(row)
    params = {
        "action": "TEMPLATE",
        "text": f"{row['session_name']} Duty",
        "dates": f"{start.strftime('%Y%m%dT%H%M%S')}/{end.strftime('%Y%m%dT%H%M%S')}",
        "location": row["building_name"],
        "details": "RA duty shift",
    }
    return redirect("https://calendar.google.com/calendar/render?" + urlencode(params))
