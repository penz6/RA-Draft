"""Recurring events invalidate dashboard state without changing their anchor."""
from datetime import datetime
import queue
import unittest
from unittest.mock import patch
from test_admin_management import AdminManagementTestCase
from core import app, db
from live_updates import dashboard_state_version
from staff_event_schedule import SCHOOL_TIMEZONE


class StaffEventLiveTests(unittest.TestCase):
    setUp = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    login_as = AdminManagementTestCase.login_as

    def test_interval_edit_and_next_occurrence_change_dashboard_version(self):
        hall = self.add_building('Maple')
        ra = self.add_user(sub='ra',email='ra@rwu.edu',name='RA',building_id=hall)
        with app.app_context():
            conn = db()
            viewer = conn.execute('SELECT users.*,buildings.name AS building_name FROM users LEFT JOIN buildings ON buildings.id=users.building_id WHERE users.id=?',(ra,)).fetchone()
            conn.execute("UPDATE buildings SET staff_meeting_at='2026-09-01T18:00',staff_meeting_repeat_weeks=1,staff_meeting_location='Lounge' WHERE id=?",(hall,))
            conn.commit()
            with patch('live_updates.school_now',return_value=datetime(2026,9,1,17,tzinfo=SCHOOL_TIMEZONE)):
                before = dashboard_state_version(viewer)
                self.assertEqual(before,dashboard_state_version(viewer))
                conn.execute('UPDATE buildings SET staff_meeting_repeat_weeks=2 WHERE id=?',(hall,))
                conn.commit()
                edited = dashboard_state_version(viewer)
                self.assertNotEqual(before,edited)
            with patch('live_updates.school_now',return_value=datetime(2026,9,1,18,1,tzinfo=SCHOOL_TIMEZONE)):
                advanced = dashboard_state_version(viewer)
            self.assertNotEqual(edited,advanced)
            row = conn.execute('SELECT staff_meeting_at FROM buildings WHERE id=?',(hall,)).fetchone()
            self.assertEqual(row[0],'2026-09-01T18:00')

    def test_dashboard_heartbeat_publishes_time_based_change(self):
        hall = self.add_building('Maple')
        ra = self.add_user(sub='ra',email='ra@rwu.edu',name='RA',building_id=hall)
        self.login_as(ra)
        with patch('live_updates.LiveSubscriber.get',side_effect=queue.Empty), patch('live_updates._stream_version',return_value='next-event-version'):
            response = self.request('get','/live-events',buffered=False)
            try:
                stream = iter(response.response)
                self.assertIn(b'event: state',next(stream))
                self.assertIn(b'next-event-version',next(stream))
            finally:
                response.close()


del AdminManagementTestCase
if __name__ == '__main__':
    unittest.main()
