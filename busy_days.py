"""Private date preferences for duty picking and swaps."""

from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for

from core import app, current_user, db, login_required, require_csrf


def busy_dates_for(user_id):
    return {row[0] for row in db().execute(
        "SELECT duty_date FROM busy_dates WHERE user_id=?", (user_id,)
    )}


@app.route("/settings/busy-days", methods=["GET", "POST"])
@login_required
def busy_days():
    user = current_user()
    if request.method == "GET":
        return render_template("busy_days.html", me=user, dates=sorted(busy_dates_for(user["id"])))

    require_csrf()
    action = request.form.get("action")
    conn = db()
    if action == "dismiss":
        conn.execute("UPDATE users SET busy_banner_dismissed=1 WHERE id=?", (user["id"],))
        conn.commit()
        return redirect(url_for("dashboard"))
    if action not in {"add", "remove"}:
        abort(400)
    try:
        day = date.fromisoformat(request.form.get("day", "")).isoformat()
    except ValueError:
        abort(400)
    if action == "add":
        conn.execute("INSERT OR IGNORE INTO busy_dates(user_id,duty_date) VALUES(?,?)", (user["id"], day))
        flash("Busy day saved.", "success")
    else:
        conn.execute("DELETE FROM busy_dates WHERE user_id=? AND duty_date=?", (user["id"], day))
        flash("Busy day removed.", "success")
    conn.commit()
    return redirect(url_for("busy_days"))
