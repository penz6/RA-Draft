"""Local Prostaff authentication and read-only campus duty dashboard."""

import secrets
from calendar import monthcalendar
from datetime import date
from datetime import datetime, timedelta, timezone

from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from core import app, audit, clean_single_line, current_user, db, require_csrf, roles
from staff_event_schedule import SCHOOL_TIMEZONE

_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def _valid_password(value):
    return isinstance(value, str) and 12 <= len(value) <= 128


def _duty_display_date(now=None):
    """Keep the prior night's duty roster visible through 7:59 a.m."""
    current = now or datetime.now(SCHOOL_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SCHOOL_TIMEZONE)
    local = current.astimezone(SCHOOL_TIMEZONE)
    return local.date() - timedelta(days=1) if local.hour < 8 else local.date()


@app.before_request
def isolate_prostaff_portal():
    """Keep local Prostaff accounts out of all RA/HRA scheduling routes."""
    user = current_user()
    if not user or not user["is_prostaff"]:
        return None
    allowed = {
        "prostaff_dashboard", "prostaff_schedule", "prostaff_one_on_ones", "prostaff_swaps",
        "prostaff_set_password", "schedule_one_on_one",
        "delete_one_on_one", "stop_impersonation", "logout", "static",
        "prostaff_staff_search",
    }
    is_impersonated = isinstance(session.get("impersonator_uid"), int)
    if request.endpoint not in allowed:
        if user["password_must_change"] and not is_impersonated:
            return redirect(url_for("prostaff_set_password"))
        return redirect(url_for("prostaff_dashboard"))
    if (user["password_must_change"] and not is_impersonated
            and request.endpoint not in {"prostaff_set_password", "logout", "static"}):
        return redirect(url_for("prostaff_set_password"))
    return None


@app.route("/prostaff/login", methods=["GET", "POST"])
def prostaff_login():
    """Authenticate a manually provisioned Prostaff account."""
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        require_csrf()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db().execute(
            "SELECT * FROM users WHERE email=? COLLATE NOCASE AND is_prostaff=1",
            (email,),
        ).fetchone()
        now = datetime.now(timezone.utc)
        locked = bool(
            user and user["prostaff_locked_until"]
            and datetime.fromisoformat(user["prostaff_locked_until"]) > now
        )
        candidate_hash = user["password_hash"] if user and user["password_hash"] else _DUMMY_PASSWORD_HASH
        password_ok = check_password_hash(candidate_hash, password)
        if not user or user["disabled"] or locked or not password_ok:
            if user and not user["disabled"] and not locked:
                failures = user["prostaff_failed_logins"] + 1
                locked_until = (
                    (now + timedelta(minutes=15)).isoformat() if failures >= 5 else None
                )
                db().execute(
                    "UPDATE users SET prostaff_failed_logins=?,prostaff_locked_until=? WHERE id=?",
                    (0 if locked_until else failures, locked_until, user["id"]),
                )
                audit("auth.prostaff.login_failed", "user", user["id"], actor_user_id=user["id"])
                db().commit()
            flash("Email or password was not recognized.", "error")
            return render_template("prostaff_login.html"), 401
        db().execute(
            "UPDATE users SET prostaff_failed_logins=0,prostaff_locked_until=NULL WHERE id=?",
            (user["id"],),
        )
        session.clear()
        session["uid"] = user["id"]
        session["csrf"] = secrets.token_hex(32)
        session.permanent = True
        audit("auth.prostaff.login", "user", user["id"], actor_user_id=user["id"])
        db().commit()
        return redirect(url_for("prostaff_set_password" if user["password_must_change"] else "prostaff_dashboard"))
    return render_template("prostaff_login.html")


@app.route("/prostaff/set-password", methods=["GET", "POST"])
def prostaff_set_password():
    user = current_user()
    if not user or not user["is_prostaff"]:
        abort(403)
    # Impersonation is for support and inspection only. An administrator must
    # never be able to choose or replace the Area Coordinator's password.
    if session.get("impersonator_uid"):
        abort(403)
    if request.method == "POST":
        require_csrf()
        password = request.form.get("password", "")
        if not _valid_password(password):
            flash("Password must be between 12 and 128 characters.", "error")
        elif password != request.form.get("password_confirm", ""):
            flash("Passwords do not match.", "error")
        else:
            db().execute(
                "UPDATE users SET password_hash=?,password_must_change=0 WHERE id=?",
                (generate_password_hash(password), user["id"]),
            )
            audit("auth.prostaff.password_set", "user", user["id"])
            db().commit()
            flash("Your password has been set.", "success")
            return redirect(url_for("prostaff_dashboard"))
    return render_template("prostaff_set_password.html")


@app.route("/prostaff")
def prostaff_dashboard():
    user = current_user()
    if not user or (not user["is_prostaff"] and user["role"] != "ADMIN"):
        abort(403)
    # Preserve bookmarked filtered/calendar URLs from the original combined page.
    if request.args.get("one_on_one_month"):
        return prostaff_one_on_ones()
    if request.args.get("q") or request.args.get("building") or request.args.get("month"):
        return prostaff_schedule()
    now = datetime.now(SCHOOL_TIMEZONE)
    local_today = now.date()
    duty_date = _duty_display_date(now)
    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()
    tonight = db().execute(
        "SELECT b.id building_id,b.name building_name,u.name,u.email,s.shift_start,s.shift_end "
        "FROM buildings b LEFT JOIN draft_sessions s ON s.building_id=b.id "
        "LEFT JOIN assignments a ON a.session_id=s.id AND a.duty_date=? "
        "LEFT JOIN users u ON u.id=a.user_id ORDER BY b.name,u.name",
        (duty_date.isoformat(),),
    ).fetchall()
    return render_template("prostaff_dashboard.html", buildings=buildings, tonight=tonight,
                           duty_date=duty_date.isoformat(),
                           showing_previous_night=duty_date < local_today,
                           prostaff_page="overview")


def _schedule_month(raw):
    try:
        return datetime.strptime(str(raw or ""), "%Y-%m").date().replace(day=1)
    except ValueError:
        return datetime.now(SCHOOL_TIMEZONE).date().replace(day=1)


def consolidate_duty_schedule(schedule_rows):
    """
    Consolidate monthly duty schedule assignment rows by day and building.

    For each date and building, multiple staff assignments are collapsed into a
    single entry with staff names joined by ' & ' (or a single name if only one).
    Shift times are omitted from the consolidated entries.
    """
    by_day_building = {}
    for row in schedule_rows:
        day = int(row["duty_date"][-2:])
        b_id = row["building_id"]
        key = (day, b_id)
        if key not in by_day_building:
            by_day_building[key] = {
                "building_id": b_id,
                "building_name": row["building_name"],
                "duty_date": row["duty_date"],
                "staff": [],
                "staff_names": set(),
            }
        staff_name = (row["name"] or "").strip()
        try:
            email = (row["email"] or "").strip()
        except (IndexError, KeyError):
            email = ""
        if staff_name and staff_name not in by_day_building[key]["staff_names"]:
            by_day_building[key]["staff"].append({
                "name": staff_name,
                "email": email,
            })
            by_day_building[key]["staff_names"].add(staff_name)

    by_day = {}
    for (day, _), item in by_day_building.items():
        names_list = [s["name"] for s in item["staff"]]
        joined_names = " & ".join(names_list) if names_list else "Unassigned"
        terms = []
        for s in item["staff"]:
            if s["name"]:
                terms.append(s["name"])
            if s["email"]:
                terms.append(s["email"])
        search_terms = " ".join(terms)
        entry = {
            "building_id": item["building_id"],
            "building_name": item["building_name"],
            "duty_date": item["duty_date"],
            "names": joined_names,
            "name": joined_names,
            "search_terms": search_terms,
            "staff_list": item["staff"],
        }
        by_day.setdefault(day, []).append(entry)

    return by_day


@app.route("/prostaff/schedule")
def prostaff_schedule():
    user = current_user()
    if not user or (not user["is_prostaff"] and user["role"] != "ADMIN"):
        abort(403)
    selected = _schedule_month(request.args.get("month"))
    month_end = date(selected.year + (selected.month == 12), 1 if selected.month == 12 else selected.month + 1, 1)
    building_raw = request.args.get("building", "").strip()
    search = request.args.get("q", "").strip()[:120]
    buildings = db().execute("SELECT * FROM buildings ORDER BY name").fetchall()
    selected_building = int(building_raw) if building_raw.isdigit() else None
    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    search_pattern = f"%{escaped}%"
    params = [
        selected.isoformat(), month_end.isoformat(),
        selected_building, selected_building,
        search, search_pattern, search_pattern,
    ]
    schedule = db().execute(
        "SELECT a.duty_date,u.name,u.email,b.id building_id,b.name building_name,s.shift_start,s.shift_end "
        "FROM assignments a JOIN users u ON u.id=a.user_id JOIN draft_sessions s ON s.id=a.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "WHERE u.role IN ('RA','HRA','ADMIN') AND u.is_prostaff=0 "
        "AND a.duty_date>=? AND a.duty_date<? "
        "AND (? IS NULL OR b.id=?) "
        "AND (?='' OR u.name LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\') "
        "ORDER BY a.duty_date,b.name,u.name LIMIT 1000",
        params,
    ).fetchall()
    by_day = consolidate_duty_schedule(schedule)
    return render_template("prostaff_schedule.html", buildings=buildings, schedule=schedule,
                           schedule_by_day=by_day, schedule_weeks=monthcalendar(selected.year, selected.month),
                           schedule_month=selected.strftime("%Y-%m"), schedule_month_label=selected.strftime("%B %Y"),
                           selected_building=selected_building, search=search,
                           prostaff_page="schedule")


def _prostaff_swap_batches(user):
    """Return duty-swap batches visible in the Prostaff portal.

    Area Coordinators are always restricted to their assigned building. Admin
    users may use the Prostaff view for support and can see all buildings.
    """
    if user["is_prostaff"]:
        if not user["building_id"]:
            return []
        scoped_building_id = user["building_id"]
    else:
        scoped_building_id = None

    rows = db().execute(
        "SELECT sr.*, s.name session_name, b.name building_name, "
        "u1.name requester_name, u2.name target_name, "
        "a1.duty_date requester_date, a2.duty_date target_date, "
        "ur.name reviewer_name, manual.target_id IS NOT NULL manager_manual "
        "FROM duty_swap_requests sr "
        "JOIN draft_sessions s ON s.id=sr.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "JOIN users u1 ON u1.id=sr.requester_user_id "
        "JOIN users u2 ON u2.id=sr.target_user_id "
        "JOIN assignments a1 ON a1.id=sr.requester_assignment_id "
        "JOIN assignments a2 ON a2.id=sr.target_assignment_id "
        "LEFT JOIN users ur ON ur.id=sr.reviewed_by "
        "LEFT JOIN (SELECT DISTINCT target_id FROM audit_log "
        "WHERE action='swap.manager_manual' AND target_type='swap_request') manual "
        "ON manual.target_id=sr.id "
        "WHERE (? IS NULL OR s.building_id=?) "
        "ORDER BY sr.created_at DESC, sr.id ASC",
        (scoped_building_id, scoped_building_id),
    ).fetchall()

    labels = {
        "PENDING": "Waiting for recipient",
        "TARGET_APPROVED": "Waiting for HRA",
        "APPROVED": "Approved",
        "REJECTED": "Rejected",
        "CANCELLED": "Cancelled",
    }
    batches = {}
    for row in rows:
        batch_key = row["batch_id"] or f"row:{row['id']}"
        if batch_key not in batches:
            batches[batch_key] = {
                "batch_id": batch_key,
                "status": row["status"],
                "status_label": labels.get(row["status"], row["status"].replace("_", " ").title()),
                "requester_name": row["requester_name"],
                "target_name": row["target_name"],
                "session_name": row["session_name"],
                "building_name": row["building_name"],
                "created_at": row["created_at"],
                "reviewer_name": row["reviewer_name"],
                "manager_manual": bool(row["manager_manual"]),
                "pairs": [],
            }
        batches[batch_key]["pairs"].append({
            "requester_date": row["requester_date"],
            "target_date": row["target_date"],
        })

    return list(batches.values())


@app.route("/prostaff/swaps")
def prostaff_swaps():
    user = current_user()
    if not user or (not user["is_prostaff"] and user["role"] != "ADMIN"):
        abort(403)

    building = None
    if user["is_prostaff"] and user["building_id"]:
        building = db().execute(
            "SELECT id,name FROM buildings WHERE id=?",
            (user["building_id"],),
        ).fetchone()

    return render_template(
        "prostaff_swaps.html",
        building=building,
        swap_batches=_prostaff_swap_batches(user),
        prostaff_page="swaps",
    )


@app.route("/prostaff/api/staff-search")
def prostaff_staff_search():
    user = current_user()
    if not user or (not user["is_prostaff"] and user["role"] != "ADMIN"):
        abort(403)
    q = request.args.get("q", "").strip()[:120]
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    search_pattern = f"%{escaped}%"
    rows = db().execute(
        "SELECT DISTINCT u.id, u.name, u.email FROM users u "
        "WHERE u.role IN ('RA','HRA','ADMIN') AND u.is_prostaff=0 AND u.disabled=0 "
        "AND (?='' OR u.name LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\') "
        "ORDER BY u.name LIMIT 25",
        (q, search_pattern, search_pattern),
    ).fetchall()
    return {"results": [{"id": r["id"], "name": r["name"], "email": r["email"]} for r in rows]}


@app.route("/prostaff/one-on-ones")
def prostaff_one_on_ones():
    user = current_user()
    if not user or (not user["is_prostaff"] and user["role"] != "ADMIN"):
        abort(403)
    return render_template("prostaff_one_on_ones.html", prostaff_page="one_on_ones")


@app.route("/admin/prostaff", methods=["POST"])
@roles("ADMIN")
def provision_prostaff():
    require_csrf()
    try:
        name = clean_single_line(request.form.get("name"), max_length=120)
    except ValueError:
        flash("Enter a valid name.", "error")
        return redirect(url_for("admin"))
    email = request.form.get("email", "").strip().lower()
    temporary_password = request.form.get("temporary_password", "")
    try:
        building_id = int(request.form.get("building_id", ""))
    except (TypeError, ValueError):
        flash("Choose the Area Coordinator's building.", "error")
        return redirect(url_for("admin"))
    if (len(email) > 254 or email.count("@") != 1 or not email.split("@", 1)[0]
            or not email.split("@", 1)[1] or not email.isascii()
            or any(ord(character) < 33 for character in email)):
        flash("Enter a valid email address.", "error")
        return redirect(url_for("admin"))
    if not _valid_password(temporary_password):
        flash("Temporary password must be between 12 and 128 characters.", "error")
        return redirect(url_for("admin"))
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    actor = conn.execute("SELECT role,disabled FROM users WHERE id=?", (current_user()["id"],)).fetchone()
    if not actor or actor["disabled"] or actor["role"] != "ADMIN":
        conn.rollback()
        abort(403)
    if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        conn.rollback()
        flash("Choose a valid building.", "error")
        return redirect(url_for("admin"))
    if conn.execute("SELECT 1 FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone():
        conn.rollback()
        flash("A user with that email already exists.", "error")
        return redirect(url_for("admin"))
    cur = conn.execute(
        "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_hash,password_must_change) "
        "VALUES(?,?,?,?,?,1,?,1)",
        (f"prostaff:{secrets.token_urlsafe(24)}", email, name, "RA", building_id,
         generate_password_hash(temporary_password)),
    )
    audit("admin.prostaff.create", "user", cur.lastrowid,
          {"email": email, "building_id": building_id})
    conn.commit()
    flash("Prostaff account created. Share the temporary password securely; it must be changed at first sign-in.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/prostaff/<int:user_id>/building", methods=["POST"])
@roles("ADMIN")
def assign_prostaff_building(user_id):
    """Assign an Area Coordinator to exactly one building."""
    require_csrf()
    try:
        building_id = int(request.form.get("building_id", ""))
    except (TypeError, ValueError):
        abort(400)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    actor = conn.execute("SELECT role,disabled FROM users WHERE id=?", (current_user()["id"],)).fetchone()
    target = conn.execute("SELECT id,is_prostaff,building_id FROM users WHERE id=?", (user_id,)).fetchone()
    building = conn.execute("SELECT id FROM buildings WHERE id=?", (building_id,)).fetchone()
    if not actor or actor["disabled"] or actor["role"] != "ADMIN":
        conn.rollback()
        abort(403)
    if not target or not target["is_prostaff"]:
        conn.rollback()
        abort(404)
    if not building:
        conn.rollback()
        abort(400)
    conn.execute("UPDATE users SET building_id=? WHERE id=?", (building_id, user_id))
    audit("admin.prostaff.building", "user", user_id,
          {"old_building_id": target["building_id"], "new_building_id": building_id})
    conn.commit()
    flash("Area Coordinator building updated.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/prostaff/<int:user_id>/reset-password", methods=["POST"])
@roles("ADMIN")
def reset_prostaff_password(user_id):
    """Issue a temporary password that must be replaced at next sign-in."""
    require_csrf()
    temporary_password = request.form.get("temporary_password", "")
    if not _valid_password(temporary_password):
        flash("Temporary password must be between 12 and 128 characters.", "error")
        return redirect(url_for("admin"))
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    actor = conn.execute(
        "SELECT role,disabled FROM users WHERE id=?", (current_user()["id"],)
    ).fetchone()
    target = conn.execute(
        "SELECT id,is_prostaff,disabled FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not actor or actor["disabled"] or actor["role"] != "ADMIN":
        conn.rollback()
        abort(403)
    if not target or not target["is_prostaff"]:
        conn.rollback()
        abort(404)
    conn.execute(
        "UPDATE users SET password_hash=?,password_must_change=1,"
        "prostaff_failed_logins=0,prostaff_locked_until=NULL WHERE id=?",
        (generate_password_hash(temporary_password), user_id),
    )
    audit("admin.prostaff.password_reset", "user", user_id)
    conn.commit()
    flash("Temporary Prostaff password saved. It must be changed at next sign-in.", "success")
    return redirect(url_for("admin"))
