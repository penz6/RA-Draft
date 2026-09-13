"""Building appearance and staff event settings."""

from __future__ import annotations

import re
from datetime import datetime
from xml.etree import ElementTree as ET

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


BUILDING_THEME_CHOICES = (
    ("rwu", "RWU Blue"),
    ("maple", "Maple — warm red & gold"),
    ("willow", "Willow — coastal teal"),
    ("cedar", "Cedar — evergreen"),
    ("stonewall", "Stonewall — slate blue"),
    ("bayside", "Bayside — bay blue"),
    ("north", "North Campus — cool navy"),
)
BUILDING_THEME_KEYS = {key for key, _label in BUILDING_THEME_CHOICES}
BUILDING_THEME_DEFAULT_ACCENTS = {
    "rwu": "#1f6f95",
    "maple": "#9d3f39",
    "willow": "#287c79",
    "cedar": "#3f6b52",
    "stonewall": "#536b86",
    "bayside": "#2f6f9b",
    "north": "#304f73",
}
MAX_BUILDING_ICON_BYTES = 128 * 1024
ACCENT_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_ALLOWED_SVG_TAGS = {
    "svg",
    "g",
    "path",
    "circle",
    "rect",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "defs",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "mask",
    "title",
    "desc",
}
_ALLOWED_SVG_ATTRS = {
    "id",
    "class",
    "viewBox",
    "width",
    "height",
    "x",
    "y",
    "x1",
    "x2",
    "y1",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "d",
    "points",
    "fill",
    "fill-rule",
    "fill-opacity",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-opacity",
    "opacity",
    "transform",
    "preserveAspectRatio",
    "gradientUnits",
    "gradientTransform",
    "offset",
    "stop-color",
    "stop-opacity",
    "clip-path",
    "mask",
    "vector-effect",
    "shape-rendering",
    "role",
    "aria-label",
}
_SAFE_LOCAL_URL = re.compile(r"^url\(#[A-Za-z0-9_.:-]+\)$")


def infer_building_theme(name: str) -> str:
    lowered = str(name or "").strip().lower()
    for needle, theme in (
        ("maple", "maple"),
        ("willow", "willow"),
        ("cedar", "cedar"),
        ("stonewall", "stonewall"),
        ("bayside", "bayside"),
        ("north", "north"),
    ):
        if needle in lowered:
            return theme
    return "rwu"


def normalize_building_theme(value, *, building_name="") -> str:
    key = str(value or "").strip().lower()
    if not key:
        return infer_building_theme(building_name)
    if key not in BUILDING_THEME_KEYS:
        raise ValueError("Choose a valid building theme.")
    return key


def normalize_accent_color(value, *, theme_key="rwu") -> str:
    color = str(value or "").strip()
    if not color:
        return BUILDING_THEME_DEFAULT_ACCENTS.get(theme_key, BUILDING_THEME_DEFAULT_ACCENTS["rwu"])
    if not ACCENT_COLOR_RE.fullmatch(color):
        raise ValueError("Choose a valid accent color.")
    return color.lower()


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def sanitize_building_svg(raw: bytes) -> str:
    if not raw:
        raise ValueError("Choose an SVG file to upload.")
    if len(raw) > MAX_BUILDING_ICON_BYTES:
        raise ValueError("Building icons must be 128 KB or smaller.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The building icon must be a UTF-8 SVG file.") from exc

    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("SVG document types and entities are not allowed.")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("The uploaded file is not valid SVG.") from exc
    if _local_name(root.tag) != "svg":
        raise ValueError("The uploaded file must have an SVG root element.")

    for element in root.iter():
        tag = _local_name(element.tag)
        if tag not in _ALLOWED_SVG_TAGS:
            raise ValueError(f"SVG element '{tag}' is not allowed in building icons.")
        for raw_name, raw_value in element.attrib.items():
            name = _local_name(raw_name)
            value = str(raw_value or "").strip()
            if name.lower().startswith("on") or name.lower() in {"href", "style"}:
                raise ValueError("SVG scripts, links, and inline styles are not allowed.")
            if name not in _ALLOWED_SVG_ATTRS:
                raise ValueError(f"SVG attribute '{name}' is not allowed in building icons.")
            lowered_value = value.lower()
            if any(token in lowered_value for token in ("javascript:", "data:", "http://", "https://")):
                raise ValueError("SVG icons cannot load external content.")
            if "url(" in lowered_value and not _SAFE_LOCAL_URL.fullmatch(value):
                raise ValueError("Only local SVG paint references are allowed.")

    normalized = ET.tostring(root, encoding="unicode", method="xml")
    if len(normalized.encode("utf-8")) > MAX_BUILDING_ICON_BYTES:
        raise ValueError("Building icons must be 128 KB or smaller.")
    return normalized


def _ensure_building_profile_schema() -> None:
    with app.app_context():
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(buildings)")}
        added_theme = "theme_key" not in columns
        if added_theme:
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

        if added_theme:
            for row in conn.execute("SELECT id,name FROM buildings").fetchall():
                conn.execute(
                    "UPDATE buildings SET theme_key=? WHERE id=?",
                    (infer_building_theme(row["name"]), row["id"]),
                )

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
    return db().execute(
        "SELECT b.*, CASE WHEN b.icon_svg IS NOT NULL AND trim(b.icon_svg)<>'' "
        "THEN 1 ELSE 0 END AS has_icon FROM buildings b WHERE b.id=?",
        (building_id,),
    ).fetchone()


@app.context_processor
def inject_building_personality():
    user = current_user()
    profile = _building_row(user["building_id"]) if user and user["building_id"] else None
    return {
        "building_profile": profile,
        "building_theme_choices": BUILDING_THEME_CHOICES,
        "building_theme_default_accents": BUILDING_THEME_DEFAULT_ACCENTS,
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


@app.route("/buildings/<int:building_id>/icon.svg")
@login_required
def building_icon(building_id):
    row = _building_row(building_id)
    if not row or not row["icon_svg"]:
        abort(404)
    response = Response(row["icon_svg"], mimetype="image/svg+xml")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = "inline; filename=building-icon.svg"
    return response


@app.route("/buildings/<int:building_id>/theme.css")
@login_required
def building_theme_css(building_id):
    row = _building_row(building_id)
    if not row:
        abort(404)
    try:
        accent = normalize_accent_color(row["accent_color"], theme_key=row["theme_key"] or "rwu")
    except ValueError:
        accent = BUILDING_THEME_DEFAULT_ACCENTS["rwu"]
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
        theme_key = normalize_building_theme(request.form.get("theme_key"), building_name=name)
        accent_color = normalize_accent_color(request.form.get("accent_color"), theme_key=theme_key)
        upload = request.files.get("icon_svg")
        icon_svg = None
        if upload and upload.filename:
            if not upload.filename.lower().endswith(".svg"):
                raise ValueError("Building marks must be SVG files.")
            icon_svg = sanitize_building_svg(upload.read(MAX_BUILDING_ICON_BYTES + 1))
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
        "INSERT INTO buildings(name,theme_key,accent_color,icon_svg) VALUES(?,?,?,?)",
        (name, theme_key, accent_color, icon_svg),
    )
    audit(
        "admin.building.create",
        "building",
        cur.lastrowid,
        {
            "name": name,
            "theme_key": theme_key,
            "accent_color": accent_color,
            "has_icon": bool(icon_svg),
        },
    )
    conn.commit()
    flash("Building added.", "success")
    return redirect(url_for("admin", _anchor=f"building-{cur.lastrowid}"))


@app.route("/admin/buildings/<int:building_id>/appearance", methods=["POST"])
@roles("ADMIN")
def update_building_appearance(building_id):
    require_csrf()
    existing = _building_row(building_id)
    if not existing:
        abort(404)
    try:
        theme_key = normalize_building_theme(
            request.form.get("theme_key"), building_name=existing["name"]
        )
        accent_color = normalize_accent_color(request.form.get("accent_color"), theme_key=theme_key)
        remove_icon = request.form.get("remove_icon") == "1"
        upload = request.files.get("icon_svg")
        replacement = None
        has_replacement = bool(upload and upload.filename)
        if has_replacement:
            if not upload.filename.lower().endswith(".svg"):
                raise ValueError("Building marks must be SVG files.")
            replacement = sanitize_building_svg(upload.read(MAX_BUILDING_ICON_BYTES + 1))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin", _anchor=f"building-{building_id}"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    locked = conn.execute("SELECT * FROM buildings WHERE id=?", (building_id,)).fetchone()
    if not locked:
        conn.rollback()
        abort(404)
    if has_replacement:
        icon_svg = replacement
    elif remove_icon:
        icon_svg = None
    else:
        icon_svg = locked["icon_svg"]
    conn.execute(
        "UPDATE buildings SET theme_key=?,accent_color=?,icon_svg=? WHERE id=?",
        (theme_key, accent_color, icon_svg, building_id),
    )
    audit(
        "admin.building.appearance",
        "building",
        building_id,
        {
            "theme_key": theme_key,
            "accent_color": accent_color,
            "icon_changed": bool(has_replacement or remove_icon),
            "has_icon": bool(icon_svg),
        },
    )
    conn.commit()
    flash("Building appearance updated.", "success")
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


@app.route("/buildings/<int:building_id>/staff-meeting", methods=["POST"])
@roles("HRA", "ADMIN")
def update_staff_meeting(building_id):
    require_csrf()
    actor = _check_staff_event_access(building_id)
    try:
        event_at, event_location = _parse_staff_event_form()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        conn.rollback()
        abort(404)
    if actor["role"] == "HRA":
        current = conn.execute("SELECT building_id,role FROM users WHERE id=?", (actor["id"],)).fetchone()
        if not current or current["role"] != "HRA" or current["building_id"] != building_id:
            conn.rollback()
            abort(403)
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
    return redirect(url_for("dashboard"))


@app.route("/buildings/<int:building_id>/staff-dinner", methods=["POST"])
@roles("HRA", "ADMIN")
def update_staff_dinner(building_id):
    require_csrf()
    actor = _check_staff_event_access(building_id)
    try:
        event_at, event_location = _parse_staff_event_form()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        conn.rollback()
        abort(404)
    if actor["role"] == "HRA":
        current = conn.execute("SELECT building_id,role FROM users WHERE id=?", (actor["id"],)).fetchone()
        if not current or current["role"] != "HRA" or current["building_id"] != building_id:
            conn.rollback()
            abort(403)
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
    return redirect(url_for("dashboard"))
