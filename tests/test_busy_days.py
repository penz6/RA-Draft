import unittest

import test_admin_management as fixtures


class BusyDaysTests(unittest.TestCase):
    setUp = fixtures.AdminManagementTestCase.setUp
    request = fixtures.AdminManagementTestCase.request
    add_building = fixtures.AdminManagementTestCase.add_building
    add_user = fixtures.AdminManagementTestCase.add_user
    login_as = fixtures.AdminManagementTestCase.login_as

    def test_busy_date_is_private_editable_and_banner_dismissal_is_independent(self):
        building = self.add_building()
        users = [self.add_user(sub=name, email=name+"@rwu.edu", name=name,
                               building_id=building) for name in ("Alex", "Sam")]
        csrf = self.login_as(users[0])
        path = "/settings/busy-days"
        data = {"action": "add", "day": "2027-01-04"}
        self.assertEqual(self.request("post", path, data=data).status_code, 400)
        data["csrf"] = csrf
        for _ in range(2):
            self.assertEqual(self.request("post", path, data=data).status_code, 302)
        self.assertIn(b'2027-01-04', self.request("get", path).data)
        self.assertIn(b'Busy days announcement', self.request("get", "/dashboard").data)
        self.request("post", path, data={"action": "dismiss", "csrf": csrf})
        page = self.request("get", "/dashboard").data
        self.assertNotIn(b'Busy days announcement', page)
        self.assertIn(b'My busy days', page)
        self.login_as(users[1])
        self.assertNotIn(b'2027-01-04', self.request("get", path).data)
        self.assertIn(b'Busy days announcement', self.request("get", "/dashboard").data)
        self.login_as(users[0])
        data["action"] = "remove"
        self.request("post", path, data=data)
        self.assertNotIn(b'2027-01-04', self.request("get", path).data)
