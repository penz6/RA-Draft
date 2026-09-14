import unittest
from unittest.mock import patch

from test_admin_management import AdminManagementTestCase
from core import app, db
from live_updates import dashboard_state_version


class BuildingStaffEventTests(unittest.TestCase):
    setUp = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    add_admin = AdminManagementTestCase.add_admin
    login_as = AdminManagementTestCase.login_as

    def test_admin_can_set_accent_color(self):
        building = self.add_building("Maple")
        admin = self.add_admin()
        csrf = self.login_as(admin)

        response = self.request(
            "post",
            f"/admin/buildings/{building}/appearance",
            data={
                "csrf": csrf,
                "theme_key": "maple",
                "accent_color": "#7b1f2b",
            },
        )
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            row = db().execute(
                "SELECT theme_key,accent_color FROM buildings WHERE id=?",
                (building,),
            ).fetchone()
            self.assertEqual(row["theme_key"], "maple")
            self.assertEqual(row["accent_color"], "#7b1f2b")

        css = self.request("get", f"/buildings/{building}/theme.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn(b"--hall-accent:#7b1f2b", css.data)

    def test_invalid_accent_color_is_rejected(self):
        building = self.add_building("Maple")
        admin = self.add_admin()
        csrf = self.login_as(admin)
        response = self.request(
            "post",
            f"/admin/buildings/{building}/appearance",
            data={
                "csrf": csrf,
                "theme_key": "maple",
                "accent_color": "red;body{display:none}",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            row = db().execute(
                "SELECT accent_color FROM buildings WHERE id=?",
                (building,),
            ).fetchone()
            self.assertIsNone(row["accent_color"])

    def test_hra_can_edit_own_staff_meeting_and_dinner(self):
        building = self.add_building("Willow")
        hra = self.add_user(
            sub="hra",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building,
        )
        csrf = self.login_as(hra)

        meeting = self.request(
            "post",
            f"/buildings/{building}/staff-meeting",
            data={
                "csrf": csrf,
                "event_at": "2026-10-04T19:30",
                "event_location": "Willow Lounge",
            },
        )
        dinner = self.request(
            "post",
            f"/buildings/{building}/staff-dinner",
            data={
                "csrf": csrf,
                "event_at": "2026-10-08T18:00",
                "event_location": "Commons",
            },
        )
        self.assertEqual(meeting.status_code, 302)
        self.assertEqual(dinner.status_code, 302)

        with app.app_context():
            row = db().execute("SELECT * FROM buildings WHERE id=?", (building,)).fetchone()
            self.assertEqual(row["staff_meeting_at"], "2026-10-04T19:30")
            self.assertEqual(row["staff_meeting_location"], "Willow Lounge")
            self.assertEqual(row["staff_dinner_at"], "2026-10-08T18:00")
            self.assertEqual(row["staff_dinner_location"], "Commons")

    def test_ra_cannot_edit_staff_events(self):
        building = self.add_building("Willow")
        ra = self.add_user(
            sub="ra",
            email="ra@rwu.edu",
            name="RA",
            role="RA",
            building_id=building,
        )
        csrf = self.login_as(ra)
        response = self.request(
            "post",
            f"/buildings/{building}/staff-meeting",
            data={
                "csrf": csrf,
                "event_at": "2026-10-04T19:30",
                "event_location": "Willow Lounge",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_hra_cannot_edit_another_building(self):
        own = self.add_building("Willow")
        other = self.add_building("Maple")
        hra = self.add_user(
            sub="hra",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=own,
        )
        csrf = self.login_as(hra)
        response = self.request(
            "post",
            f"/buildings/{other}/staff-dinner",
            data={
                "csrf": csrf,
                "event_at": "2026-10-08T18:00",
                "event_location": "Commons",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_edit_any_building_staff_event(self):
        building = self.add_building("Maple")
        admin = self.add_admin()
        csrf = self.login_as(admin)
        response = self.request(
            "post",
            f"/buildings/{building}/staff-meeting",
            data={
                "csrf": csrf,
                "event_at": "2026-11-01T20:00",
                "event_location": "First Floor Lounge",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            row = db().execute(
                "SELECT staff_meeting_location FROM buildings WHERE id=?",
                (building,),
            ).fetchone()
            self.assertEqual(row["staff_meeting_location"], "First Floor Lounge")

    def test_unassigned_admin_can_manage_events_from_admin_page(self):
        building = self.add_building("Maple")
        admin = self.add_admin()
        csrf = self.login_as(admin)

        page = self.request("get", "/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            f'action="/buildings/{building}/staff-meeting"'.encode(),
            page.data,
        )
        self.assertIn(b'name="return_to" value="admin"', page.data)

        response = self.request(
            "post",
            f"/buildings/{building}/staff-meeting",
            data={
                "csrf": csrf,
                "return_to": "admin",
                "event_at": "2026-11-01T20:00",
                "event_location": "First Floor Lounge",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(f"/admin#building-{building}"))

    def test_appearance_revalidates_admin_after_write_lock(self):
        building = self.add_building("Maple")
        admin = self.add_admin()
        csrf = self.login_as(admin)

        def demote_before_lock(_value, *, theme_key):
            conn = db()
            conn.execute("UPDATE users SET role='RA' WHERE id=?", (admin,))
            conn.commit()
            return "#7b1f2b"

        with patch(
            "building_settings.normalize_accent_color",
            side_effect=demote_before_lock,
        ):
            response = self.request(
                "post",
                f"/admin/buildings/{building}/appearance",
                data={
                    "csrf": csrf,
                    "theme_key": "maple",
                    "accent_color": "#7b1f2b",
                },
            )
        self.assertEqual(response.status_code, 403)
        with app.app_context():
            row = db().execute(
                "SELECT theme_key,accent_color FROM buildings WHERE id=?",
                (building,),
            ).fetchone()
            self.assertEqual(row["theme_key"], "rwu")
            self.assertIsNone(row["accent_color"])

    def test_staff_event_revalidates_hra_building_after_write_lock(self):
        original = self.add_building("Willow")
        reassigned = self.add_building("Maple")
        hra = self.add_user(
            sub="hra-reassigned",
            email="hra-reassigned@rwu.edu",
            name="Reassigned HRA",
            role="HRA",
            building_id=original,
        )
        csrf = self.login_as(hra)

        def reassign_before_lock():
            conn = db()
            conn.execute(
                "UPDATE users SET building_id=? WHERE id=?",
                (reassigned, hra),
            )
            conn.commit()
            return "2026-11-01T20:00", "Willow Lounge"

        with patch(
            "building_settings._parse_staff_event_form",
            side_effect=reassign_before_lock,
        ):
            response = self.request(
                "post",
                f"/buildings/{original}/staff-meeting",
                data={"csrf": csrf},
            )
        self.assertEqual(response.status_code, 403)
        with app.app_context():
            row = db().execute(
                "SELECT staff_meeting_at FROM buildings WHERE id=?",
                (original,),
            ).fetchone()
            self.assertIsNone(row["staff_meeting_at"])

    def test_dashboard_version_tracks_building_profile_changes(self):
        building = self.add_building("Willow")
        ra = self.add_user(
            sub="version-ra",
            email="version-ra@rwu.edu",
            name="Version RA",
            building_id=building,
        )
        with app.app_context():
            conn = db()
            viewer = conn.execute(
                "SELECT users.*,buildings.name AS building_name FROM users "
                "LEFT JOIN buildings ON buildings.id=users.building_id WHERE users.id=?",
                (ra,),
            ).fetchone()
            before = dashboard_state_version(viewer)
            conn.execute(
                "UPDATE buildings SET staff_dinner_at='2026-11-02T18:00',"
                "staff_dinner_location='Commons' WHERE id=?",
                (building,),
            )
            conn.commit()
            after = dashboard_state_version(viewer)
        self.assertNotEqual(before, after)


# Keep the imported fixture class out of unittest discovery.
del AdminManagementTestCase


if __name__ == "__main__":
    unittest.main()
