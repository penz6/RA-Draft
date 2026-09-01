"""Keep dashboard live state in sync with duty assignment changes."""

import sys

from flask import g, request

from core import app, db
import live_updates


_base_dashboard_state_version = live_updates.dashboard_state_version


def dashboard_state_version_with_assignments(user):
    """Include the viewer's duty assignments and partners in dashboard live state."""
    base_version = _base_dashboard_state_version(user)
    rows = db().execute(
        "SELECT a.id,a.session_id,a.user_id,a.duty_date,u.name,s.shift_start,s.shift_end "
        "FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "JOIN draft_sessions s ON s.id=a.session_id "
        "WHERE EXISTS ("
        "SELECT 1 FROM assignments mine "
        "WHERE mine.session_id=a.session_id AND mine.duty_date=a.duty_date "
        "AND mine.user_id=?"
        ") ORDER BY a.session_id,a.duty_date,a.id",
        (user["id"],),
    ).fetchall()
    schedule_state = [
        [
            row["id"],
            row["session_id"],
            row["user_id"],
            row["duty_date"],
            row["name"],
            row["shift_start"],
            row["shift_end"],
        ]
        for row in rows
    ]
    return live_updates._digest(
        {
            "base": base_version,
            "viewer_duty_schedule": schedule_state,
        }
    )


dashboard_state_version_with_assignments._duty_shift_schedule_aware = True
live_updates.dashboard_state_version = dashboard_state_version_with_assignments

# portal_app imports the original function into its module namespace before route
# modules load, so update that reference too.
portal_module = sys.modules.get("portal_app")
if portal_module is not None:
    portal_module.dashboard_state_version = dashboard_state_version_with_assignments


@app.after_request
def publish_manual_swap_dashboard_change(response):
    """Manual manager swaps also invalidate open dashboard tabs after commit."""
    if request.endpoint == "manager_manual_swap" and getattr(g, "live_commits", 0):
        live_updates.publish_live_topics("dashboard:all")
    return response
