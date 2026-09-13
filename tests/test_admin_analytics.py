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


# Keep the imported fixture class out of unittest's module discovery.
del AdminManagementTestCase

if __name__ == '__main__':
    unittest.main()
