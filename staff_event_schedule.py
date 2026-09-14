"""School-local weekly recurrence, independent of Flask and database writes."""

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

SCHOOL_TIMEZONE = ZoneInfo("America/New_York")
MAX_REPEAT_WEEKS = 52
_LOCAL_MINUTE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}")


def school_now():
    return datetime.now(SCHOOL_TIMEZONE)


def parse_repeat_weeks(value):
    """Blank or zero means a one-time event; otherwise require whole weeks."""
    raw = str(value or "").strip()
    if raw in ("", "0"):
        return 0
    if not re.fullmatch(r"[0-9]{1,2}", raw) or not 1 <= int(raw) <= MAX_REPEAT_WEEKS:
        raise ValueError("Repeat interval must be a whole number from 1 to 52 weeks.")
    return int(raw)


def parse_event_start(value):
    raw = str(value or "").strip()
    if not _LOCAL_MINUTE.fullmatch(raw):
        raise ValueError("Choose a valid date and time.")
    try:
        start = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Choose a valid date and time.") from exc
    # datetime-local has no offset. Reject nonexistent spring-forward times;
    # repeated fall-back times use the first occurrence (fold=0).
    try:
        resolved = _resolve_local(start)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Choose a valid date and time.") from exc
    if resolved.replace(tzinfo=None) != start:
        raise ValueError("That time does not exist due to daylight saving time. Choose another time.")
    return start.isoformat(timespec="minutes")


def _resolve_local(wall_time):
    """Keep wall-clock time across DST; advance a future gap by its DST jump."""
    return wall_time.replace(tzinfo=SCHOOL_TIMEZONE, fold=0).astimezone(
        timezone.utc
    ).astimezone(SCHOOL_TIMEZONE)


def next_occurrence(value, repeat_weeks=0, *, now=None):
    """Return the next local ISO minute without moving the stored series anchor.

    A one-time event keeps its original date. Repeating events advance just
    after their start minute. Arithmetic jumps directly to the current cycle,
    including for old series; there is no loop over missed occurrences.
    """
    if not value:
        return None
    try:
        start = datetime.fromisoformat(str(value))
        if start.tzinfo is not None:
            start = start.astimezone(SCHOOL_TIMEZONE).replace(tzinfo=None)
        interval = parse_repeat_weeks(repeat_weeks)
    except (TypeError, ValueError):
        return None
    if not interval:
        return start.isoformat(timespec="minutes")
    current = now if now is not None else school_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SCHOOL_TIMEZONE)
    current = current.astimezone(SCHOOL_TIMEZONE).replace(second=0, microsecond=0)
    period = timedelta(weeks=interval)
    cycles = max(0, (current.replace(tzinfo=None) - start) // period)
    try:
        candidate = _resolve_local(start + cycles * period)
        if candidate.astimezone(timezone.utc) < current.astimezone(timezone.utc):
            candidate = _resolve_local(start + (cycles + 1) * period)
    except (OverflowError, ValueError):
        return None
    return candidate.replace(tzinfo=None).isoformat(timespec="minutes")


def repeat_label(value):
    interval = parse_repeat_weeks(value)
    if not interval:
        return ""
    return "Every week" if interval == 1 else f"Every {interval} weeks"
