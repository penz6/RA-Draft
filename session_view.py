from flask import abort, render_template, url_for
from core import app, can_manage, current_user, dates_for, db, login_required, next_picker, ordered_people, session_row

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
        "SELECT a.*,u.name FROM assignments a JOIN users u ON u.id=a.user_id WHERE a.session_id=? ORDER BY a.duty_date,u.name",
        (session_id,),
    ).fetchall()
    counts = {
        record["duty_date"]: record["n"]
        for record in db().execute(
            "SELECT duty_date,COUNT(*) n FROM assignments WHERE session_id=? GROUP BY duty_date",
            (session_id,),
        ).fetchall()
    }
    mine = next((item for item in picks if item["user_id"] == user["id"]), None)
    return render_template(
        "session_v2.html",
        draft=row,
        people=people,
        picks=picks,
        counts=counts,
        dates=dates_for(row),
        next=next_picker(session_id),
        can_manage=can_manage(user, row),
        calendar_ics_link=url_for("calendar_ics", assignment_id=mine["id"]) if mine else None,
    )
