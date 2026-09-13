"""Analytics authorization, scope, and capacity semantics."""
import unittest

from test_admin_management import AdminManagementTestCase
from core import app, db


class AdminAnalyticsTests(unittest.TestCase):
    setUp = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    add_admin = AdminManagementTestCase.add_admin
    login_as = AdminManagementTestCase.login_as

    def test_access_and_empty_state(self):
        self.assertEqual(self.request('get', '/admin/analytics').status_code, 302)
        for role in ('RA', 'HRA'):
            uid = self.add_user(sub=role, email=f'{role}@rwu.edu', name=role, role=role)
            self.login_as(uid)
            self.assertEqual(self.request('get', '/admin/analytics').status_code, 403)
            self.assertNotIn(b'href="/admin/analytics"', self.request('get', '/').data)
        uid = self.add_admin()
        self.login_as(uid)
        response = self.request('get', '/admin/analytics')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No sessions to report yet', response.data)
        self.assertNotIn(b'None%', response.data)
        self.assertEqual(self.request('get', '/admin/analytics?building_id=bad').status_code, 400)
        self.assertEqual(self.request('get', '/admin/analytics?building_id=999999').status_code, 404)
        with app.app_context():
            db().execute('UPDATE users SET disabled=1 WHERE id=?', (uid,))
            db().commit()
        self.assertEqual(self.request('get', '/admin/analytics').status_code, 302)

    def test_coverage_and_building_scope(self):
        admin = self.add_admin()
        building = self.add_building('Maple')
        other = self.add_building('Other Hall')
        teammate = self.add_user(sub='team', email='team@rwu.edu', name='Zero Shift RA', building_id=building)
        with app.app_context():
            conn = db()
            sid = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,capacity,created_by) "
                "VALUES('Coverage fixture',?,'2099-01-01','2099-01-03',2,?)", (building, admin),
            ).lastrowid
            conn.execute("INSERT INTO session_order VALUES(?,?,1)", (sid, teammate))
            conn.execute("INSERT INTO session_date_capacities(session_id,duty_date,capacity,updated_by) VALUES(?,'2099-01-01',1,?)", (sid, admin))
            conn.execute("INSERT INTO session_date_overrides(session_id,duty_date,date_kind,updated_by) VALUES(?,'2099-01-03','NO_DUTY',?)", (sid, admin))
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,'2099-01-01',?)", (sid, admin, admin))
            conn.commit()
        self.login_as(admin)
        response = self.request('get', f'/admin/analytics?building_id={building}')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'33%', response.data)
        self.assertIn(b'1 of 3 required slots filled', response.data)
        self.assertIn(b'Zero Shift RA', response.data)
        self.assertIn(b'<strong>2</strong><small>Unfilled slots from today onward', response.data)
        other_response = self.request('get', f'/admin/analytics?building_id={other}')
        self.assertNotIn(b'Coverage fixture', other_response.data)
        self.assertNotIn(b'Zero Shift RA', other_response.data)
        dashboard = self.request('get', '/dashboard')
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b'<h1>Dashboard</h1>', dashboard.data)
        self.assertIn(b'<details class="card create-session-card', dashboard.data)

    def test_logins_per_week(self):
        admin = self.add_admin()
        with app.app_context():
            conn = db()
            conn.execute(
                "INSERT INTO audit_log(actor_user_id,action,target_type,target_id,created_at) "
                "VALUES(?,'auth.login','user',?,'2026-09-10 12:00:00')",
                (admin, admin),
            )
            conn.commit()
        self.login_as(admin)
        response = self.request('get', '/admin/analytics')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Logins per week', response.data)
        self.assertIn(b'Logins this week', response.data)

    def test_hra_analytics_and_dashboard_widgets(self):
        building = self.add_building('Willow')
        other_bldg = self.add_building('Oak')
        hra = self.add_user(sub='hra_user', email='hra@rwu.edu', name='Willow HRA', role='HRA', building_id=building)
        ra = self.add_user(sub='ra_user', email='ra@rwu.edu', name='Willow RA', role='RA', building_id=building)
        unassigned_hra = self.add_user(sub='hra_no_bldg', email='hra2@rwu.edu', name='No Bldg HRA', role='HRA')

        # RA cannot access HRA analytics
        self.login_as(ra)
        self.assertEqual(self.request('get', '/hra/analytics').status_code, 403)

        # RA dashboard has side-by-side glance row with Duty swaps, no Analytics, and no upper-left brand
        ra_dash = self.request('get', '/dashboard')
        self.assertEqual(ra_dash.status_code, 200)
        self.assertIn(b'dashboard-glance-row', ra_dash.data)
        actions_html = ra_dash.data.split(b'class="dashboard-actions-panel"')[1].split(b'</div>\n</div>')[0]
        self.assertIn(b'Duty swaps', actions_html)
        self.assertNotIn(b'Analytics', actions_html)
        self.assertNotIn(b'class="brand"', ra_dash.data)

        # HRA dashboard has Duty swaps and Analytics in the glance row, red HRA badge, and no brand
        self.login_as(hra)
        hra_dash = self.request('get', '/dashboard')
        self.assertEqual(hra_dash.status_code, 200)
        self.assertIn(b'dashboard-glance-row', hra_dash.data)
        hra_actions = hra_dash.data.split(b'class="dashboard-actions-panel"')[1].split(b'</div>\n</div>')[0]
        self.assertIn(b'Duty swaps', hra_actions)
        self.assertIn(b'href="/hra/analytics"', hra_actions)
        self.assertIn(b'hra-role', hra_dash.data)
        self.assertNotIn(b'class="brand"', hra_dash.data)

        # HRA can access HRA analytics
        hra_analytics_resp = self.request('get', '/hra/analytics')
        self.assertEqual(hra_analytics_resp.status_code, 200)
        self.assertIn(b'Assigned shifts by status', hra_analytics_resp.data)
        self.assertIn(b'Duty swap status', hra_analytics_resp.data)
        self.assertIn(b'Assigned shifts by person', hra_analytics_resp.data)
        self.assertIn(b'Willow', hra_analytics_resp.data)

        # Unassigned HRA gets redirected to dashboard
        self.login_as(unassigned_hra)
        resp = self.request('get', '/hra/analytics')
        self.assertEqual(resp.status_code, 302)


# Keep the imported fixture class out of unittest's module discovery.
del AdminManagementTestCase

if __name__ == '__main__':
    unittest.main()
