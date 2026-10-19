"""Read the shared deadline feed from GitHub and render list/calendar views."""

import json
import os
from calendar import Calendar
from datetime import date, datetime, time, timedelta
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit
from pathlib import Path

import requests
from flask import g, render_template, request

from core import app, current_user, login_required
from staff_event_schedule import SCHOOL_TIMEZONE

app.config.setdefault("DEADLINES_JSON_URL", os.environ.get("DEADLINES_JSON_URL", "").strip())
_cache = {}
_lock = Lock()
_MAX_BYTES = 1024 * 1024
REPOSITORY_FEED = Path(__file__).with_name("deadlines.json")


def parse_deadlines(payload):
    """Validate the complete feed so a broken update cannot hide deadlines."""
    rows = payload.get("deadlines") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError("Expected a list of up to 2000 deadlines")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid deadline")
        title = row.get("title")
        raw_due = row.get("due_at") or row.get("due_date")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("A deadline needs a title")
        if not isinstance(raw_due, str):
            raise ValueError("A deadline needs an ISO due date")
        all_day = len(raw_due) == 10
        if all_day:
            day = date.fromisoformat(raw_due)
            due = datetime.combine(day, time.max, tzinfo=SCHOOL_TIMEZONE)
        else:
            due = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=SCHOOL_TIMEZONE)
            due = due.astimezone(SCHOOL_TIMEZONE)
        description = row.get("description", "")
        link = row.get("url", "")
        if not isinstance(description, str) or len(description) > 4000:
            raise ValueError("Invalid description")
        if not isinstance(link, str) or (link and (
                urlsplit(link).scheme != "https" or not urlsplit(link).hostname)):
            raise ValueError("Deadline links must use HTTPS")
        result.append({"title": title.strip(), "due": due, "day": due.date(),
                       "all_day": all_day, "description": description, "url": link,
                       "label": due.strftime("%b %d, %Y") + (
                           " · All day" if all_day else due.strftime(" · %-I:%M %p %Z"))})
    return sorted(result, key=lambda item: (item["due"], item["title"]))


def load_deadlines():
    """Cache per worker for five minutes, retaining the last successful feed."""
    source = app.config["DEADLINES_JSON_URL"]
    if not source:
        try:
            return {"items": parse_deadlines(json.loads(REPOSITORY_FEED.read_bytes())), "notice": ""}
        except (OSError, ValueError, TypeError, OverflowError):
            app.logger.warning("Could not read deadlines.json from the repository")
            return {"items": [], "notice": "Deadlines are temporarily unavailable. Please try again later."}
    try:
        parsed = urlsplit(source)
        valid_source = (parsed.scheme == "https" and parsed.hostname == "raw.githubusercontent.com"
                        and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        valid_source = False
    if not valid_source:
        return {"items": [], "notice": "The deadline feed is unavailable. Please contact an administrator."}
    with _lock:
        cached = _cache.get(source)
        if cached and monotonic() < cached["expires"]:
            return cached
        try:
            with requests.get(source, timeout=(3, 5), stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("Expected a JSON response")
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        raise ValueError("Feed too large")
                items = parse_deadlines(json.loads(body))
            state = {"items": items, "notice": "", "expires": monotonic() + 300}
        except (requests.RequestException, ValueError, TypeError, OverflowError):
            app.logger.warning("Could not refresh the deadline feed")
            state = {"items": cached["items"] if cached else [], "expires": monotonic() + 60,
                     "notice": "Updates are temporarily unavailable. Showing the last saved deadlines."
                     if cached and cached["items"] else "Deadlines are temporarily unavailable. Please try again later."}
        _cache[source] = state
        return state


def deadline_context():
    if "deadline_feed" not in g:
        state = load_deadlines()
        now = datetime.now(SCHOOL_TIMEZONE)
        g.deadline_feed = {"upcoming_deadlines": [item for item in state["items"] if item["due"] >= now],
                           "deadline_notice": state["notice"], "deadline_today": now.date()}
    return g.deadline_feed


@app.context_processor
def inject_deadlines():
    return deadline_context() if request.endpoint == "dashboard" and current_user() else {}


@app.route("/deadlines")
@login_required
def deadline_calendar():
    context = deadline_context()
    try:
        selected = datetime.strptime(request.args.get("month", ""), "%Y-%m").date().replace(day=1)
        if not 2 <= selected.year <= 9998:
            raise ValueError("Unsupported calendar year")
    except ValueError:
        selected = context["deadline_today"].replace(day=1)
    by_day = {}
    for item in context["upcoming_deadlines"]:
        if (item["day"].year, item["day"].month) == (selected.year, selected.month):
            by_day.setdefault(item["day"].day, []).append(item)
    return render_template(
        "deadlines.html", **context, prostaff_page="deadlines", deadline_by_day=by_day,
        deadline_weeks=Calendar().monthdayscalendar(selected.year, selected.month),
        selected_month=selected.strftime("%Y-%m"), month_label=selected.strftime("%B %Y"),
        previous_month=(selected - timedelta(days=1)).strftime("%Y-%m"),
        next_month=(selected.replace(day=28) + timedelta(days=4)).strftime("%Y-%m"),
    )
