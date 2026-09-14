"""Building setup and one-time or repeating staff events."""

from __future__ import annotations

from datetime import datetime
from flask import Response, abort, flash, redirect, request, url_for

from core import (
    app, audit, clean_single_line, current_user, db, login_required,
    require_csrf, roles,
)
from staff_event_schedule import next_occurrence, parse_event_start, parse_repeat_weeks, repeat_label

PORTAL_NAVY = "#01295f"


def _ensure_building_profile_schema() -> None:
    with app.app_context():
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(buildings)")}
        # Retain retired columns for existing databases/rollback compatibility.
        # No active theme rendering reads accent_color or theme_key anymore.
        if "theme_key" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN theme_key TEXT NOT NULL DEFAULT 'rwu'")
        if "icon_svg" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN icon_svg TEXT")
        if "accent_color" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN accent_color TEXT")
        if "staff_meeting_at" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN staff_meeting_at TEXT")
        if "staff_meeting_location" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN staff_meeting_location TEXT")
        if "staff_dinner_at" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN staff_dinner_at TEXT")
        if "staff_dinner_location" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN staff_dinner_location TEXT")
        if "staff_meeting_repeat_weeks" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN staff_meeting_repeat_weeks INTEGER NOT NULL DEFAULT 0 CHECK(staff_meeting_repeat_weeks BETWEEN 0 AND 52)")
        if "staff_dinner_repeat_weeks" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN staff_dinner_repeat_weeks INTEGER NOT NULL DEFAULT 0 CHECK(staff_dinner_repeat_weeks BETWEEN 0 AND 52)")
        conn.execute("UPDATE buildings SET icon_svg=NULL WHERE icon_svg IS NOT NULL")
        conn.execute("UPDATE buildings SET theme_key='rwu' WHERE theme_key IS NULL OR theme_key<>'rwu'")
        # Preserve staff events created by the original theme branch.
        if "community_meeting_at" in columns and "community_meeting_location" in columns:
            conn.execute(
                "UPDATE buildings SET "
                "staff_meeting_at=COALESCE(staff_meeting_at,community_meeting_at),"
                "staff_meeting_location=COALESCE(staff_meeting_location,community_meeting_location) "
                "WHERE community_meeting_at IS NOT NULL OR community_meeting_location IS NOT NULL"
            )
        conn.commit()


_ensure_building_profile_schema()


def _building_row(building_id):
    return db().execute("SELECT b.* FROM buildings b WHERE b.id=?", (building_id,)).fetchone()


@app.context_processor
def inject_building_profile():
    user = current_user()
    profile = _building_row(user["building_id"]) if user and user["building_id"] else None
    return {"building_profile": profile}


@app.template_filter("event_label")
def event_label(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    clock = parsed.strftime("%I:%M %p").lstrip("0")
    return f"{parsed.strftime('%a, %b')} {parsed.day} \u00b7 {clock}"


app.add_template_filter(next_occurrence, "next_event")
app.add_template_filter(repeat_label, "repeat_label")


@app.route("/buildings/<int:building_id>/theme.css")
@login_required
def building_theme_css(building_id):
    """Serve fixed navy to older cached pages; new pages use shared static CSS."""
    if not _building_row(building_id):
        abort(404)
    response = Response(
        f":root{{--hall-accent:{PORTAL_NAVY};--building-accent:{PORTAL_NAVY};}}",
        mimetype="text/css",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/admin/buildings/profiled", methods=["POST"])
@roles("ADMIN")
def create_profiled_building():
    require_csrf()
    actor_id = current_user()["id"]
    try:
        name = clean_single_line(request.form.get("name"), max_length=80)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin"))
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    actor = conn.execute("SELECT role,disabled FROM users WHERE id=?", (actor_id,)).fetchone()
    if not actor or actor["disabled"] or actor["role"] != "ADMIN":
        conn.rollback()
        abort(403)
    if conn.execute("SELECT id FROM buildings WHERE name=? COLLATE NOCASE", (name,)).fetchone():
        conn.rollback()
        flash("That building already exists.", "error")
        return redirect(url_for("admin"))
    # Ignore retired appearance inputs, including forged color or SVG fields.
    cur = conn.execute(
        "INSERT INTO buildings(name,theme_key,accent_color,icon_svg) VALUES(?,'rwu',?,NULL)",
        (name, PORTAL_NAVY),
    )
    audit("admin.building.create", "building", cur.lastrowid, {"name": name})
    conn.commit()
    flash("Building added.", "success")
    return redirect(url_for("admin", _anchor=f"building-{cur.lastrowid}"))


def _parse_staff_event_form():
    raw_when = str(request.form.get("event_at") or "").strip()
    raw_location = str(request.form.get("event_location") or "").strip()
    if not raw_when and not raw_location:
        return None, None, 0
    if not raw_when or not raw_location:
        raise ValueError("Set both a date/time and location, or clear both fields.")
    event_at = parse_event_start(raw_when)
    repeat_weeks = parse_repeat_weeks(request.form.get("repeat_weeks"))
    try:
        location = clean_single_line(raw_location, max_length=120)
    except ValueError as exc:
        raise ValueError("Location must be 1 to 120 characters.") from exc
    return event_at, location, repeat_weeks


def _check_staff_event_access(building_id):
    actor = current_user()
    if not actor or actor["role"] not in ("HRA", "ADMIN"):
        abort(403)
    if actor["role"] == "HRA" and actor["building_id"] != building_id:
        abort(403)
    return actor


def _check_locked_staff_event_access(conn, actor_id, building_id):
    """Revalidate event permissions after acquiring the database write lock."""
    actor = conn.execute(
        "SELECT id,building_id,role,disabled FROM users WHERE id=?", (actor_id,),
    ).fetchone()
    if not actor or actor["disabled"] or actor["role"] not in ("HRA", "ADMIN"):
        conn.rollback()
        abort(403)
    if actor["role"] == "HRA" and actor["building_id"] != building_id:
        conn.rollback()
        abort(403)
    return actor


def _staff_event_redirect(building_id):
    if request.form.get("return_to") == "admin":
        actor = current_user()
        if actor and actor["role"] == "ADMIN":
            return redirect(url_for("admin", _anchor=f"building-{building_id}"))
    return redirect(url_for("dashboard"))


@app.route("/buildings/<int:building_id>/staff-meeting", methods=["POST"])
@roles("HRA", "ADMIN")
def update_staff_meeting(building_id):
    require_csrf()
    actor = _check_staff_event_access(building_id)
    try:
        event_at, event_location, repeat_weeks = _parse_staff_event_form()
    except ValueError as exc:
        flash(str(exc), "error")
        return _staff_event_redirect(building_id)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        conn.rollback()
        abort(404)
    _check_locked_staff_event_access(conn, actor["id"], building_id)
    conn.execute(
        "UPDATE buildings SET staff_meeting_at=?,staff_meeting_location=?,staff_meeting_repeat_weeks=? WHERE id=?",
        (event_at, event_location, repeat_weeks, building_id),
    )
    audit("building.staff_meeting.update", "building", building_id,
          {"event_at": event_at, "event_location": event_location, "repeat_weeks": repeat_weeks})
    conn.commit()
    flash("Staff meeting cleared." if event_at is None else "Staff meeting updated.", "success")
    return _staff_event_redirect(building_id)


@app.route("/buildings/<int:building_id>/staff-dinner", methods=["POST"])
@roles("HRA", "ADMIN")
def update_staff_dinner(building_id):
    require_csrf()
    actor = _check_staff_event_access(building_id)
    try:
        event_at, event_location, repeat_weeks = _parse_staff_event_form()
    except ValueError as exc:
        flash(str(exc), "error")
        return _staff_event_redirect(building_id)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        conn.rollback()
        abort(404)
    _check_locked_staff_event_access(conn, actor["id"], building_id)
    conn.execute(
        "UPDATE buildings SET staff_dinner_at=?,staff_dinner_location=?,staff_dinner_repeat_weeks=? WHERE id=?",
        (event_at, event_location, repeat_weeks, building_id),
    )
    audit("building.staff_dinner.update", "building", building_id,
          {"event_at": event_at, "event_location": event_location, "repeat_weeks": repeat_weeks})
    conn.commit()
    flash("Staff dinner cleared." if event_at is None else "Staff dinner updated.", "success")
    return _staff_event_redirect(building_id)
