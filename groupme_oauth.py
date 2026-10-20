"""Connect the owner's GroupMe application to an HRA-selected group.

GroupMe documents implicit OAuth returning access_token in the callback query.
https://dev.groupme.com/tutorials/oauth
"""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode

import requests
from cryptography.fernet import InvalidToken
from flask import abort, flash, redirect, render_template, request, session, url_for

from core import app, audit, current_user, db, login_required, require_csrf
from groupme_bot import BOT_ID_PATTERN, _can_manage, _cipher

API_URL = "https://api.groupme.com/v3"


class GroupMeError(Exception):
    pass


def _api(method, path, token, *, params=None, payload=None):
    """Account tokens only belong in headers. Never expose provider error text."""
    try:
        with requests.request(method, API_URL + path, headers={"X-Access-Token": token},
                              params=params, json=payload, timeout=(3, 10), allow_redirects=False) as response:
            if response.status_code != (201 if method == "POST" else 200):
                raise GroupMeError("GroupMe could not complete setup. Please reconnect and try again.")
            body = response.json()
            if not isinstance(body, dict) or "response" not in body:
                raise ValueError()
            return body["response"]
    except (requests.RequestException, ValueError):
        raise GroupMeError("Could not reach GroupMe. Please reconnect and try again.") from None


def _flow():
    nonce = session.get("groupme_oauth_flow", "")
    user = current_user()
    if not isinstance(nonce, str) or not nonce or not user:
        return None
    row = db().execute(
        "SELECT f.*,b.name AS building_name FROM groupme_oauth_flows f "
        "JOIN buildings b ON b.id=f.building_id WHERE f.state_hash=? AND f.user_id=? AND f.expires_at>?",
        (hashlib.sha256(nonce.encode()).hexdigest(), user["id"], time.time()),
    ).fetchone()
    if not row or not _can_manage(user, row["building_id"]):
        return None
    return row


def _clear_flow(flow):
    if flow:
        db().execute("DELETE FROM groupme_oauth_flows WHERE state_hash=?", (flow["state_hash"],))
        db().commit()
    session.pop("groupme_oauth_flow", None)


def _setup_error(message, flow=None):
    _clear_flow(flow)
    flash(message, "error")
    return redirect(url_for("deadline_calendar"))


@app.route("/buildings/<int:building_id>/groupme/connect", methods=["POST"])
@login_required
def groupme_connect(building_id):
    require_csrf()
    if not _can_manage(current_user(), building_id):
        abort(403)
    if not db().execute("SELECT 1 FROM buildings WHERE id=?", (building_id,)).fetchone():
        abort(404)
    if not app.config["GROUPME_CLIENT_ID"]:
        return _setup_error("GroupMe connection is not configured yet. Contact your administrator.")
    nonce = secrets.token_urlsafe(32)
    conn = db()
    conn.execute("DELETE FROM groupme_oauth_flows WHERE user_id=? OR expires_at<?",
                 (current_user()["id"], time.time()))
    original = conn.execute("SELECT bot_hash FROM building_groupme_bots WHERE building_id=?", (building_id,)).fetchone()
    conn.execute(
        "INSERT INTO groupme_oauth_flows(state_hash,user_id,building_id,expires_at,original_bot_hash) VALUES(?,?,?,?,?)",
        (hashlib.sha256(nonce.encode()).hexdigest(), current_user()["id"], building_id,
         time.time() + 600, original["bot_hash"] if original else None),
    )
    conn.commit()
    session["groupme_oauth_flow"] = nonce
    # GroupMe's documented authorization URL; no invented scopes/code exchange.
    return redirect("https://oauth.groupme.com/oauth/authorize?" + urlencode({
        "client_id": app.config["GROUPME_CLIENT_ID"], "state": nonce,
    }))


@app.route("/groupme/callback")
def groupme_oauth_callback():
    # A minimal first-party page immediately removes the query/fragment from
    # browser history, then sends the token in a CSRF-protected POST body.
    return render_template("groupme_callback.html", flow_valid=bool(_flow()))


@app.route("/groupme/oauth/complete", methods=["POST"])
@login_required
def groupme_oauth_complete():
    require_csrf()
    flow = _flow()
    if not flow or flow["token_ciphertext"]:
        return _setup_error("This GroupMe connection attempt expired. Please start again.", flow)
    echoed_state = request.form.get("state", "")
    # The official tutorial does not promise state echo. Reject a mismatched
    # echo; when absent, require explicit account/group confirmation below.
    if echoed_state and not secrets.compare_digest(echoed_state.encode(), session["groupme_oauth_flow"].encode()):
        return _setup_error("GroupMe connection could not be verified. Please start again.", flow)
    token = request.form.get("access_token", "")
    if not re.fullmatch(r"[A-Za-z0-9._~+/-]{8,2048}={0,2}", token):
        return _setup_error("GroupMe authorization was cancelled or did not return a valid token.", flow)
    try:
        account = _api("GET", "/users/me", token)
        if not isinstance(account, dict) or not isinstance(account.get("id"), str) or not account.get("name"):
            raise GroupMeError("GroupMe did not return an account. Please reconnect.")
        groups = []
        for page in range(1, 21):
            batch = _api("GET", "/groups", token, params={"page": page, "per_page": 100, "omit": "memberships"})
            if not isinstance(batch, list):
                raise GroupMeError("GroupMe did not return a group list. Please reconnect.")
            groups.extend({"id": g["id"], "name": g["name"][:200]} for g in batch
                          if isinstance(g, dict) and isinstance(g.get("id"), str)
                          and re.fullmatch(r"[0-9]{1,64}", g["id"])
                          and isinstance(g.get("name"), str) and g["name"])
            if len(batch) < 100:
                break
        else:
            raise GroupMeError("Too many groups to list. Contact your administrator for manual setup.")
        if not groups:
            raise GroupMeError("No GroupMe groups were found. Join your building’s chat and reconnect.")
    except GroupMeError as error:
        return _setup_error(str(error), flow)
    changed = db().execute(
        "UPDATE groupme_oauth_flows SET token_ciphertext=?,account_name=?,account_id=?,groups_json=? "
        "WHERE state_hash=? AND token_ciphertext IS NULL AND expires_at>?",
        (_cipher().encrypt(token.encode()).decode(), str(account["name"])[:200], account["id"][:64],
         json.dumps(groups), flow["state_hash"], time.time()),
    ).rowcount
    db().commit()
    if not changed:
        return _setup_error("This GroupMe connection attempt expired. Please start again.", flow)
    return redirect(url_for("groupme_choose_group"))


@app.route("/groupme/select", methods=["GET", "POST"])
@login_required
def groupme_choose_group():
    flow = _flow()
    if not flow or not flow["token_ciphertext"]:
        return _setup_error("This GroupMe connection attempt expired. Please start again.", flow)
    groups = json.loads(flow["groups_json"])
    if request.method == "GET":
        return render_template("groupme_select.html", flow=flow, groups=groups)
    require_csrf()
    if request.form.get("action") == "cancel":
        return _setup_error("GroupMe setup cancelled. No bot was added.", flow)
    selected = next((g for g in groups if g["id"] == request.form.get("group_id")), None)
    if not selected or request.form.get("confirm_account") != "1":
        abort(400)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    user = conn.execute("SELECT * FROM users WHERE id=?", (flow["user_id"],)).fetchone()
    existing = conn.execute("SELECT bot_hash FROM building_groupme_bots WHERE building_id=?", (flow["building_id"],)).fetchone()
    original = existing["bot_hash"] if existing else None
    if not _can_manage(user, flow["building_id"]) or original != flow["original_bot_hash"]:
        conn.rollback()
        return _setup_error("The building’s connection changed. Please reconnect.", flow)
    # Consume before the provider write, so double submits cannot create bots.
    consumed = conn.execute("DELETE FROM groupme_oauth_flows WHERE state_hash=? AND expires_at>?",
                            (flow["state_hash"], time.time())).rowcount
    conn.commit()
    session.pop("groupme_oauth_flow", None)
    if not consumed:
        return _setup_error("This GroupMe connection attempt expired. Please start again.")
    try:
        token = _cipher().decrypt(flow["token_ciphertext"].encode()).decode()
        created = _api("POST", "/bots", token, payload={"bot": {
            "name": "RA Deadlines", "group_id": selected["id"], "active": True,
        }})
        created = created.get("bot", created) if isinstance(created, dict) else {}
        bot_id = created.get("bot_id", "")
        if not isinstance(bot_id, str) or not BOT_ID_PATTERN.fullmatch(bot_id):
            raise GroupMeError("GroupMe did not return a Bot ID. Check your bots on GroupMe before retrying.")
    except (InvalidToken, UnicodeError):
        return _setup_error("Please reconnect your GroupMe account.")
    except GroupMeError as error:
        return _setup_error(str(error))
    conn.execute("BEGIN IMMEDIATE")
    user = conn.execute("SELECT * FROM users WHERE id=?", (flow["user_id"],)).fetchone()
    existing = conn.execute("SELECT bot_hash FROM building_groupme_bots WHERE building_id=?", (flow["building_id"],)).fetchone()
    if not _can_manage(user, flow["building_id"]) or (existing["bot_hash"] if existing else None) != original:
        conn.rollback()
        return _setup_error("Setup changed while GroupMe was adding the bot. It was not connected; remove it on GroupMe and reconnect.")
    try:
        conn.execute(
            "INSERT INTO building_groupme_bots(building_id,bot_ciphertext,bot_hash,enabled,configured_by,group_name) "
            "VALUES(?,?,?,1,?,?) ON CONFLICT(building_id) DO UPDATE SET "
            "bot_ciphertext=excluded.bot_ciphertext,bot_hash=excluded.bot_hash,enabled=1,"
            "configured_by=excluded.configured_by,group_name=excluded.group_name,last_error=NULL,last_sent_at=NULL",
            (flow["building_id"], _cipher().encrypt(bot_id.encode()).decode(), hashlib.sha256(bot_id.encode()).hexdigest(),
             user["id"], selected["name"]),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return _setup_error("This bot is already connected to another building. Contact your administrator.")
    audit("deadlines.groupme.connect", "building", flow["building_id"])
    conn.commit()
    flash("GroupMe connected. Deadline reminders are on for the selected group.", "success")
    return redirect(url_for("deadline_calendar", groupme_building=flow["building_id"]))
