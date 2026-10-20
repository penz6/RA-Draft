"""Building-scoped, outbound-only GroupMe deadline bot connections.

Protocol: https://dev.groupme.com/tutorials/bots and /docs/v3#bots_post.
OAuth registration is handled in groupme_oauth; Admins can also connect manually
registered bots. Only encrypted Bot IDs are used for scheduled posts.
"""

import base64
import hashlib
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import abort, flash, redirect, request, url_for

from core import app, audit, current_user, db, login_required, require_csrf
from deadlines import load_deadlines
from reminder_schedule import GROUPME_REMINDER_DAYS, reminder_key, reminder_label
from staff_event_schedule import SCHOOL_TIMEZONE

POST_URL = "https://api.groupme.com/v3/bots/post"
BOT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,200}\Z")
app.config.setdefault("GROUPME_CLIENT_ID", os.environ.get("GROUPME_CLIENT_ID", "").strip())


def _cipher():
    secret = app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret + b"\0groupme-bot").digest()))


def _can_manage(user, building_id):
    return bool(user and not user["disabled"] and (
        user["role"] == "ADMIN" or
        (user["role"] == "HRA" and user["building_id"] == building_id)
    ))


def _connection(building_id, bot_hash=None, *, enabled=True):
    """Recheck the configuring account as well as the current destination."""
    row = db().execute(
        "SELECT b.*,u.role,u.disabled,u.building_id AS owner_building_id "
        "FROM building_groupme_bots b LEFT JOIN users u ON u.id=b.configured_by "
        "WHERE b.building_id=?", (building_id,),
    ).fetchone()
    if not row or (bot_hash is not None and row["bot_hash"] != bot_hash):
        return None
    if enabled and not row["enabled"]:
        return None
    if row["disabled"] != 0 or not (
        row["role"] == "ADMIN" or
        (row["role"] == "HRA" and row["owner_building_id"] == building_id)
    ):
        return None
    return row


@app.context_processor
def inject_groupme_settings():
    if request.endpoint != "deadline_calendar":
        return {}
    user = current_user()
    if not user or user["role"] not in ("ADMIN", "HRA") or (user["role"] == "HRA" and not user["building_id"]):
        return {}
    buildings = db().execute("SELECT id,name FROM buildings ORDER BY name,id").fetchall()
    if user["role"] != "ADMIN":
        buildings = [b for b in buildings if b["id"] == user["building_id"]]
    if not buildings:
        return {}
    selected_id = request.args.get("groupme_building", type=int) or user["building_id"]
    selected = next((b for b in buildings if b["id"] == selected_id), buildings[0])
    row = db().execute(
        "SELECT enabled,last_sent_at,last_error,group_name FROM building_groupme_bots WHERE building_id=?",
        (selected["id"],),
    ).fetchone()
    return {"groupme_building_id": selected["id"], "groupme_building_name": selected["name"],
            "groupme_buildings": buildings,
            "groupme_oauth_ready": bool(app.config["GROUPME_CLIENT_ID"]),
            "groupme_connection": dict(row) if row else None,
            "groupme_authorized": bool(_connection(selected["id"], enabled=False))}


def post_message(bot_ciphertext, text):
    """Do not expose credentials in URLs, exceptions, response bodies or logs."""
    if not text or len(text) > 1000:
        return False, "Message exceeds the allowed length."
    try:
        bot_id = _cipher().decrypt(bot_ciphertext.encode()).decode()
    except (InvalidToken, ValueError, UnicodeError):
        return False, "Reconnect GroupMe to restore this bot connection."
    try:
        with requests.post(POST_URL, json={"bot_id": bot_id, "text": text},
                           timeout=(3, 10), allow_redirects=False) as response:
            if response.status_code == 201:
                return True, None
            if response.status_code in (400, 401, 403, 404):
                return False, "GroupMe rejected the post. Reconnect GroupMe or ask an administrator to check that the bot is active."
            return False, "GroupMe is unavailable or limiting requests. Delivery will retry on a later scan."
    except requests.RequestException:
        return False, "Could not reach GroupMe. Delivery will retry on a later scan."


def _record_result(connection, delivered, error, now):
    db().execute(
        "UPDATE building_groupme_bots SET last_error=?,"
        "last_sent_at=CASE WHEN ? THEN ? ELSE last_sent_at END "
        "WHERE building_id=? AND bot_hash=?",
        (error, int(delivered), now.isoformat(), connection["building_id"], connection["bot_hash"]),
    )


@app.route("/buildings/<int:building_id>/groupme", methods=["POST"])
@login_required
def groupme_settings(building_id):
    require_csrf()
    user = current_user()
    destination = url_for("deadline_calendar", groupme_building=building_id)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    # Read again under the write lock, so role/building changes cannot race a save.
    user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    if not _can_manage(user, building_id):
        conn.rollback()
        abort(403)
    if not conn.execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        conn.rollback()
        abort(404)
    action = request.form.get("action", "save")
    existing = conn.execute(
        "SELECT * FROM building_groupme_bots WHERE building_id=?", (building_id,),
    ).fetchone()
    if action == "disconnect":
        conn.execute("DELETE FROM building_groupme_bots WHERE building_id=?", (building_id,))
        audit("deadlines.groupme.disconnect", "building", building_id)
        conn.commit()
        flash("GroupMe disconnected. Deadline posts have stopped.", "success")
    elif action == "save":
        bot_id = request.form.get("bot_id", "").strip()
        if user["role"] != "ADMIN" and (bot_id or not existing):
            conn.rollback()
            abort(403)
        if (not existing and not bot_id) or (bot_id and not BOT_ID_PATTERN.fullmatch(bot_id)):
            conn.rollback()
            flash("Enter a valid Bot ID from GroupMe’s developer page.", "error")
            return redirect(destination)
        ciphertext = _cipher().encrypt(bot_id.encode()).decode() if bot_id else existing["bot_ciphertext"]
        bot_hash = hashlib.sha256(bot_id.encode()).hexdigest() if bot_id else existing["bot_hash"]
        enabled = int(request.form.get("groupme_reminders") == "1")
        try:
            conn.execute(
                "INSERT INTO building_groupme_bots "
                "(building_id,bot_ciphertext,bot_hash,enabled,configured_by) VALUES(?,?,?,?,?) "
                "ON CONFLICT(building_id) DO UPDATE SET bot_ciphertext=excluded.bot_ciphertext,"
                "bot_hash=excluded.bot_hash,enabled=excluded.enabled,configured_by=excluded.configured_by,"
                "last_error=NULL,last_sent_at=CASE WHEN bot_hash=excluded.bot_hash THEN last_sent_at ELSE NULL END,"
                "group_name=CASE WHEN bot_hash=excluded.bot_hash THEN group_name ELSE NULL END",
                (building_id, ciphertext, bot_hash, enabled, user["id"]),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("That bot is already connected. Create a separate bot for this building’s group.", "error")
            return redirect(destination)
        audit("deadlines.groupme.save", "building", building_id, {"enabled": bool(enabled)})
        conn.commit()
        flash("GroupMe settings saved. Saving does not send a test message.", "success")
    elif action == "test":
        now = datetime.now(timezone.utc)
        connection = _connection(building_id, enabled=False)
        if not connection:
            conn.rollback()
            flash("Save your GroupMe connection first.", "error")
            return redirect(destination)
        if time.time() - connection["last_test_at"] < 60:
            conn.rollback()
            flash("Wait a minute before sending another test message.", "error")
            return redirect(destination)
        conn.execute("UPDATE building_groupme_bots SET last_test_at=? WHERE building_id=?",
                     (time.time(), building_id))
        audit("deadlines.groupme.test", "building", building_id)
        conn.commit()
        if _connection(building_id, connection["bot_hash"], enabled=False):
            delivered, error = post_message(connection["bot_ciphertext"], "📅 Deadline reminders are connected to this group.")
            _record_result(connection, delivered, error, now)
            conn.commit()
            flash("Test message sent to your GroupMe group." if delivered else error,
                  "success" if delivered else "error")
    else:
        conn.rollback()
        abort(400)
    return redirect(destination)


def deadline_message(item, days):
    # Only titles and dates; no descriptions, links, or duty notifications.
    title = " ".join(item["title"].split())[:200]
    return f"📅 {reminder_label(days)}\n{title}\nDue {item['label']}"


def _claim(connection, item, days, now):
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    if not _connection(connection["building_id"], connection["bot_hash"]):
        conn.rollback()
        return None
    token = secrets.token_hex(16)
    changed = conn.execute(
        "INSERT INTO groupme_deadline_deliveries "
        "(building_id,bot_hash,deadline_key,reminder_days,claim_token,lease_until) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(building_id,bot_hash,deadline_key,reminder_days) DO UPDATE SET "
        "claim_token=excluded.claim_token,lease_until=excluded.lease_until "
        "WHERE groupme_deadline_deliveries.sent_at IS NULL AND groupme_deadline_deliveries.lease_until <= ?",
        (connection["building_id"], connection["bot_hash"], reminder_key(item), days, token,
         (now + timedelta(minutes=15)).isoformat(), now.isoformat()),
    ).rowcount
    conn.commit()
    return token if changed else None


def send_due_groupme_reminders(now=None):
    db().execute("DELETE FROM groupme_oauth_flows WHERE expires_at < ?", (time.time(),))
    db().commit()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = now.astimezone(SCHOOL_TIMEZONE)
    if local.hour < 9:
        return 0
    state = load_deadlines()
    if state["notice"]:
        return 0
    candidates = [(item, (item["day"] - local.date()).days) for item in state["items"]
                  if (item["day"] - local.date()).days in GROUPME_REMINDER_DAYS]
    if not candidates:
        return 0
    connections = db().execute("SELECT * FROM building_groupme_bots WHERE enabled=1").fetchall()
    sent = 0
    for connection in connections:
        for item, days in candidates:
            token = _claim(connection, item, days, now)
            if not token:
                continue
            delivered, error = False, None
            if _connection(connection["building_id"], connection["bot_hash"]):
                delivered, error = post_message(connection["bot_ciphertext"], deadline_message(item, days))
            if delivered:
                db().execute(
                    "UPDATE groupme_deadline_deliveries SET sent_at=? "
                    "WHERE building_id=? AND claim_token=?",
                    (now.isoformat(), connection["building_id"], token),
                )
                sent += 1
            else:
                db().execute(
                    "DELETE FROM groupme_deadline_deliveries "
                    "WHERE building_id=? AND claim_token=? AND sent_at IS NULL",
                    (connection["building_id"], token),
                )
            _record_result(connection, delivered, error, now)
            db().commit()
            # Avoid bursts after a provider failure/rate limit. Retry next scan.
            if error:
                break
    return sent
