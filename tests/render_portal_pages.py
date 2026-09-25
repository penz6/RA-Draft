"""Render real application pages with isolated, fictional data for browser checks."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Never use a production database or credentials when generating previews.
os.environ.update(
    DATABASE_PATH=str(Path(tempfile.mkdtemp(prefix='portal-preview-')) / 'preview.db'),
    SECRET_KEY='preview-only-key-0123456789abcdef0123456789abcdef',
    PUBLIC_HOST='ci.local', GOOGLE_CLIENT_ID='preview.apps.googleusercontent.com',
    GOOGLE_CLIENT_SECRET='preview-only', ADMIN_EMAILS='admin@rwu.edu', PROXY_HOPS='0',
)
from test_admin_management import AdminManagementTestCase
import main
from core import app, db
from flask import url_for


def render_pages(destination):
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'static', destination / 'static', dirs_exist_ok=True)
    fixture = AdminManagementTestCase()
    fixture.setUp()
    hall = fixture.add_building('Maple Hall')
    fixture.add_building('Willow Hall')
    admin = fixture.add_admin()
    hra = fixture.add_user(sub='hra-preview', email='taylor@rwu.edu', name='Taylor Morgan', role='HRA', building_id=hall)
    ra = fixture.add_user(sub='ra-preview', email='alex@g.rwu.edu', name='Alex Rivera', building_id=hall)
    partner = fixture.add_user(sub='partner-preview', email='jordan@g.rwu.edu', name='Jordan Ellis', building_id=hall)
    prostaff = fixture.add_user(sub='prostaff-preview', email='coordinator@rwu.edu', name='Morgan Coordinator', building_id=hall)
    new_ra = fixture.add_user(sub='new-preview', email='sam@g.rwu.edu', name='Sam Lee')
    inactive = fixture.add_user(sub='inactive-preview', email='casey@g.rwu.edu', name='Casey Park', building_id=hall)
    with app.app_context():
        conn = db()
        conn.execute('UPDATE users SET building_id=? WHERE id=?', (hall, admin))
        conn.execute('UPDATE users SET is_prostaff=1,password_must_change=0 WHERE id=?', (prostaff,))
        conn.execute('UPDATE users SET disabled=1 WHERE id=?', (inactive,))
        conn.execute("UPDATE buildings SET accent_color='#ffffff',staff_meeting_at='2026-09-01T19:00',staff_meeting_location='Maple Lounge',staff_meeting_repeat_weeks=2,staff_dinner_at='2026-09-02T18:00',staff_dinner_location='Commons',staff_dinner_repeat_weeks=1 WHERE id=?", (hall,))
        opened = conn.execute("INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status,capacity,date_order) VALUES(?,?,?,?,?,'OPEN',2,'CHRONOLOGICAL')", ('September duty', hall, '2026-09-01', '2026-09-14', admin)).lastrowid
        closed = conn.execute("INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status,capacity) VALUES(?,?,?,?,?,'CLOSED',2)", ('October duty', hall, '2026-10-01', '2026-10-07', admin)).lastrowid
        for session_id in (opened, closed):
            for position, user in enumerate((ra, partner, hra, admin), 1):
                conn.execute('INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,?)', (session_id, user, position))
        assignments = {}
        for user, day in ((admin,'2026-10-01'), (ra,'2026-10-01'), (admin,'2026-10-02'), (partner,'2026-10-02'), (hra,'2026-10-03')):
            assignments[(user, day)] = conn.execute('INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)', (closed, user, day, admin)).lastrowid
        conn.execute(
            "INSERT INTO duty_swap_requests(session_id,requester_user_id,requester_assignment_id,"
            "target_user_id,target_assignment_id,status,batch_id) VALUES(?,?,?,?,?,'PENDING','preview-swap')",
            (closed, ra, assignments[(ra, '2026-10-01')], partner, assignments[(partner, '2026-10-02')]),
        )
        conn.execute("INSERT INTO session_date_overrides(session_id,duty_date,date_kind,updated_by) VALUES(?,?,'NO_DUTY',?)", (opened,'2026-09-10',admin))
        conn.commit()
    def save(name, endpoint, user=None, **values):
        with fixture.client.session_transaction() as session:
            session.clear()
        if user is not None:
            fixture.login_as(user)
        with app.test_request_context(base_url='https://ci.local'):
            path = url_for(endpoint, **values)
        response = fixture.request('get', path)
        if response.status_code != 200:
            raise AssertionError(f'{name}: {response.status_code} at {path}')
        text = response.get_data(as_text=True)
        if 'portal_navy.css' not in text:
            raise AssertionError(f'{name}: missing shared palette')
        (destination / f'{name}.html').write_text(text, encoding='utf-8')
    # /login initiates Google OAuth; / is the actual public sign-in page.
    save('login', 'index')
    save('onboarding', 'onboarding', new_ra)
    save('dashboard', 'dashboard', admin)
    save('admin', 'admin', admin)
    save('admin-analytics', 'admin_analytics', admin)
    save('hra-analytics', 'hra_analytics', hra)
    save('schedule', 'upcoming_duty_shifts', admin)
    save('session', 'view_session', admin, session_id=opened)
    save('ra-session', 'view_session', ra, session_id=opened)
    save('closed-session', 'view_session', admin, session_id=closed)
    save('swap-home', 'swap_home', admin)
    save('swaps', 'building_swap_page', ra, building_id=hall)
    save('prostaff-swaps', 'prostaff_swaps', prostaff)
    print('Rendered 13 application pages with sample data.')


if __name__ == '__main__':
    render_pages(Path(sys.argv[1]))
