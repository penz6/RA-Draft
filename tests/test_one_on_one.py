import unittest
from datetime import datetime
from unittest.mock import patch

from test_admin_management import AdminManagementTestCase
from core import app, db
from staff_event_schedule import SCHOOL_TIMEZONE


class OneOnOneTests(unittest.TestCase):
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    login_as = AdminManagementTestCase.login_as

    def setUp(self):
        with app.app_context():
            db().execute("DELETE FROM one_on_one_appointments")
            db().commit()
        AdminManagementTestCase.setUp(self)

    def add_team(self):
        building = self.add_building("Maple")
        ac = self.add_user(sub="ac", email="ac@rwu.edu", name="Area Coordinator",
                           role="RA", building_id=building)
        with app.app_context():
            db().execute("UPDATE users SET is_prostaff=1 WHERE id=?", (ac,))
            db().commit()
        hra = self.add_user(sub="hra", email="hra@rwu.edu", name="Hall Director",
                            role="HRA", building_id=building)
        ra = self.add_user(sub="ra", email="ra@rwu.edu", name="Alex RA",
                           role="RA", building_id=building)
        return building, ac, hra, ra

    def schedule(self, ac, recipient, **changes):
        csrf = self.login_as(ac)
        data = {"csrf": csrf, "recipient_user_id": str(recipient),
                "scheduled_at": "2026-10-14T14:30", "location": "AC office",
                "repeat_weeks": "1"}
        data.update(changes)
        return self.request("post", "/one-on-ones", data=data)

    def test_ac_schedules_recurring_series_for_ra_and_calendar_expands_it(self):
        _building, ac, _hra, ra = self.add_team()
        with patch("one_on_one.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 1, tzinfo=SCHOOL_TIMEZONE)
            clock.strptime = datetime.strptime
            clock.fromisoformat = datetime.fromisoformat
            response = self.schedule(ac, ra)
            self.assertEqual(response.status_code, 302)
            page = self.request("get", "/prostaff?one_on_one_month=2026-10")
        self.assertEqual(page.data.count(b'class="rwu-calendar-event"'), 3)
        self.assertIn(b"Every week", page.data)
        self.assertIn(b"One-on-one calendar for October 2026", page.data)

    def test_ra_and_hra_see_their_own_next_meeting_and_unavailable_otherwise(self):
        _building, ac, hra, ra = self.add_team()
        with patch("one_on_one.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 1, tzinfo=SCHOOL_TIMEZONE)
            clock.strptime = datetime.strptime
            clock.fromisoformat = datetime.fromisoformat
            self.schedule(ac, hra, repeat_weeks="2")
            self.login_as(hra)
            hra_page = self.request("get", "/dashboard")
            self.login_as(ra)
            ra_page = self.request("get", "/dashboard")
        self.assertIn(b"Wed, Oct 14", hra_page.data)
        self.assertIn(b"Every 2 weeks", hra_page.data)
        self.assertIn(b"Unavailable", ra_page.data)

    def test_hra_and_ra_cannot_manage_one_on_ones(self):
        _building, ac, hra, ra = self.add_team()
        for user in (hra, ra):
            csrf = self.login_as(user)
            response = self.request("post", "/one-on-ones", data={
                "csrf": csrf, "recipient_user_id": str(ra),
                "scheduled_at": "2026-10-14T14:30", "location": "Office",
                "repeat_weeks": "1",
            })
            self.assertEqual(response.status_code, 403)
        with patch("one_on_one.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 1, tzinfo=SCHOOL_TIMEZONE)
            clock.strptime = datetime.strptime
            clock.fromisoformat = datetime.fromisoformat
            self.schedule(ac, ra)
        with app.app_context():
            appointment = db().execute("SELECT id FROM one_on_one_appointments").fetchone()["id"]
        csrf = self.login_as(hra)
        self.assertEqual(self.request("post", f"/one-on-ones/{appointment}/delete",
                                      data={"csrf": csrf}).status_code, 403)

    def test_ac_cannot_see_schedule_or_manage_staff_from_another_building(self):
        _building, ac, _hra, _ra = self.add_team()
        other = self.add_building("Willow")
        other_ra = self.add_user(sub="other", email="other@rwu.edu", name="Other RA",
                                 role="RA", building_id=other)
        with app.app_context():
            conn = db()
            series = conn.execute(
                "INSERT INTO one_on_one_appointments"
                "(ra_user_id,scheduled_by,scheduled_at,location,repeat_weeks) VALUES(?,?,?,?,?)",
                (other_ra, ac, "2026-10-14T15:30", "Willow office", 1),
            ).lastrowid
            conn.commit()
        self.login_as(ac)
        page = self.request("get", "/prostaff?one_on_one_month=2026-10")
        self.assertNotIn(b"Other RA", page.data)
        self.assertNotIn(b"Willow office", page.data)
        with patch("one_on_one.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 1, tzinfo=SCHOOL_TIMEZONE)
            clock.strptime = datetime.strptime
            clock.fromisoformat = datetime.fromisoformat
            self.assertEqual(self.schedule(ac, other_ra).status_code, 403)
        csrf = self.login_as(ac)
        self.assertEqual(self.request("post", f"/one-on-ones/{series}/delete",
                                      data={"csrf": csrf}).status_code, 404)

    def test_recurring_series_cannot_double_book_recipient_or_coordinator(self):
        _building, ac, hra, ra = self.add_team()
        with patch("one_on_one.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 1, tzinfo=SCHOOL_TIMEZONE)
            clock.strptime = datetime.strptime
            clock.fromisoformat = datetime.fromisoformat
            self.assertEqual(self.schedule(ac, ra).status_code, 302)

            # A biweekly series beginning on the next week's occurrence still
            # intersects the existing weekly recipient series.
            response = self.schedule(
                ac, ra, scheduled_at="2026-10-21T14:30", repeat_weeks="2"
            )
            self.assertEqual(response.status_code, 302)

            # The same Area Coordinator also cannot schedule a different
            # recipient during one of their own recurring meeting times.
            response = self.schedule(
                ac, hra, scheduled_at="2026-10-21T14:30", repeat_weeks="2"
            )
            self.assertEqual(response.status_code, 302)

        with app.app_context():
            count = db().execute("SELECT COUNT(*) total FROM one_on_one_appointments").fetchone()
            self.assertEqual(count["total"], 1)

    def test_ac_can_schedule_an_admin_assigned_to_the_same_building(self):
        building, ac, _hra, _ra = self.add_team()
        admin = self.add_user(
            sub="assigned-admin", email="assigned-admin@rwu.edu",
            name="Assigned Admin", role="ADMIN", building_id=building,
        )
        with patch("one_on_one.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 1, tzinfo=SCHOOL_TIMEZONE)
            clock.strptime = datetime.strptime
            clock.fromisoformat = datetime.fromisoformat
            self.assertEqual(self.schedule(ac, admin).status_code, 302)
            self.login_as(admin)
            page = self.request("get", "/dashboard")
        self.assertIn(b"Wed, Oct 14", page.data)
        self.assertIn(b"AC office", page.data)


if __name__ == "__main__":
    unittest.main()
