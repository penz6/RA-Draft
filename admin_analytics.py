"""Read-only reporting over current scheduling records.

Admin, Admin Lite, and Prostaff users see the full analytics dashboard.
HRA users see a building-scoped subset.
"""

from collections import Counter

from flask import abort, flash, redirect, render_template, request, url_for

from core import app, capacities_for, current_user, db, login_required, roles
from runtime_policy import school_today


@app.route('/admin/analytics')
@login_required
def admin_analytics():
    me = current_user()
    if me['role'] != 'ADMIN' and not me['admin_lite'] and not me['is_prostaff']:
        abort(403)
    conn = db()
    conn.execute('BEGIN')
    buildings = conn.execute('SELECT * FROM buildings ORDER BY name').fetchall()
    raw_building = request.args.get('building_id', '')
    building_id = None
    if raw_building:
        try:
            building_id = int(raw_building)
        except ValueError:
            abort(400)
        if building_id not in {b['id'] for b in buildings}:
            abort(404)
    sessions = conn.execute(
        'SELECT s.*, b.name building_name FROM draft_sessions s '
        'JOIN buildings b ON b.id=s.building_id '
        'WHERE (? IS NULL OR s.building_id=?) ORDER BY s.start_date DESC, s.id DESC',
        (building_id, building_id),
    ).fetchall()
    assignments = conn.execute(
        'SELECT a.* FROM assignments a JOIN draft_sessions s ON s.id=a.session_id '
        'WHERE (? IS NULL OR s.building_id=?)', (building_id, building_id),
    ).fetchall()
    counts = Counter((a['session_id'], a['duty_date']) for a in assignments)
    today = school_today().isoformat()
    reports = []
    for s in sessions:
        capacities = capacities_for(s)
        required = sum(capacities.values())
        filled = sum(min(counts[s['id'], day], capacity) for day, capacity in capacities.items())
        upcoming_open = sum(max(0, capacity - counts[s['id'], day])
                            for day, capacity in capacities.items() if day >= today)
        reports.append(dict(s, required=required, filled=filled, upcoming_open=upcoming_open,
                            coverage=round(100 * filled / required) if required else None))
    workload = conn.execute(
        'SELECT u.id,u.name,u.disabled,COUNT(a.id) shifts FROM users u '
        'LEFT JOIN assignments a ON a.user_id=u.id '
        'AND a.session_id IN (SELECT id FROM draft_sessions WHERE (? IS NULL OR building_id=?)) '
        'WHERE EXISTS (SELECT 1 FROM session_order o JOIN draft_sessions s ON s.id=o.session_id '
        'WHERE o.user_id=u.id AND (? IS NULL OR s.building_id=?)) OR a.id IS NOT NULL '
        'GROUP BY u.id ORDER BY shifts DESC,u.name,u.id',
        (building_id,) * 4,
    ).fetchall()
    swap_counts = {row['status']: row['total'] for row in conn.execute(
        "SELECT r.status, COUNT(DISTINCT CASE WHEN r.batch_id IS NULL THEN 'row:' || r.id "
        "ELSE 'batch:' || r.batch_id END) total FROM duty_swap_requests r "
        'JOIN draft_sessions s ON s.id=r.session_id '
        'WHERE (? IS NULL OR s.building_id=?) GROUP BY r.status',
        (building_id, building_id),
    )}
    required = sum(r['required'] for r in reports)
    filled = sum(r['filled'] for r in reports)
    metrics = dict(assignments=len(assignments),
                   open_sessions=sum(s['status'] == 'OPEN' for s in sessions),
                   coverage=round(100 * filled / required) if required else None,
                   required=required, filled=filled,
                   upcoming_open=sum(r['upcoming_open'] for r in reports))

    # ── Logins per week ──
    logins_per_week = conn.execute(
        "SELECT strftime('%Y-W%W', created_at) AS week, COUNT(*) AS logins "
        "FROM audit_log WHERE action='auth.login' "
        "GROUP BY week ORDER BY week DESC LIMIT 12"
    ).fetchall()

    conn.commit()
    return render_template('admin_analytics.html', me=me, buildings=buildings,
                           building_id=building_id, reports=reports, metrics=metrics,
                           workload=workload, max_shifts=max((r['shifts'] for r in workload), default=0),
                           swap_counts=swap_counts, today=today,
                           logins_per_week=logins_per_week)


@app.route('/hra/analytics')
@roles('HRA')
def hra_analytics():
    """Building-scoped analytics dashboard for Head RAs."""
    me = current_user()
    building_id = me['building_id']
    if not building_id:
        if me['role'] == 'ADMIN':
            return redirect(url_for('admin_analytics'))
        flash('Please ask an administrator to assign your building before viewing analytics.', 'error')
        return redirect(url_for('dashboard'))

    conn = db()
    conn.execute('BEGIN')
    today = school_today().isoformat()

    building = conn.execute(
        'SELECT * FROM buildings WHERE id=?', (building_id,)
    ).fetchone()

    # ── Assigned shifts by status (per session, building-scoped) ──
    sessions = conn.execute(
        'SELECT s.*, b.name building_name FROM draft_sessions s '
        'JOIN buildings b ON b.id=s.building_id '
        'WHERE s.building_id=? ORDER BY s.start_date DESC, s.id DESC',
        (building_id,),
    ).fetchall()
    assignments = conn.execute(
        'SELECT a.* FROM assignments a JOIN draft_sessions s ON s.id=a.session_id '
        'WHERE s.building_id=?', (building_id,),
    ).fetchall()
    counts = Counter((a['session_id'], a['duty_date']) for a in assignments)
    reports = []
    for s in sessions:
        capacities = capacities_for(s)
        required = sum(capacities.values())
        filled = sum(min(counts[s['id'], day], capacity) for day, capacity in capacities.items())
        upcoming_open = sum(max(0, capacity - counts[s['id'], day])
                            for day, capacity in capacities.items() if day >= today)
        reports.append(dict(s, required=required, filled=filled, upcoming_open=upcoming_open,
                            coverage=round(100 * filled / required) if required else None))

    # ── Duty swaps for this building ──
    swap_counts = {row['status']: row['total'] for row in conn.execute(
        "SELECT r.status, COUNT(DISTINCT CASE WHEN r.batch_id IS NULL THEN 'row:' || r.id "
        "ELSE 'batch:' || r.batch_id END) total FROM duty_swap_requests r "
        'JOIN draft_sessions s ON s.id=r.session_id '
        'WHERE s.building_id=? GROUP BY r.status',
        (building_id,),
    )}

    # ── Assigned shifts per person (building-scoped) ──
    workload = conn.execute(
        'SELECT u.id,u.name,u.disabled,COUNT(a.id) shifts FROM users u '
        'LEFT JOIN assignments a ON a.user_id=u.id '
        'AND a.session_id IN (SELECT id FROM draft_sessions WHERE building_id=?) '
        'WHERE EXISTS (SELECT 1 FROM session_order o JOIN draft_sessions s ON s.id=o.session_id '
        'WHERE o.user_id=u.id AND s.building_id=?) OR a.id IS NOT NULL '
        'GROUP BY u.id ORDER BY shifts DESC,u.name,u.id',
        (building_id, building_id),
    ).fetchall()

    # ── Aggregate metrics ──
    required = sum(r['required'] for r in reports)
    filled = sum(r['filled'] for r in reports)
    metrics = dict(
        assignments=len(assignments),
        open_sessions=sum(s['status'] == 'OPEN' for s in sessions),
        coverage=round(100 * filled / required) if required else None,
        required=required, filled=filled,
        upcoming_open=sum(r['upcoming_open'] for r in reports),
    )

    conn.commit()
    return render_template('hra_analytics.html', me=me, building=building,
                           reports=reports, metrics=metrics,
                           workload=workload, max_shifts=max((r['shifts'] for r in workload), default=0),
                           swap_counts=swap_counts, today=today)
