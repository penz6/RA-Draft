"""Unified helper for returning standard JSON or redirect responses on session actions."""

from flask import flash, jsonify, redirect, request, url_for


def session_action_response(session_id, message, category="success", status=200):
    """Return JSON for background live clients, or flash and redirect for standard forms."""
    if (
        request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    ):
        return jsonify({"ok": category != "error", "message": message, "category": category}), status
    flash(message, category)
    return redirect(url_for("view_session", session_id=session_id))
