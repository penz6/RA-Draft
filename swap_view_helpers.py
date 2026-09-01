"""View helpers for participant duty-swap history."""

from core import app, session_swap_requests


def participant_swap_batches(session_id, user_id):
    """Return swap batches involving a user, oriented for that user's display."""
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return []

    batches = {}
    for row in session_swap_requests(session_id):
        requester_id = row["requester_user_id"]
        target_id = row["target_user_id"]
        if user_id not in (requester_id, target_id):
            continue

        # A pending target already sees the swap in the actionable Incoming requests
        # section. Once it leaves PENDING, keep it visible here permanently.
        if target_id == user_id and row["status"] == "PENDING":
            continue

        batch_id = row["batch_id"] or str(row["id"])
        if batch_id not in batches:
            viewer_is_requester = requester_id == user_id
            manager_manual = bool(
                row["status"] == "APPROVED"
                and row["reviewed_by"] is not None
                and row["created_at"] == row["target_reviewed_at"]
                and row["created_at"] == row["reviewed_at"]
            )
            batches[batch_id] = {
                "batch_id": batch_id,
                "status": row["status"],
                "requester_user_id": requester_id,
                "requester_name": row["requester_name"],
                "target_user_id": target_id,
                "target_name": row["target_name"],
                "other_name": (
                    row["target_name"] if viewer_is_requester else row["requester_name"]
                ),
                "viewer_is_requester": viewer_is_requester,
                "created_at": row["created_at"],
                "manager_manual": manager_manual,
                "manager_name": row["reviewer_name"] if manager_manual else None,
                "pairs": [],
            }

        batches[batch_id]["pairs"].append(
            {
                "requester_date": row["requester_date"],
                "target_date": row["target_date"],
            }
        )

    return list(batches.values())


app.jinja_env.globals["participant_swap_batches"] = participant_swap_batches
