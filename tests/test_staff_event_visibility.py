"""Regular RAs can read their building's staff events, without editing them."""

from datetime import datetime
import re
import unittest
from unittest.mock import patch

from test_admin_management import AdminManagementTestCase
from core import app, db
from staff_event_schedule import SCHOOL_TIMEZONE


class StaffEventVisibilityTests(unittest.TestCase):
    setUp = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    add_admin = AdminManagementTestCase.add_admin
    login_as = AdminManagementTestCase.login_as

    def add_ra(self, building):
        return self.add_user(
            sub='event-viewer', email='viewer@g.rwu.edu', name='Event Viewer',
            building_id=building,
        )

    def dashboard_events(self):
        with patch('staff_event_schedule.school_now', return_value=datetime(2026, 10, 10, 12, tzinfo=SCHOOL_TIMEZONE)):
            response = self.request('get', '/dashboard')
        self.assertEqual(response.status_code, 200)
        match = re.search(
            r'<section class="rwu-staff-events"[^>]*>(.*?)</section>',
            response.get_data(as_text=True), re.S,
        )
        return match.group(1) if match else ''

    def test_ra_sees_scheduled_events_without_edit_controls_or_duty_assignments(self):
        hall = self.add_building('Maple')
        self.login_as(self.add_ra(hall))
        with app.app_context():
            db().execute(
                "UPDATE buildings SET staff_meeting_at='2026-10-20T19:00',"
                "staff_meeting_location='Maple Lounge',staff_dinner_at='2026-10-21T18:00',"
                "staff_dinner_location='Commons' WHERE id=?", (hall,),
            )
            db().commit()
        events = self.dashboard_events()
        for text in ('Staff Dinner &amp; Meeting', 'Maple Lounge', 'Oct 20', '7:00 PM'):
            self.assertIn(text, events)
        for text in ('Commons', 'Oct 21', '6:00 PM'):
            self.assertNotIn(text, events)
        self.assertEqual(events.count('class="card rwu-staff-event-card"'), 2)
        self.assertNotIn('rwu-meeting-editor', events)
        self.assertNotIn('<form', events)
        self.assertNotIn('name="repeat_weeks"', events)

    def test_ra_sees_next_recurring_dates_and_repeat_labels(self):
        hall = self.add_building('Maple')
        self.login_as(self.add_ra(hall))
        with app.app_context():
            db().execute(
                "UPDATE buildings SET staff_meeting_at='2026-10-01T19:00',"
                "staff_meeting_location='Maple Lounge',staff_meeting_repeat_weeks=2,"
                "staff_dinner_at='2026-10-02T18:00',staff_dinner_location='Commons',"
                "staff_dinner_repeat_weeks=1 WHERE id=?", (hall,),
            )
            db().commit()
        events = self.dashboard_events()
        for text in ('Oct 15', 'Every 2 weeks'):
            self.assertIn(text, events)
        self.assertNotIn('Oct 16', events)
        self.assertNotIn('Every week', events)
        self.assertNotIn('rwu-meeting-editor', events)
        with app.app_context():
            row = db().execute('SELECT staff_meeting_at FROM buildings WHERE id=?', (hall,)).fetchone()
            self.assertEqual(row['staff_meeting_at'], '2026-10-01T19:00')

    def test_ra_does_not_see_unscheduled_or_other_building_events(self):
        hall = self.add_building('Maple')
        other = self.add_building('Willow')
        self.login_as(self.add_ra(hall))
        with app.app_context():
            db().execute(
                "UPDATE buildings SET staff_meeting_at='2026-10-20T19:00',"
                "staff_meeting_location='Willow Only' WHERE id=?", (other,),
            )
            db().commit()
        events = self.dashboard_events()
        self.assertIn('Staff Dinner &amp; Meeting', events)
        self.assertIn('One-on-one time', events)
        self.assertNotIn('Other lounge', events)
        with app.app_context():
            db().execute(
                "UPDATE buildings SET staff_dinner_at='2026-10-21T18:00',"
                "staff_dinner_location='Maple Only' WHERE id=?", (hall,),
            )
            db().commit()
        events = self.dashboard_events()
        self.assertIn('Staff Dinner &amp; Meeting', events)
        self.assertIn('Maple Only', events)
        self.assertNotIn('Willow Only', events)

    def test_hra_keeps_edit_controls_for_own_building(self):
        hall = self.add_building('Maple')
        hra = self.add_user(sub='event-manager', email='manager@rwu.edu',
                            name='Event Manager', role='HRA', building_id=hall)
        self.login_as(hra)
        events = self.dashboard_events()
        self.assertIn('Staff Dinner &amp; Meeting', events)
        self.assertEqual(events.count('class="rwu-meeting-editor"'), 1)
        self.assertNotIn('Schedule an RA', events)
        self.assertIn(f'action="/buildings/{hall}/staff-meeting"', events)
        self.assertNotIn(f'action="/buildings/{hall}/staff-dinner"', events)

    def test_swap_building_selector_retains_the_building_link(self):
        hall = self.add_building('Maple')
        admin = self.add_admin()
        self.login_as(admin)
        with app.app_context():
            db().execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES(?,?,?,?,?,'CLOSED')",
                ('October duty', hall, '2026-10-01', '2026-10-07', admin),
            )
            db().commit()
        response = self.request('get', '/swaps')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(f'class="session-card swap-building-card" href="/swaps/building/{hall}"', html)
        self.assertIn('class="swap-building-arrow" aria-hidden="true"', html)
        destination = self.request('get', f'/swaps/building/{hall}')
        self.assertEqual(destination.status_code, 200)


# Do not discover the imported fixture's tests a second time.
del AdminManagementTestCase


if __name__ == '__main__':
    unittest.main()
