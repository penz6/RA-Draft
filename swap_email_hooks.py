"""Observe committed duty-swap transitions and dispatch notification emails."""

from flask import g, request

from core import app, current_user, db
from email_notifications import (
    MAIL_ENABLED,
    send_hra_swap_review_notification,
    send_swap_approved_notifications,
    send_swap_request_notification,
)


_STATE_KEY = "ra_draft_swap_email_state"


def _batch_status(batch_id):
    row = db().execute(
        "SELECT status FROM duty_swap_requests WHERE batch_id=? ORDER BY id LIMIT 1",
        (batch_id,),
    ).fetchone()
    return row["status"] if row else None


@app.before_request
def capture_swap_email_state():
    """Capture enough pre-request state to recognize successful swap transitions."""
    if not MAIL_ENABLED:
        return

    endpoint = request.endpoint
    view_args = request.view_args or {}

    if endpoint == "request_swap_batch":
        user = current_user()
        session_id = view_args.get("session_id")
        if not user or session_id is None:
            return
        row = db().execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM duty_swap_requests "
            "WHERE session_id=? AND requester_user_id=?",
            (session_id, user["id"]),
        ).fetchone()
        setattr(
            g,
            _STATE_KEY,
            {
                "kind": "request",
                "session_id": session_id,
                "requester_user_id": user["id"],
                "max_id": row["max_id"],
            },
        )
        return

    if endpoint not in {"target_review_swap", "hra_review_swap"}:
        return

    batch_id = view_args.get("batch_id")
    if not batch_id:
        return
    user = current_user()
    setattr(
        g,
        _STATE_KEY,
        {
            "kind": "target_review" if endpoint == "target_review_swap" else "hra_review",
            "batch_id": batch_id,
            "before_status": _batch_status(batch_id),
            "reviewer_user_id": user["id"] if user else None,
        },
    )


@app.after_request
def dispatch_swap_email_notifications(response):
    """Send email only when the underlying request committed the expected new state."""
    if not MAIL_ENABLED or response.status_code >= 400:
        return response

    state = getattr(g, _STATE_KEY, None)
    if not state:
        return response

    try:
        if state["kind"] == "request":
            new_batches = db().execute(
                "SELECT DISTINCT batch_id FROM duty_swap_requests "
                "WHERE session_id=? AND requester_user_id=? AND id>? "
                "AND status='PENDING' AND batch_id IS NOT NULL ORDER BY id",
                (
                    state["session_id"],
                    state["requester_user_id"],
                    state["max_id"],
                ),
            ).fetchall()
            for row in new_batches:
                send_swap_request_notification(row["batch_id"])

        elif state["kind"] == "target_review":
            if (
                state["before_status"] == "PENDING"
                and _batch_status(state["batch_id"]) == "TARGET_APPROVED"
            ):
                send_hra_swap_review_notification(state["batch_id"])

        elif state["kind"] == "hra_review":
            if (
                state["before_status"] == "TARGET_APPROVED"
                and _batch_status(state["batch_id"]) == "APPROVED"
                and state["reviewer_user_id"] is not None
            ):
                send_swap_approved_notifications(
                    state["batch_id"], state["reviewer_user_id"]
                )
    except Exception:  # Notification failures must never change a committed swap result.
        app.logger.exception("Could not prepare or dispatch an RA Draft swap email notification.")

    return response
