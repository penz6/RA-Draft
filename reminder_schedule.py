"""Shared reminder identity and channel-specific schedules."""

import hashlib

EMAIL_REMINDER_DAYS = (7, 0)
GROUPME_REMINDER_DAYS = (7, 0)


def reminder_key(item):
    # Public-content fingerprint for delivery deduplication, not a credential.
    value = f"{item['title']}\n{item['due'].isoformat()}"
    return hashlib.sha256(value.encode()).hexdigest()


def reminder_label(days):
    if days == 7:
        return "Due in 1 week"
    return "Due today"
