"""Require managers to confirm the shared Google Calendar is updated before a swap."""

from flask import flash, redirect, request, url_for

from core import app, can_manage, current_user, db, require_csrf, session_row


_CONFIRMATION_FIELD = "google_calendar_updated"
_CONFIRMATION_MESSAGE = (
    "Confirm that you updated the shared Google Calendar before approving this swap."
)


def _confirmation_missing():
    return request.form.get(_CONFIRMATION_FIELD) != "1"


def _calendar_confirmation_error(session_id):
    flash(_CONFIRMATION_MESSAGE, "error")
    return redirect(url_for("swap_page", session_id=session_id))


@app.before_request
def require_swap_calendar_confirmation():
    """Enforce the manager calendar checklist on swap-changing POST requests."""
    if request.method != "POST":
        return None

    endpoint = request.endpoint
    if endpoint == "manager_manual_swap":
        session_id = (request.view_args or {}).get("session_id")
        if session_id is None:
            return None
        manager = current_user()
        row = session_row(session_id)
        if not row or not can_manage(manager, row):
            return None
        require_csrf()
        if _confirmation_missing():
            return _calendar_confirmation_error(session_id)
        return None

    if endpoint == "hra_review_swap":
        if request.form.get("action", "").strip().upper() != "APPROVE":
            return None
        batch_id = (request.view_args or {}).get("batch_id")
        if not batch_id:
            return None
        swap = db().execute(
            "SELECT session_id FROM duty_swap_requests WHERE batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone()
        if not swap:
            return None
        session_id = swap["session_id"]
        manager = current_user()
        row = session_row(session_id)
        if not row or not can_manage(manager, row):
            return None
        require_csrf()
        if _confirmation_missing():
            return _calendar_confirmation_error(session_id)

    return None
