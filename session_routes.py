"""Unified routes and controller handlers for draft session operations."""

from collections import defaultdict
from datetime import date
import sqlite3

from flask import abort, flash, redirect, render_template, request, url_for

from core import (
    DATE_KIND_AUTO,
    DATE_KIND_FORM_CHOICES,
    DATE_KIND_LABELS,
    DATE_KIND_NO_DUTY,
    DATE_ORDER_WEEKDAYS_FIRST,
    DATE_ORDER_WEEKENDS_FIRST,
    advance_turn,
    app,
    assignment_counts,
    audit,
    calendar_dates,
    calendar_months,
    can_manage,
    can_view_session,
    capacities_for,
    capacity_overrides,
    clean_single_line,
    current_user,
    dates_for,
    date_kind_overrides,
    date_kinds_for,
    db,
    effective_capacity,
    effective_date_kind,
    filled_slots,
    is_participant,
    login_required,
    next_picker,
    normalize_date_order,
    ordered_people,
    participant_count,
    require_csrf,
    roles,
    selectable_dates,
    selection_phase_label,
    session_complete,
    session_row,
    session_swap_requests,
    total_slots,
)
from live_updates import session_state_version
from session_action_response import session_action_response


def _locked_manager_session(conn, session_id):
    """Verify management authorization and fetch session row inside an immediate transaction."""
    manager = current_user()
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)
    if not can_manage(manager, row):
        conn.rollback()
        abort(403)
    return manager, row


def _session_template_context(session_id, user):
    """Assemble all session state, calendar, participant, and permission data for views."""
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)

    people = ordered_people(session_id)
    picks = db().execute(
        "SELECT a.*,u.name,u.role,o.position FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "JOIN session_order o ON o.session_id=a.session_id AND o.user_id=a.user_id "
        "WHERE a.session_id=? ORDER BY a.duty_date,o.position,a.id",
        (session_id,),
    ).fetchall()

    assignments_by_date = defaultdict(list)
    assignment_user_ids = defaultdict(list)
    for assignment in picks:
        assignments_by_date[assignment["duty_date"]].append(assignment)
        assignment_user_ids[assignment["duty_date"]].append(str(assignment["user_id"]))

    counts = assignment_counts(session_id)
    dates = calendar_dates(row)
    ordered_dates = dates_for(row)
    capacities = capacities_for(row)
    overrides = capacity_overrides(session_id)
    kinds = date_kinds_for(row)
    kind_overrides = date_kind_overrides(session_id)
    current_picker = next_picker(session_id)
    self_selectable = (
        set(selectable_dates(row, user["id"]))
        if row["status"] == "OPEN"
        and not row["picking_paused"]
        and current_picker
        and current_picker["id"] == user["id"]
        else set()
    )
    complete = all(counts.get(duty_date, 0) >= capacity for duty_date, capacity in capacities.items())
    filled = len(picks)
    slots = sum(capacities.values())
    people_count = len(people)
    manager_allowed = can_manage(user, row)
    phase_label = selection_phase_label(row)
    live_version = session_state_version(row, user)
    swaps = session_swap_requests(session_id)
    pending_swaps = [s for s in swaps if s["status"] == "PENDING"]
    my_swaps = [s for s in swaps if s["requester_user_id"] == user["id"] or s["target_user_id"] == user["id"]]

    return {
        "me": user,
        "draft": row,
        "people": people,
        "picks": picks,
        "counts": counts,
        "dates": dates,
        "ordered_dates": ordered_dates,
        "months": calendar_months(row),
        "capacities": capacities,
        "capacity_overrides": overrides,
        "date_kinds": kinds,
        "date_kind_overrides": kind_overrides,
        "date_kind_labels": DATE_KIND_LABELS,
        "assignments_by_date": dict(assignments_by_date),
        "assignment_user_ids": {
            duty_date: ",".join(user_ids)
            for duty_date, user_ids in assignment_user_ids.items()
        },
        "selectable_dates": self_selectable,
        "next": current_picker,
        "can_manage": manager_allowed,
        "participant_count": people_count,
        "assigned_count": filled,
        "total_slot_count": slots,
        "open_slot_count": max(slots - filled, 0),
        "available_date_count": sum(
            1
            for duty_date in dates
            if capacities[duty_date] > 0
            and counts.get(duty_date, 0) < capacities[duty_date]
        ),
        "schedule_complete": complete,
        "selection_phase": phase_label,
        "live_version": live_version,
        "swaps": swaps,
        "pending_swaps": pending_swaps,
        "my_swaps": my_swaps,
    }


def _begin_session_snapshot(session_id):
    """Open an isolated read transaction and generate the session template context."""
    conn = db()
    conn.execute("BEGIN")
    user = current_user()
    if not user:
        conn.rollback()
        return conn, None
    row = session_row(session_id)
    if not row or not can_view_session(user, row):
        conn.rollback()
        abort(403 if row else 404)
    return conn, _session_template_context(session_id, user)


@app.route("/sessions/<int:session_id>")
@login_required
def view_session(session_id):
    """Render the full interactive draft session page for participants and managers."""
    conn, context = _begin_session_snapshot(session_id)
    if context is None:
        return redirect(url_for("login"))
    try:
        response = render_template("session_v2.html", **context)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return response


@app.route("/sessions/<int:session_id>/live-fragments")
@login_required
def session_live_fragments(session_id):
    """Return the current dynamic session HTML partials without full page reloads."""
    conn, context = _begin_session_snapshot(session_id)
    if context is None:
        return redirect(url_for("login"))
    try:
        fragments = {
            "heading": render_template("session_heading_live_v2.html", **context),
            "summary": render_template("session_summary_live_v2.html", **context),
            "status": render_template("session_status_live_v2.html", **context),
            "turn_order": render_template("session_order_v2.html", **context),
            "calendar": render_template("session_dates_v2.html", **context),
            "assignments": render_template("session_assignments_v2.html", **context),
        }
        version = context["live_version"]
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    conn.commit()
    return {"version": version, "fragments": fragments}


@app.route("/sessions", methods=["POST"])
@roles("HRA", "ADMIN")
def create_session():
    """Create a new draft session with configured date range, capacity, and participants."""
    require_csrf()
    initial_user = current_user()
    if not initial_user:
        return redirect(url_for("login"))

    try:
        building_id = int(request.form.get("building_id") or initial_user["building_id"] or 0)
    except (TypeError, ValueError):
        building_id = 0
    if not building_id:
        flash("A building must be assigned or selected before creating a session.", "error")
        return redirect(url_for("dashboard"))

    try:
        name = clean_single_line(request.form.get("name"), max_length=120)
    except ValueError:
        flash("Session name must be 1 to 120 characters with no control characters.", "error")
        return redirect(url_for("dashboard"))

    try:
        start_date = date.fromisoformat(request.form["start_date"])
        end_date = date.fromisoformat(request.form["end_date"])
    except (KeyError, ValueError):
        abort(400)
    if end_date < start_date or (end_date - start_date).days > 400:
        flash("Choose a valid date range of 400 days or less.", "error")
        return redirect(url_for("dashboard"))

    try:
        requested_capacity = int(request.form.get("capacity", 2))
    except (TypeError, ValueError):
        requested_capacity = 2

    raw_participant_ids = request.form.getlist("participant_ids")
    participant_order = []
    seen = set()
    for raw_id in raw_participant_ids:
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if pid not in seen:
            seen.add(pid)
            order_val = request.form.get(f"order_{pid}", len(participant_order) + 1)
            try:
                numeric_order = int(order_val)
            except (TypeError, ValueError):
                numeric_order = len(participant_order) + 1
            participant_order.append((numeric_order, pid))

    participant_order.sort(key=lambda item: (item[0], item[1]))
    ordered_participant_ids = [pid for _, pid in participant_order]

    if not ordered_participant_ids:
        flash("At least one active participant must be selected.", "error")
        return redirect(url_for("dashboard"))

    try:
        date_order = normalize_date_order(request.form.get("date_order"))
    except ValueError:
        flash("Invalid date selection mode selected.", "error")
        return redirect(url_for("dashboard"))

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    building = conn.execute(
        "SELECT id FROM buildings WHERE id=?",
        (building_id,),
    ).fetchone()
    if not building:
        conn.rollback()
        flash("Selected building does not exist.", "error")
        return redirect(url_for("dashboard"))

    user = current_user()
    if not user:
        conn.rollback()
        return redirect(url_for("login"))
    if user["role"] == "HRA" and user["building_id"] != building_id:
        conn.rollback()
        abort(403)

    capacity = max(1, min(requested_capacity, len(ordered_participant_ids), 50))
    cur = conn.execute(
        "INSERT INTO draft_sessions("
        "name,building_id,start_date,end_date,capacity,date_order,current_position,picking_paused,created_by"
        ") VALUES(?,?,?,?,?,?,1,0,?)",
        (
            name,
            building_id,
            start_date.isoformat(),
            end_date.isoformat(),
            capacity,
            date_order,
            user["id"],
        ),
    )
    session_id = cur.lastrowid

    for pos, pid in enumerate(ordered_participant_ids, start=1):
        conn.execute(
            "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,?)",
            (session_id, pid, pos),
        )

    audit(
        "draft.session.create",
        "session",
        session_id,
        {
            "name": name,
            "building_id": building_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "capacity": capacity,
            "date_order": date_order,
            "participants": ordered_participant_ids,
        },
    )
    conn.commit()
    flash(f"Session '{name}' created and opened.", "success")
    return redirect(url_for("view_session", session_id=session_id))


@app.route("/sessions/<int:session_id>/status", methods=["POST"])
@roles("HRA", "ADMIN")
def update_session_status(session_id):
    """Open or close a draft session (HRA/Admin only)."""
    require_csrf()
    next_status = request.form.get("status", "").strip().upper()
    if next_status not in ("OPEN", "CLOSED"):
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)
    if row["status"] == next_status:
        conn.rollback()
        return session_action_response(session_id, f"Session is already {next_status.lower()}.")

    conn.execute("UPDATE draft_sessions SET status=? WHERE id=?", (next_status, session_id))
    audit(
        "draft.session.status",
        "session",
        session_id,
        {"old_status": row["status"], "new_status": next_status},
    )
    conn.commit()
    return session_action_response(session_id, f"Session status updated to {next_status}.")


@app.route("/sessions/<int:session_id>/picking", methods=["POST"])
@roles("HRA", "ADMIN")
def session_picking(session_id):
    """Pause or resume active round robin draft picking in a session."""
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)

    raw_paused = request.form.get("paused", "").strip().lower()
    next_paused = 1 if raw_paused in ("1", "true", "yes", "on") else 0

    if row["picking_paused"] == next_paused:
        conn.rollback()
        label = "paused" if next_paused else "active"
        return session_action_response(session_id, f"Picking is already {label}.")

    conn.execute("UPDATE draft_sessions SET picking_paused=? WHERE id=?", (next_paused, session_id))
    audit(
        "draft.session.picking_paused",
        "session",
        session_id,
        {"old_paused": row["picking_paused"], "new_paused": next_paused},
    )
    conn.commit()
    label = "paused" if next_paused else "resumed"
    return session_action_response(session_id, f"Duty picking has been {label}.")


@app.route("/sessions/<int:session_id>/choose", methods=["POST"])
@login_required
def choose_date(session_id):
    """Submit a self-service duty date pick when it is the participant's turn."""
    require_csrf()
    user = current_user()
    duty_date = request.form.get("duty_date", "").strip()

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    row = session_row(session_id)
    if not row or not can_view_session(user, row):
        conn.rollback()
        abort(403 if row else 404)

    if row["status"] != "OPEN":
        conn.rollback()
        return session_action_response(
            session_id,
            "This session is closed.",
            category="error",
            status=409,
        )
    if row["picking_paused"]:
        conn.rollback()
        return session_action_response(
            session_id,
            "Picking is currently paused by the HRA.",
            category="error",
            status=409,
        )

    current_picker = next_picker(session_id)
    if not current_picker or current_picker["id"] != user["id"]:
        conn.rollback()
        return session_action_response(
            session_id,
            "It is not your turn to pick.",
            category="error",
            status=409,
        )

    if duty_date not in selectable_dates(row, user["id"]):
        conn.rollback()
        return session_action_response(
            session_id,
            "That date is not available in the current selection phase.",
            category="error",
            status=409,
        )

    conn.execute(
        "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
        (session_id, user["id"], duty_date, user["id"]),
    )
    advance_turn(session_id, user["id"])
    audit(
        "draft.pick",
        "assignment",
        session_id,
        {"duty_date": duty_date, "participant_user_id": user["id"]},
    )
    conn.commit()
    return session_action_response(session_id, f"You picked {duty_date}.")


@app.route("/sessions/<int:session_id>/pass", methods=["POST"])
@login_required
def pass_turn(session_id):
    """Defer the active participant's turn to the end of the round."""
    require_csrf()
    try:
        target_user_id = int(request.form.get("user_id", ""))
    except (TypeError, ValueError):
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    user = current_user()
    row = session_row(session_id)
    if not row or not can_view_session(user, row):
        conn.rollback()
        abort(403 if row else 404)

    if user["id"] != target_user_id and not can_manage(user, row):
        conn.rollback()
        abort(403)

    if row["status"] != "OPEN" or row["picking_paused"]:
        conn.rollback()
        return session_action_response(
            session_id,
            "Cannot pass turn when session is closed or paused.",
            category="error",
            status=409,
        )

    current_picker = next_picker(session_id)
    if not current_picker or current_picker["id"] != target_user_id:
        conn.rollback()
        return session_action_response(
            session_id,
            "It is not that participant's turn to pick.",
            category="error",
            status=409,
        )

    advance_turn(session_id, target_user_id)
    audit(
        "draft.pass",
        "session",
        session_id,
        {"deferred_user_id": target_user_id, "by_user_id": user["id"]},
    )
    conn.commit()
    return session_action_response(session_id, f"Turn passed for {current_picker['name']}.")


@app.route("/sessions/<int:session_id>/assignments", methods=["POST"])
@roles("HRA", "ADMIN")
def create_assignment(session_id):
    """Manually assign a participant to a duty date (HRA/Admin only)."""
    require_csrf()
    try:
        user_id = int(request.form.get("user_id", ""))
    except (TypeError, ValueError):
        abort(400)

    duty_date = request.form.get("duty_date", "").strip()

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)

    participant = conn.execute(
        "SELECT u.id,u.name,u.disabled FROM users u "
        "JOIN session_order o ON o.user_id=u.id "
        "WHERE o.session_id=? AND u.id=?",
        (session_id, user_id),
    ).fetchone()
    if not participant:
        conn.rollback()
        return session_action_response(
            session_id,
            "User is not a participant in this session.",
            category="error",
            status=400,
        )
    if participant["disabled"]:
        conn.rollback()
        return session_action_response(
            session_id,
            f"{participant['name']} is disabled.",
            category="error",
            status=409,
        )

    if duty_date not in calendar_dates(row):
        conn.rollback()
        return session_action_response(
            session_id,
            "Selected date is outside the session range.",
            category="error",
            status=400,
        )

    kinds = date_kinds_for(row)
    if kinds.get(duty_date) == DATE_KIND_NO_DUTY:
        conn.rollback()
        return session_action_response(
            session_id,
            "No duty is needed on that date.",
            category="error",
            status=409,
        )

    try:
        conn.execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
            (session_id, user_id, duty_date, manager["id"]),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return session_action_response(
            session_id,
            f"{participant['name']} is already assigned to {duty_date}.",
            category="error",
            status=409,
        )

    current_picker = next_picker(session_id)
    if current_picker and current_picker["id"] == user_id:
        advance_turn(session_id, user_id)

    audit(
        "draft.assignment.manual",
        "assignment",
        session_id,
        {"duty_date": duty_date, "participant_user_id": user_id},
    )
    conn.commit()
    return session_action_response(session_id, f"Assigned {participant['name']} to {duty_date}.")


@app.route("/sessions/<int:session_id>/assignments/<int:assignment_id>/delete", methods=["POST"])
@roles("HRA", "ADMIN")
def delete_assignment(session_id, assignment_id):
    """Remove an assignment record from the session (HRA/Admin only)."""
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)

    assignment = conn.execute(
        "SELECT a.*, u.name FROM assignments a "
        "JOIN users u ON u.id=a.user_id "
        "WHERE a.id=? AND a.session_id=?",
        (assignment_id, session_id),
    ).fetchone()
    if not assignment:
        conn.rollback()
        return session_action_response(
            session_id,
            "Assignment not found.",
            category="error",
            status=404,
        )

    conn.execute("DELETE FROM assignments WHERE id=? AND session_id=?", (assignment_id, session_id))
    audit(
        "draft.assignment.delete",
        "assignment",
        assignment_id,
        {
            "session_id": session_id,
            "duty_date": assignment["duty_date"],
            "user_id": assignment["user_id"],
        },
    )
    conn.commit()
    return session_action_response(
        session_id,
        f"Removed assignment for {assignment['name']} on {assignment['duty_date']}.",
    )


@app.route("/sessions/<int:session_id>/delete", methods=["POST"])
@roles("ADMIN")
def delete_session(session_id):
    """Permanently delete a draft session and all its associated records (Admin only)."""
    require_csrf()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    row = session_row(session_id)
    if not row:
        conn.rollback()
        abort(404)

    conn.execute("DELETE FROM draft_sessions WHERE id=?", (session_id,))
    audit(
        "draft.session.delete",
        "session",
        session_id,
        {
            "name": row["name"],
            "building_id": row["building_id"],
            "building_name": row["building_name"],
        },
    )
    conn.commit()
    flash(f"Session '{row['name']}' was permanently deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/sessions/<int:session_id>/swaps/request", methods=["POST"])
@login_required
def request_duty_swap(session_id):
    """Submit a peer-to-peer duty shift swap request for HRA approval."""
    require_csrf()
    user = current_user()
    row = session_row(session_id)
    if not row:
        abort(404)
    if not can_view_session(user, row):
        abort(403)

    try:
        my_assignment_id = int(request.form.get("my_assignment_id", ""))
        target_assignment_id = int(request.form.get("target_assignment_id", ""))
    except (TypeError, ValueError):
        abort(400)

    if my_assignment_id == target_assignment_id:
        return session_action_response(
            session_id,
            "You cannot swap an assignment with itself.",
            category="error",
            status=400,
        )

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    my_assignment = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
        (my_assignment_id, session_id, user["id"]),
    ).fetchone()
    if not my_assignment:
        conn.rollback()
        return session_action_response(
            session_id,
            "Your selected assignment is not valid.",
            category="error",
            status=400,
        )

    target_assignment = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=?",
        (target_assignment_id, session_id),
    ).fetchone()
    if not target_assignment or target_assignment["user_id"] == user["id"]:
        conn.rollback()
        return session_action_response(
            session_id,
            "Target assignment must belong to a different participant in this session.",
            category="error",
            status=400,
        )

    target_user_id = target_assignment["user_id"]

    existing_pending = conn.execute(
        "SELECT 1 FROM duty_swap_requests WHERE session_id=? AND status='PENDING' "
        "AND (requester_assignment_id IN (?, ?) OR target_assignment_id IN (?, ?))",
        (session_id, my_assignment_id, target_assignment_id, my_assignment_id, target_assignment_id),
    ).fetchone()
    if existing_pending:
        conn.rollback()
        return session_action_response(
            session_id,
            "A pending swap request already exists for one of these assignments.",
            category="error",
            status=409,
        )

    cur = conn.execute(
        "INSERT INTO duty_swap_requests("
        "session_id, requester_user_id, requester_assignment_id, target_user_id, target_assignment_id, status"
        ") VALUES (?, ?, ?, ?, ?, 'PENDING')",
        (session_id, user["id"], my_assignment_id, target_user_id, target_assignment_id),
    )
    audit(
        "swap.request",
        "swap_request",
        cur.lastrowid,
        {
            "session_id": session_id,
            "requester_user_id": user["id"],
            "requester_assignment_id": my_assignment_id,
            "target_user_id": target_user_id,
            "target_assignment_id": target_assignment_id,
        },
    )
    conn.commit()
    return session_action_response(
        session_id,
        "Swap request submitted. An HRA or Admin can now review and approve it.",
    )


@app.route("/sessions/<int:session_id>/swaps/<int:swap_id>/review", methods=["POST"])
@roles("HRA", "ADMIN")
def review_duty_swap(session_id, swap_id):
    """Approve or reject a pending duty swap request (HRA/Admin only)."""
    require_csrf()
    action = request.form.get("action", "").strip().upper()
    if action not in ("APPROVE", "REJECT"):
        abort(400)

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    manager, row = _locked_manager_session(conn, session_id)

    swap = conn.execute(
        "SELECT * FROM duty_swap_requests WHERE id=? AND session_id=?",
        (swap_id, session_id),
    ).fetchone()
    if not swap or swap["status"] != "PENDING":
        conn.rollback()
        return session_action_response(
            session_id,
            "That swap request is no longer pending.",
            category="error",
            status=409,
        )

    if action == "REJECT":
        conn.execute(
            "UPDATE duty_swap_requests SET status='REJECTED', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (manager["id"], swap_id),
        )
        audit(
            "swap.reject",
            "swap_request",
            swap_id,
            {"session_id": session_id, "reviewed_by": manager["id"]},
        )
        conn.commit()
        return session_action_response(session_id, "Duty swap request rejected.")

    req_assign = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
        (swap["requester_assignment_id"], session_id, swap["requester_user_id"]),
    ).fetchone()
    target_assign = conn.execute(
        "SELECT * FROM assignments WHERE id=? AND session_id=? AND user_id=?",
        (swap["target_assignment_id"], session_id, swap["target_user_id"]),
    ).fetchone()

    if not req_assign or not target_assign:
        conn.rollback()
        return session_action_response(
            session_id,
            "One or both assignments have changed; swap cannot be approved.",
            category="error",
            status=409,
        )

    conn.execute(
        "UPDATE assignments SET user_id=? WHERE id=?",
        (swap["target_user_id"], swap["requester_assignment_id"]),
    )
    conn.execute(
        "UPDATE assignments SET user_id=? WHERE id=?",
        (swap["requester_user_id"], swap["target_assignment_id"]),
    )
    conn.execute(
        "UPDATE duty_swap_requests SET status='APPROVED', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (manager["id"], swap_id),
    )
    audit(
        "swap.approve",
        "swap_request",
        swap_id,
        {
            "session_id": session_id,
            "requester_user_id": swap["requester_user_id"],
            "requester_assignment_id": swap["requester_assignment_id"],
            "target_user_id": swap["target_user_id"],
            "target_assignment_id": swap["target_assignment_id"],
            "reviewed_by": manager["id"],
        },
    )
    conn.commit()
    return session_action_response(session_id, "Duty swap approved and schedule updated.")


@app.route("/sessions/<int:session_id>/swaps/<int:swap_id>/cancel", methods=["POST"])
@login_required
def cancel_duty_swap(session_id, swap_id):
    """Cancel a pending swap request by the user who initiated it."""
    require_csrf()
    user = current_user()
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    row = session_row(session_id)
    if not row or not can_view_session(user, row):
        conn.rollback()
        abort(403 if row else 404)

    swap = conn.execute(
        "SELECT * FROM duty_swap_requests WHERE id=? AND session_id=? AND requester_user_id=?",
        (swap_id, session_id, user["id"]),
    ).fetchone()
    if not swap or swap["status"] != "PENDING":
        conn.rollback()
        return session_action_response(
            session_id,
            "That swap request is no longer pending or cannot be cancelled.",
            category="error",
            status=409,
        )

    conn.execute(
        "UPDATE duty_swap_requests SET status='CANCELLED', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (swap_id,),
    )
    audit("swap.cancel", "swap_request", swap_id, {"session_id": session_id, "user_id": user["id"]})
    conn.commit()
    return session_action_response(session_id, "Swap request cancelled.")
