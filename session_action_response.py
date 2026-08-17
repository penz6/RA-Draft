from flask import flash, redirect, request, url_for


def async_session_action_requested():
    return request.headers.get("X-RA-Draft-Async") == "1"


def session_action_response(session_id, message, *, category="success", status=200):
    """Return JSON for enhanced session actions, otherwise preserve POST/redirect."""
    if async_session_action_requested():
        return {
            "ok": category != "error",
            "message": message,
        }, status

    flash(message, category)
    return redirect(url_for("view_session", session_id=session_id))
