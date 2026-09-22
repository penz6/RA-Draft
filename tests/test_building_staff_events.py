import io
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch

from test_admin_management import AdminManagementTestCase
from core import app, db
from live_updates import dashboard_state_version
from staff_event_schedule import SCHOOL_TIMEZONE


class BuildingStaffEventTests(unittest.TestCase):
    setUp = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    add_admin = AdminManagementTestCase.add_admin
    login_as = AdminManagementTestCase.login_as

    def row(self, building):
        with app.app_context():
            return dict(db().execute('SELECT * FROM buildings WHERE id=?', (building,)).fetchone())

    def hra(self, building):
        return self.add_user(sub='hra', email='hra@rwu.edu', name='HRA', role='HRA', building_id=building)

    def event(self, building, kind, csrf, **changes):
        data = {'csrf': csrf, 'event_at': '2026-10-04T19:30', 'event_location': 'Lounge', 'repeat_weeks': '2'}
        data.update(changes)
        return self.request('post', f'/buildings/{building}/staff-{kind}', data=data)

    def test_fixed_navy_ignores_old_database_color(self):
        building = self.add_building('Maple')
        self.login_as(self.add_admin())
        with app.app_context():
            db().execute("UPDATE buildings SET accent_color='#ffffff' WHERE id=?", (building,))
            db().commit()
        css = self.request('get', f'/buildings/{building}/theme.css')
        self.assertEqual(css.status_code, 200)
        self.assertIn(b'--hall-accent:#01295f', css.data)
        self.assertNotIn(b'#ffffff', css.data)
        page = self.request('get', '/admin')
        self.assertNotIn(b'type="color"', page.data)
        self.assertNotIn(b'type="file"', page.data)
        self.assertNotIn(b'name="theme_key"', page.data)
        self.assertNotIn(b'Save color', page.data)

    def test_retired_appearance_endpoint_cannot_change_data(self):
        building = self.add_building('Maple')
        csrf = self.login_as(self.add_admin())
        before = self.row(building)
        response = self.request('post', f'/admin/buildings/{building}/appearance', data={
            'csrf': csrf, 'accent_color': '#ffffff', 'theme_key': 'maple',
            'icon_svg': (io.BytesIO(b'<svg/>'), 'mark.svg'),
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.row(building), before)
        self.assertEqual(self.request('get', f'/buildings/{building}/icon.svg').status_code, 404)

    def test_new_building_has_fixed_navy_and_ignores_forged_appearance(self):
        csrf = self.login_as(self.add_admin())
        response = self.request('post', '/admin/buildings/profiled', data={
            'csrf': csrf, 'name': 'New Hall', 'accent_color': 'red;body{display:none}',
            'theme_key': 'maple', 'icon_svg': (io.BytesIO(b'<svg/>'), 'mark.svg'),
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            row = db().execute("SELECT * FROM buildings WHERE name='New Hall'").fetchone()
            self.assertEqual(row['accent_color'], '#01295f')
            self.assertEqual(row['theme_key'], 'rwu')
            self.assertIsNone(row['icon_svg'])

    def test_building_create_revalidates_admin_after_write_lock(self):
        admin = self.add_admin()
        csrf = self.login_as(admin)
        def demote_before_lock(_value, **_kwargs):
            db().execute("UPDATE users SET role='RA' WHERE id=?", (admin,))
            db().commit()
            return 'Race Hall'
        with patch('building_settings.clean_single_line', side_effect=demote_before_lock):
            response = self.request('post', '/admin/buildings/profiled', data={'csrf': csrf, 'name': 'Race Hall'})
        self.assertEqual(response.status_code, 403)
        with app.app_context():
            self.assertIsNone(db().execute("SELECT id FROM buildings WHERE name='Race Hall'").fetchone())

    def test_hra_can_edit_own_staff_meeting_and_dinner_independently(self):
        building = self.add_building('Willow')
        csrf = self.login_as(self.hra(building))
        self.assertEqual(self.event(building, 'meeting', csrf).status_code, 302)
        self.assertEqual(self.event(building, 'dinner', csrf, repeat_weeks='3', event_location='Commons').status_code, 302)
        row = self.row(building)
        self.assertEqual(row['staff_meeting_repeat_weeks'], 2)
        self.assertEqual(row['staff_dinner_repeat_weeks'], 3)
        self.assertEqual(row['staff_dinner_location'], 'Commons')
        with patch('staff_event_schedule.school_now', return_value=datetime(2026, 10, 10, 12, tzinfo=SCHOOL_TIMEZONE)):
            page = self.request('get', '/dashboard')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Every 2 weeks', page.data)
        self.assertIn(b'Oct 18', page.data)
        self.assertNotIn(b'Every 3 weeks', page.data)
        self.assertNotIn(b'Oct 25', page.data)
        self.assertIn(b'value="2026-10-04T19:30"', page.data)
        self.assertEqual(self.row(building)['staff_meeting_at'], '2026-10-04T19:30')

    def test_one_time_edit_stop_repeat_and_clear(self):
        building = self.add_building('Willow')
        csrf = self.login_as(self.hra(building))
        for kind in ('meeting', 'dinner'):
            with self.subTest(kind=kind):
                self.assertEqual(self.event(building, kind, csrf).status_code, 302)
                self.assertEqual(self.event(building, kind, csrf, repeat_weeks='').status_code, 302)
                self.assertEqual(self.row(building)[f'staff_{kind}_repeat_weeks'], 0)
                self.event(building, kind, csrf)
                self.assertEqual(self.event(building, kind, csrf, event_at='', event_location='').status_code, 302)
                row = self.row(building)
                self.assertIsNone(row[f'staff_{kind}_at'])
                self.assertIsNone(row[f'staff_{kind}_location'])
                self.assertEqual(row[f'staff_{kind}_repeat_weeks'], 0)

    def test_invalid_recurrence_or_date_does_not_change_saved_series(self):
        building = self.add_building('Willow')
        csrf = self.login_as(self.hra(building))
        self.event(building, 'meeting', csrf)
        before = self.row(building)
        for changes in ({'repeat_weeks': '-1'}, {'repeat_weeks': '1.5'}, {'repeat_weeks': '53'},
                        {'repeat_weeks': 'weekly'}, {'event_at': '2026-10-04'},
                        {'event_at': '2026-10-04T19:30Z'}, {'event_at': '2026-03-08T02:30'},
                        {'event_location': ''}):
            with self.subTest(changes=changes):
                self.assertEqual(self.event(building, 'meeting', csrf, **changes).status_code, 302)
                self.assertEqual(self.row(building), before)

    def test_ra_cannot_edit_staff_events(self):
        building = self.add_building('Willow')
        ra = self.add_user(sub='ra', email='ra@rwu.edu', name='RA', building_id=building)
        csrf = self.login_as(ra)
        for kind in ('meeting', 'dinner'):
            self.assertEqual(self.event(building, kind, csrf).status_code, 403)
        self.assertIsNone(self.row(building)['staff_meeting_at'])

    def test_hra_cannot_edit_another_building(self):
        own = self.add_building('Willow')
        other = self.add_building('Maple')
        csrf = self.login_as(self.hra(own))
        for kind in ('meeting', 'dinner'):
            self.assertEqual(self.event(other, kind, csrf).status_code, 403)
        self.assertIsNone(self.row(other)['staff_dinner_at'])

    def test_staff_events_require_csrf(self):
        building = self.add_building('Willow')
        self.login_as(self.hra(building))
        for kind in ('meeting', 'dinner'):
            self.assertEqual(self.event(building, kind, 'wrong-csrf').status_code, 400)
        self.assertIsNone(self.row(building)['staff_meeting_at'])

    def test_unassigned_admin_can_manage_events_from_admin_page(self):
        building = self.add_building('Maple')
        csrf = self.login_as(self.add_admin())
        page = self.request('get', '/admin')
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'action="/buildings/{building}/staff-meeting"'.encode(), page.data)
        self.assertIn(b'name="repeat_weeks"', page.data)
        for kind in ('meeting', 'dinner'):
            response = self.event(building, kind, csrf, return_to='admin')
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith(f'/admin#building-{building}'))
            self.assertEqual(self.row(building)[f'staff_{kind}_repeat_weeks'], 2)

    def test_staff_event_revalidates_hra_building_after_write_lock(self):
        original = self.add_building('Willow')
        reassigned = self.add_building('Maple')
        hra = self.hra(original)
        csrf = self.login_as(hra)
        def reassign_before_lock():
            db().execute('UPDATE users SET building_id=? WHERE id=?', (reassigned, hra))
            db().commit()
            return '2026-11-01T20:00', 'Willow Lounge', 2
        with patch('building_settings._parse_staff_event_form', side_effect=reassign_before_lock):
            response = self.event(original, 'meeting', csrf)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.row(original)['staff_meeting_at'])
        self.assertEqual(self.row(original)['staff_meeting_repeat_weeks'], 0)

    def test_staff_event_revalidates_disabled_admin_after_write_lock(self):
        building = self.add_building('Maple')
        admin = self.add_admin()
        csrf = self.login_as(admin)
        def disable_before_lock():
            db().execute('UPDATE users SET disabled=1 WHERE id=?', (admin,))
            db().commit()
            return '2026-11-01T20:00', 'Lounge', 1
        with patch('building_settings._parse_staff_event_form', side_effect=disable_before_lock):
            response = self.event(building, 'dinner', csrf)
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.row(building)['staff_dinner_at'])

    def test_dashboard_version_tracks_building_profile_changes(self):
        building = self.add_building('Willow')
        ra = self.add_user(sub='version-ra', email='version@rwu.edu', name='Version RA', building_id=building)
        with app.app_context():
            conn = db()
            viewer = conn.execute('SELECT users.*,buildings.name AS building_name FROM users LEFT JOIN buildings ON buildings.id=users.building_id WHERE users.id=?', (ra,)).fetchone()
            before = dashboard_state_version(viewer)
            conn.execute("UPDATE buildings SET staff_dinner_at='2026-11-02T18:00',staff_dinner_location='Commons' WHERE id=?", (building,))
            conn.commit()
            self.assertNotEqual(before, dashboard_state_version(viewer))

    def test_schema_upgrade_preserves_existing_dates_and_repeat_settings(self):
        from building_settings import _ensure_building_profile_schema
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('CREATE TABLE buildings(id INTEGER PRIMARY KEY,name TEXT,staff_meeting_at TEXT,staff_meeting_location TEXT,staff_dinner_at TEXT,staff_dinner_location TEXT)')
            conn.execute("INSERT INTO buildings VALUES(1,'Maple','2026-10-04T19:00','Lounge','2026-10-05T18:00','Commons')")
            conn.commit()
            with patch('building_settings.db', return_value=conn):
                _ensure_building_profile_schema()
                row = conn.execute('SELECT * FROM buildings').fetchone()
                self.assertEqual(row['staff_meeting_at'], '2026-10-04T19:00')
                self.assertEqual(row['staff_dinner_at'], '2026-10-05T18:00')
                self.assertEqual(row['staff_meeting_repeat_weeks'], 0)
                conn.execute('UPDATE buildings SET staff_dinner_repeat_weeks=2')
                conn.commit()
                _ensure_building_profile_schema()
                self.assertEqual(conn.execute('SELECT staff_dinner_repeat_weeks FROM buildings').fetchone()[0], 2)
        finally:
            conn.close()


# Keep the imported fixture class out of unittest discovery.
del AdminManagementTestCase

if __name__ == '__main__':
    unittest.main()
