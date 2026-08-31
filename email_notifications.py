"""Branded email notifications for finalized schedules and duty swaps."""

import os
import smtplib
import ssl
from collections import defaultdict
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr

from flask import render_template

from core import PUBLIC_HOST, app, db


MAIL_ENABLED = os.environ.get("MAIL_ENABLED", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
MAIL_HOST = os.environ.get("MAIL_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
try:
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
except ValueError as exc:
    raise RuntimeError("MAIL_PORT must be an integer.") from exc
MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "").strip()
MAIL_APP_PASSWORD = os.environ.get("MAIL_APP_PASSWORD", "").strip()
MAIL_FROM_NAME = "Duty Shifts"
MAIL_TIMEOUT_SECONDS = 10

if MAIL_ENABLED and (not MAIL_USERNAME or not MAIL_APP_PASSWORD):
    raise RuntimeError(
        "MAIL_USERNAME and MAIL_APP_PASSWORD must be set when MAIL_ENABLED is enabled."
    )


def _public_url(path):
    """Build an HTTPS application URL from the configured public hostname."""
    return f"https://{PUBLIC_HOST}{path}"


def _date_label(raw_value):
    """Format an ISO duty date for human-readable email copy."""
    parsed = date.fromisoformat(str(raw_value))
    return f"{parsed.strftime('%A, %B')} {parsed.day}, {parsed.year}"


def _first_name(full_name):
    cleaned = " ".join(str(full_name or "").split())
    return cleaned.split(" ", 1)[0] if cleaned else "there"


def _build_message(*, recipient, subject, template, text_body, context):
    message = EmailMessage()
    message["From"] = formataddr((MAIL_FROM_NAME, MAIL_USERNAME))
    message["To"] = formataddr((recipient["name"], recipient["email"]))
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(render_template(template, **context), subtype="html")
    return message


def _deliver(messages):
    """Deliver one or more messages over authenticated STARTTLS SMTP."""
    messages = list(messages)
    if not MAIL_ENABLED or not messages:
        return 0

    sent = 0
    try:
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=MAIL_TIMEOUT_SECONDS) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(MAIL_USERNAME, MAIL_APP_PASSWORD)

            for message in messages:
                try:
                    smtp.send_message(message)
                    sent += 1
                except (OSError, smtplib.SMTPException):
                    app.logger.exception(
                        "Could not send Duty Shift email to %s.",
                        message.get("To", "unknown recipient"),
                    )
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Could not connect or authenticate to the configured SMTP server.")

    return sent


def _session_details(session_id):
    return db().execute(
        "SELECT s.id,s.name,s.building_id,b.name building_name "
        "FROM draft_sessions s JOIN buildings b ON b.id=s.building_id "
        "WHERE s.id=?",
        (session_id,),
    ).fetchone()


def send_session_closed_notifications(session_id):
    """Email every participant their finalized duty dates when a session closes."""
    session = _session_details(session_id)
    if not session:
        return 0

    conn = db()
    participants = conn.execute(
        "SELECT u.id,u.name,u.email,o.position FROM session_order o "
        "JOIN users u ON u.id=o.user_id "
        "WHERE o.session_id=? AND u.disabled=0 ORDER BY o.position",
        (session_id,),
    ).fetchall()
    assignments = conn.execute(
        "SELECT a.user_id,a.duty_date,u.name FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "WHERE a.session_id=? ORDER BY a.duty_date,a.id",
        (session_id,),
    ).fetchall()

    assignments_by_user = defaultdict(list)
    assignees_by_date = defaultdict(list)
    for assignment in assignments:
        assignments_by_user[assignment["user_id"]].append(assignment["duty_date"])
        assignees_by_date[assignment["duty_date"]].append(
            (assignment["user_id"], assignment["name"])
        )

    messages = []
    for participant in participants:
        shifts = []
        for duty_date in assignments_by_user.get(participant["id"], []):
            partners = [
                name
                for user_id, name in assignees_by_date[duty_date]
                if user_id != participant["id"]
            ]
            shifts.append(
                {
                    "duty_date": duty_date,
                    "date_label": _date_label(duty_date),
                    "partners": partners,
                }
            )

        calendar_url = _public_url(
            f"/calendar/session/{session_id}/my-calendar.ics"
        )
        session_url = _public_url(f"/sessions/{session_id}")
        swap_url = _public_url(f"/swaps/session/{session_id}")
        context = {
            "first_name": _first_name(participant["name"]),
            "session": session,
            "shifts": shifts,
            "calendar_url": calendar_url,
            "session_url": session_url,
            "swap_url": swap_url,
        }
        if shifts:
            shift_lines = "\n".join(
                f"- {shift['date_label']} — "
                + (
                    f"On duty with: {', '.join(shift['partners'])}"
                    if shift["partners"]
                    else "Solo duty"
                )
                for shift in shifts
            )
        else:
            shift_lines = "- No duty shifts were assigned to you."

        text_body = (
            f"Hi {context['first_name']},\n\n"
            f"Your Duty Shift schedule for {session['name']} in "
            f"{session['building_name']} is ready.\n\n"
            f"Your duty dates:\n{shift_lines}\n\n"
            f"Download your iCal calendar: {calendar_url}\n"
            f"Duty Swaps: {swap_url}\n"
            f"View the session: {session_url}\n\n"
            "Duty Shift"
        )
        messages.append(
            _build_message(
                recipient=participant,
                subject=f"Your {session['building_name']} duty schedule is ready",
                template="emails/schedule_ready.html",
                text_body=text_body,
                context=context,
            )
        )

    return _deliver(messages)


def _swap_details(batch_id):
    rows = db().execute(
        "SELECT sr.id,sr.batch_id,sr.session_id,sr.requester_user_id,"
        "sr.target_user_id,req.name requester_name,req.email requester_email,"
        "target.name target_name,target.email target_email,"
        "req_a.duty_date requester_date,target_a.duty_date target_date,"
        "s.name session_name,s.building_id,b.name building_name "
        "FROM duty_swap_requests sr "
        "JOIN users req ON req.id=sr.requester_user_id "
        "JOIN users target ON target.id=sr.target_user_id "
        "JOIN assignments req_a ON req_a.id=sr.requester_assignment_id "
        "JOIN assignments target_a ON target_a.id=sr.target_assignment_id "
        "JOIN draft_sessions s ON s.id=sr.session_id "
        "JOIN buildings b ON b.id=s.building_id "
        "WHERE sr.batch_id=? ORDER BY sr.id",
        (batch_id,),
    ).fetchall()
    if not rows:
        return None

    first = rows[0]
    return {
        "batch_id": batch_id,
        "session_id": first["session_id"],
        "session_name": first["session_name"],
        "building_id": first["building_id"],
        "building_name": first["building_name"],
        "requester": {
            "id": first["requester_user_id"],
            "name": first["requester_name"],
            "email": first["requester_email"],
        },
        "target": {
            "id": first["target_user_id"],
            "name": first["target_name"],
            "email": first["target_email"],
        },
        "pairs": [
            {
                "requester_date": row["requester_date"],
                "requester_date_label": _date_label(row["requester_date"]),
                "target_date": row["target_date"],
                "target_date_label": _date_label(row["target_date"]),
            }
            for row in rows
        ],
    }


def send_swap_request_notification(batch_id):
    """Notify the target RA that another RA requested a duty swap."""
    swap = _swap_details(batch_id)
    if not swap:
        return 0

    review_url = _public_url(f"/swaps/session/{swap['session_id']}")
    context = {
        "first_name": _first_name(swap["target"]["name"]),
        "swap": swap,
        "review_url": review_url,
    }
    pair_lines = "\n".join(
        f"- {pair['requester_date_label']} ↔ {pair['target_date_label']}"
        for pair in swap["pairs"]
    )
    text_body = (
        f"Hi {context['first_name']},\n\n"
        f"{swap['requester']['name']} requested a duty swap with you in "
        f"{swap['building_name']} ({swap['session_name']}).\n\n"
        f"Requested swap:\n{pair_lines}\n\n"
        f"Review the request in Duty Shift: {review_url}\n\n"
        "Duty Shift"
    )
    message = _build_message(
        recipient=swap["target"],
        subject=f"Duty swap request from {swap['requester']['name']}",
        template="emails/swap_requested.html",
        text_body=text_body,
        context=context,
    )
    return _deliver([message])


def send_hra_swap_review_notification(batch_id):
    """Notify the building HRA when both RAs have agreed to a swap."""
    swap = _swap_details(batch_id)
    if not swap:
        return 0

    conn = db()
    reviewers = conn.execute(
        "SELECT id,name,email FROM users "
        "WHERE role='HRA' AND building_id=? AND disabled=0 ORDER BY name",
        (swap["building_id"],),
    ).fetchall()
    if not reviewers:
        reviewers = conn.execute(
            "SELECT id,name,email FROM users "
            "WHERE role='ADMIN' AND disabled=0 ORDER BY name"
        ).fetchall()

    review_url = _public_url(f"/swaps/session/{swap['session_id']}")
    messages = []
    pair_lines = "\n".join(
        f"- {swap['requester']['name']}: {pair['requester_date_label']} ↔ "
        f"{swap['target']['name']}: {pair['target_date_label']}"
        for pair in swap["pairs"]
    )
    for reviewer in reviewers:
        context = {
            "first_name": _first_name(reviewer["name"]),
            "swap": swap,
            "review_url": review_url,
        }
        text_body = (
            f"Hi {context['first_name']},\n\n"
            f"{swap['requester']['name']} and {swap['target']['name']} have both "
            f"agreed to a duty swap in {swap['building_name']} "
            f"({swap['session_name']}). Final approval is required.\n\n"
            f"Swap details:\n{pair_lines}\n\n"
            f"Review the swap in Duty Shift: {review_url}\n\n"
            "Duty Shift"
        )
        messages.append(
            _build_message(
                recipient=reviewer,
                subject=(
                    "Duty swap awaiting approval: "
                    f"{swap['requester']['name']} ↔ {swap['target']['name']}"
                ),
                template="emails/swap_hra_review.html",
                text_body=text_body,
                context=context,
            )
        )

    return _deliver(messages)


def send_swap_approved_notifications(batch_id, reviewer_user_id):
    """Confirm an approved swap to both RAs and the approving HRA/Admin."""
    swap = _swap_details(batch_id)
    if not swap:
        return 0

    conn = db()
    reviewer = conn.execute(
        "SELECT id,name,email FROM users WHERE id=?",
        (reviewer_user_id,),
    ).fetchone()

    recipients = [swap["requester"], swap["target"]]
    if reviewer:
        recipients.append(
            {
                "id": reviewer["id"],
                "name": reviewer["name"],
                "email": reviewer["email"],
            }
        )

    session_url = _public_url(f"/sessions/{swap['session_id']}")
    swap_url = _public_url(f"/swaps/session/{swap['session_id']}")
    pair_lines = "\n".join(
        f"- {swap['requester']['name']}: {pair['requester_date_label']} → "
        f"{pair['target_date_label']}; "
        f"{swap['target']['name']}: {pair['target_date_label']} → "
        f"{pair['requester_date_label']}"
        for pair in swap["pairs"]
    )

    messages = []
    seen_emails = set()
    for recipient in recipients:
        normalized_email = recipient["email"].strip().lower()
        if not normalized_email or normalized_email in seen_emails:
            continue
        seen_emails.add(normalized_email)

        is_ra_party = recipient["id"] in {
            swap["requester"]["id"],
            swap["target"]["id"],
        }
        calendar_url = (
            _public_url(f"/calendar/session/{swap['session_id']}/my-calendar.ics")
            if is_ra_party
            else None
        )
        context = {
            "first_name": _first_name(recipient["name"]),
            "swap": swap,
            "session_url": session_url,
            "swap_url": swap_url,
            "calendar_url": calendar_url,
            "show_calendar": bool(calendar_url),
        }
        text_body = (
            f"Hi {context['first_name']},\n\n"
            f"The duty swap between {swap['requester']['name']} and "
            f"{swap['target']['name']} has been approved. The Duty Shift schedule "
            f"has been updated.\n\n"
            f"Updated assignments:\n{pair_lines}\n\n"
            f"View the session: {session_url}\n"
            + (f"Download your updated iCal calendar: {calendar_url}\n" if calendar_url else "")
            + f"Duty Swaps: {swap_url}\n\n"
            "Duty Shift"
        )
        messages.append(
            _build_message(
                recipient=recipient,
                subject=(
                    "Duty swap approved: "
                    f"{swap['requester']['name']} ↔ {swap['target']['name']}"
                ),
                template="emails/swap_approved.html",
                text_body=text_body,
                context=context,
            )
        )

    return _deliver(messages)
