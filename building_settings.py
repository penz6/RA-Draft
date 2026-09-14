"""Building accent color and staff event settings."""

from __future__ import annotations

import re
from datetime import datetime

from flask import Response, abort, flash, redirect, request, url_for

from core import (
    app,
    audit,
    clean_single_line,
    current_user,
    db,
    login_required,
    require_csrf,
    roles,
)


DEFAULT_BUILDING_ACCENT = "#1f6f95"
ACCENT_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_accent_color(value) -> str:
    color = str(value or "").strip()
    if not color:
        return DEFAULT_BUILDING_ACCENT
    if not ACCENT_COLOR_RE.fullmatch(color):
        raise ValueError("Choose a valid accent color.")
    return color.lower()


def _ensure_building_profile_schema() -> None:
    with app.app_context():
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(buildings)")}

        # Keep the legacy columns for compatibility with existing databases and
        # older live-state code, but appearance is now driven only by accent_color.
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

        # Stop rendering any marks that were uploaded before this option was removed.
        conn.execute("UPDATE buildings SET icon_svg=NULL WHERE icon_svg IS NOT NULL")
        conn.execute("UPDATE buildings SET theme_key='rwu' WHERE theme_key IS NULL OR theme_key<>'rwu'")

        # Preserve meetings created by the first version of the theme branch.
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
def inject_building_personality():
    user = current_user()
    profile = _building_row(user["building_id"]) if user and user["building_id"] else None
    return {
        "building_profile": profile,
        "building_theme_choices": (("rwu", "RWU"),),
        "building_theme_default_accents": {"rwu": DEFAULT_BUILDING_ACCENT},
    }


@app.template_filter("event_label")
def event_label(value):
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    clock = parsed.strftime("%I:%M %p").lstrip("0")
    return f"{parsed.strftime('%a, %b')} {parsed.day} · {clock}"


@app.route("/buildings/<int:building_id>/theme.css")
@login_required
def building_theme_css(building_id):
    row = _building_row(building_id)
    if not row:
        abort(404)
    try:
        accent = normalize_accent_color(row["accent_color"])
    except ValueError:
        accent = DEFAULT_BUILDING_ACCENT
    response = Response(
        f":root{{--hall-accent:{accent};--building-accent:{accent};}}",
        mimetype="text/css",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/admin/buildings/profiled", methods=["POST"])
@roles("ADMIN")
def create_profiled_building():
    require_csrf()
    try:
        name = clean_single_line(request.form.get("name"), max_length=80)
        accent_color = normalize_accent_color(request.form.get("accent_color"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    actor = current_user()
    if not actor or actor["role"] != "ADMIN":
        conn.rollback()
        abort(403)
    if conn.execute(
        "SELECT id FROM buildings WHERE name=? COLLATE NOCASE",
        (name,),
    ).fetchone():
        conn.rollback()
        flash("That building already exists.", "error")
        return redirect(url_for("admin"))

    cur = conn.execute(
        "INSERT INTO buildings(name,theme_key,accent_color,icon_svg) VALUES(?,'rwu',?,NULL)",
        (name, accent_color),
    )
    audit(
        "admin.building.create",
        "building",
        cur.lastrowid,
        {"name": name, "accent_color": accent_color},
    )
    conn.commit()
    flash("Building added.", "success")
    return redirect(url_for("admin", _anchor=f"building-{cur.lastrowid}"))


@app.route("/admin/buildings/<int:building_id>/appearance", methods=["POST"])
@roles("ADMIN")
def update_building_appearance(building_id):
    require_csrf()
    if not _building_row(building_id):
        abort(404)
    try:
        accent_color = normalize_accent_color(request.form.get("accent_color"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin", _anchor=f"building-{building_id}"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    actor = current_user()
    if not actor or actor["role"] != "ADMIN":
        conn.rollback()
        abort(403)
    if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        conn.rollback()
        abort(404)
    conn.execute(
        "UPDATE buildings SET theme_key='rwu',accent_color=?,icon_svg=NULL WHERE id=?",
        (accent_color, building_id),
    )
    audit(
        "admin.building.appearance",
        "building",
        building_id,
        {"accent_color": accent_color},
    )
    conn.commit()
    flash("Building color updated.", "success")
    return redirect(url_for("admin", _anchor=f"building-{building_id}"))


def _parse_staff_event_form():
    raw_when = str(request.form.get("event_at") or "").strip()
    raw_location = str(request.form.get("event_location") or "").strip()
    clearing = not raw_when and not raw_location
    if not clearing and (not raw_when or not raw_location):
        raise ValueError("Set both a date/time and location, or clear both fields.")
    if clearing:
        return None, None
    try:
        parsed = datetime.fromisoformat(raw_when)
    except ValueError as exc:
        raise ValueError("Choose a valid date and time.") from exc
    parsed = parsed.replace(second=0, microsecond=0)
    try:
        location = clean_single_line(raw_location, max_length=120)
    except ValueError as exc:
        raise ValueError("Location must be 1 to 120 characters.") from exc
    return parsed.isoformat(timespec="minutes"), location


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
        "SELECT id,building_id,role,disabled FROM users WHERE id=?",
        (actor_id,),
    ).fetchone()
    if not actor or actor["disabled"] or actor["role"] not in ("HRA", "ADMIN"):
        conn.rollback()
        abort(403)
    if actor["role"] == "HRA" and actor["building_id"] != building_id:
        conn.rollback()
        abort(403)
    return actor


def _staff_event_redirect(building_id):
    """Return to the admin building card only for authenticated admin forms."""
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
        event_at, event_location = _parse_staff_event_form()
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
        "UPDATE buildings SET staff_meeting_at=?,staff_meeting_location=? WHERE id=?",
        (event_at, event_location, building_id),
    )
    audit(
        "building.staff_meeting.update",
        "building",
        building_id,
        {"event_at": event_at, "event_location": event_location},
    )
    conn.commit()
    flash("Staff meeting cleared." if event_at is None else "Staff meeting updated.", "success")
    return _staff_event_redirect(building_id)


@app.route("/buildings/<int:building_id>/staff-dinner", methods=["POST"])
@roles("HRA", "ADMIN")
def update_staff_dinner(building_id):
    require_csrf()
    actor = _check_staff_event_access(building_id)
    try:
        event_at, event_location = _parse_staff_event_form()
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
        "UPDATE buildings SET staff_dinner_at=?,staff_dinner_location=? WHERE id=?",
        (event_at, event_location, building_id),
    )
    audit(
        "building.staff_dinner.update",
        "building",
        building_id,
        {"event_at": event_at, "event_location": event_location},
    )
    conn.commit()
    flash("Staff dinner cleared." if event_at is None else "Staff dinner updated.", "success")
    return _staff_event_redirect(building_id)
