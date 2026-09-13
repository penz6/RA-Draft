"""Building personality, icon, and community-meeting settings.

This module keeps the optional residence-hall presentation data separate from the
core scheduling schema.  It installs an additive SQLite migration on import,
exposes the current building to templates, and owns the small management routes
used by admins and HRAs.
"""

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
MAX_BUILDING_ICON_BYTES = 128 * 1024

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
    """Choose a sensible residence-hall palette from a building name."""
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
    """Return a validated theme key, inferring one when the form leaves it blank."""
    key = str(value or "").strip().lower()
    if not key:
        return infer_building_theme(building_name)
    if key not in BUILDING_THEME_KEYS:
        raise ValueError("Choose a valid building theme.")
    return key


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def sanitize_building_svg(raw: bytes) -> str:
    """Validate a small, passive SVG icon and return normalized XML.

    Building marks are rendered through an ``img`` element rather than injected
    into page HTML.  The allow-list below additionally rejects scripting,
    embedded HTML, remote references, and inline CSS.
    """
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
    """Install additive building-personality columns for existing deployments."""
    with app.app_context():
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(buildings)")}
        added_theme = "theme_key" not in columns
        if added_theme:
            conn.execute("ALTER TABLE buildings ADD COLUMN theme_key TEXT NOT NULL DEFAULT 'rwu'")
        if "icon_svg" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN icon_svg TEXT")
        if "community_meeting_at" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN community_meeting_at TEXT")
        if "community_meeting_location" not in columns:
            conn.execute("ALTER TABLE buildings ADD COLUMN community_meeting_location TEXT")
        if added_theme:
            for row in conn.execute("SELECT id,name FROM buildings").fetchall():
                conn.execute(
                    "UPDATE buildings SET theme_key=? WHERE id=?",
                    (infer_building_theme(row["name"]), row["id"]),
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
    """Expose the signed-in user's building and theme controls to templates."""
    user = current_user()
    profile = _building_row(user["building_id"]) if user and user["building_id"] else None
    return {
        "building_profile": profile,
        "building_theme_choices": BUILDING_THEME_CHOICES,
    }


@app.template_filter("meeting_label")
def meeting_label(value):
    """Format the stored local meeting date/time for compact dashboard display."""
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
    """Serve a validated building mark as an image resource."""
    row = _building_row(building_id)
    if not row or not row["icon_svg"]:
        abort(404)
    response = Response(row["icon_svg"], mimetype="image/svg+xml")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = "inline; filename=building-icon.svg"
    return response


@app.route("/admin/buildings/profiled", methods=["POST"])
@roles("ADMIN")
def create_profiled_building():
    """Create a building with its visual theme and optional SVG mark in one step."""
    require_csrf()
    try:
        name = clean_single_line(request.form.get("name"), max_length=80)
        theme_key = normalize_building_theme(request.form.get("theme_key"), building_name=name)
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
        "INSERT INTO buildings(name,theme_key,icon_svg) VALUES(?,?,?)",
        (name, theme_key, icon_svg),
    )
    audit(
        "admin.building.create",
        "building",
        cur.lastrowid,
        {"name": name, "theme_key": theme_key, "has_icon": bool(icon_svg)},
    )
    conn.commit()
    flash("Building added.", "success")
    return redirect(url_for("admin", _anchor=f"building-{cur.lastrowid}"))


@app.route("/admin/buildings/<int:building_id>/appearance", methods=["POST"])
@roles("ADMIN")
def update_building_appearance(building_id):
    """Update a building palette and optional SVG mark from the admin console."""
    require_csrf()
    existing = _building_row(building_id)
    if not existing:
        abort(404)
    try:
        theme_key = normalize_building_theme(
            request.form.get("theme_key"), building_name=existing["name"]
        )
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
        "UPDATE buildings SET theme_key=?,icon_svg=? WHERE id=?",
        (theme_key, icon_svg, building_id),
    )
    audit(
        "admin.building.appearance",
        "building",
        building_id,
        {
            "theme_key": theme_key,
            "icon_changed": bool(has_replacement or remove_icon),
            "has_icon": bool(icon_svg),
        },
    )
    conn.commit()
    flash("Building appearance updated.", "success")
    return redirect(url_for("admin", _anchor=f"building-{building_id}"))


@app.route("/buildings/<int:building_id>/community-meeting", methods=["POST"])
@roles("HRA", "ADMIN")
def update_building_meeting(building_id):
    """Let an HRA publish or clear the next meeting for their own building."""
    require_csrf()
    actor = current_user()
    if actor["role"] == "HRA" and actor["building_id"] != building_id:
        abort(403)

    raw_when = str(request.form.get("meeting_at") or "").strip()
    raw_location = str(request.form.get("meeting_location") or "").strip()
    clearing = not raw_when and not raw_location
    if not clearing and (not raw_when or not raw_location):
        flash("Set both a meeting time and location, or clear both fields.", "error")
        return redirect(url_for("dashboard"))

    meeting_at = None
    meeting_location = None
    if not clearing:
        try:
            parsed = datetime.fromisoformat(raw_when)
        except ValueError:
            flash("Choose a valid meeting date and time.", "error")
            return redirect(url_for("dashboard"))
        if parsed.second or parsed.microsecond:
            parsed = parsed.replace(second=0, microsecond=0)
        meeting_at = parsed.isoformat(timespec="minutes")
        try:
            meeting_location = clean_single_line(raw_location, max_length=120)
        except ValueError:
            flash("Meeting location must be 1 to 120 characters.", "error")
            return redirect(url_for("dashboard"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    building = conn.execute("SELECT id,name FROM buildings WHERE id=?", (building_id,)).fetchone()
    if not building:
        conn.rollback()
        abort(404)
    if actor["role"] == "HRA":
        current = conn.execute("SELECT building_id,role FROM users WHERE id=?", (actor["id"],)).fetchone()
        if not current or current["role"] != "HRA" or current["building_id"] != building_id:
            conn.rollback()
            abort(403)
    conn.execute(
        "UPDATE buildings SET community_meeting_at=?,community_meeting_location=? WHERE id=?",
        (meeting_at, meeting_location, building_id),
    )
    audit(
        "building.community_meeting.update",
        "building",
        building_id,
        {"meeting_at": meeting_at, "meeting_location": meeting_location},
    )
    conn.commit()
    flash("Community meeting cleared." if clearing else "Community meeting updated.", "success")
    return redirect(url_for("dashboard"))
