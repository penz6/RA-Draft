"""Persistent, opt-in deadline reminders. Run as a separate Compose worker."""

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone

import email_notifications as mail
from core import app, db
from deadlines import load_deadlines
from staff_event_schedule import SCHOOL_TIMEZONE


def reminder_key(item):
    # Include the due date/time so a rescheduled deadline gets fresh reminders.
    value = f"{item['title']}\n{item['due'].isoformat()}"
    return hashlib.sha256(value.encode()).hexdigest()


def _claim(user_id, candidates, now):
    """Claim unsent reminders atomically; expired claims recover after a crash."""
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    recipient = conn.execute(
        "SELECT id,name,email FROM users WHERE id=? AND disabled=0 AND deadline_email_opt_in=1",
        (user_id,),
    ).fetchone()
    if not recipient:
        conn.rollback()
        return None, None, []
    token = secrets.token_hex(16)
    lease = (now + timedelta(minutes=15)).isoformat()
    claimed = []
    for item, days in candidates:
        key = reminder_key(item)
        changed = conn.execute(
            "INSERT INTO deadline_email_deliveries "
            "(user_id,deadline_key,reminder_days,claim_token,lease_until) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id,deadline_key,reminder_days) DO UPDATE SET "
            "claim_token=excluded.claim_token,lease_until=excluded.lease_until "
            "WHERE deadline_email_deliveries.sent_at IS NULL "
            "AND deadline_email_deliveries.lease_until <= ?",
            (user_id, key, days, token, lease, now.isoformat()),
        ).rowcount
        if changed:
            claimed.append((item, days))
    conn.commit()
    return recipient, token, claimed


def _message(recipient, reminders):
    url = mail._public_url("/deadlines")
    entries = [{**item, "when": "Due today" if days == 0 else "Due in 3 days"}
               for item, days in reminders]
    lines = "\n".join(f"- {item['when']}: {item['title']} — {item['label']}" for item in entries)
    first_name = mail._first_name(recipient["name"])
    text = (
        f"Hi {first_name},\n\nYour deadline reminders:\n{lines}\n\n"
        "*Refer to your AC for confirmed deadlines.\n\n"
        f"View deadlines or turn off email reminders: {url}\n"
    )
    return mail._build_message(
        recipient=recipient, subject="Your Residence Life deadline reminders",
        template="emails/deadline_reminder.html", text_body=text,
        context={"first_name": first_name, "entries": entries, "deadlines_url": url},
    )


def send_due_reminders(now=None):
    """Send today's 9 a.m. Eastern batch, retrying failures later the same day."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(SCHOOL_TIMEZONE)
    now = now.astimezone(timezone.utc)
    if not mail.MAIL_ENABLED or local.hour < 9:
        return 0
    state = load_deadlines()
    # Do not send stale data when the source is unavailable or malformed.
    if state["notice"]:
        return 0
    candidates = []
    seen = set()
    for item in state["items"]:
        days = (item["day"] - local.date()).days
        key = reminder_key(item)
        if days in (0, 3) and item["due"] >= local and key not in seen:
            candidates.append((item, days))
            seen.add(key)
    if not candidates:
        return 0
    users = db().execute(
        "SELECT id FROM users WHERE deadline_email_opt_in=1 AND disabled=0"
    ).fetchall()
    sent = 0
    for user in users:
        recipient, token, claimed = _claim(user["id"], candidates, now)
        if not claimed:
            continue
        delivered = False
        try:
            # Recheck consent immediately before SMTP without holding a DB lock.
            active = db().execute(
                "SELECT 1 FROM users WHERE id=? AND deadline_email_opt_in=1 AND disabled=0",
                (user["id"],),
            ).fetchone()
            if active:
                delivered = mail._deliver([_message(recipient, claimed)]) == 1
        except Exception:
            app.logger.exception("Deadline reminder delivery failed")
        conn = db()
        if delivered:
            conn.execute(
                "UPDATE deadline_email_deliveries SET sent_at=? WHERE user_id=? AND claim_token=?",
                (now.isoformat(), user["id"], token),
            )
            sent += 1
        else:
            conn.execute(
                "DELETE FROM deadline_email_deliveries WHERE user_id=? AND claim_token=? AND sent_at IS NULL",
                (user["id"], token),
            )
        conn.commit()
    return sent


def main():
    while True:
        try:
            with app.app_context():
                send_due_reminders()
        except Exception:
            app.logger.exception("Deadline reminder worker failed; will retry")
        time.sleep(300)


if __name__ == "__main__":
    main()
