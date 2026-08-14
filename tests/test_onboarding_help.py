import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = tempfile.mkdtemp(prefix="ra-draft-onboarding-tests-")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(TEST_DIR) / "test.db"))

import portal_app  # noqa: E402,F401
from core import app, db, oauth  # noqa: E402


class OnboardingHelpTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
        with app.app_context():
            conn = db()
            for table in (
                "audit_log",
                "session_deferrals",
                "assignments",
                "session_order",
                "draft_sessions",
                "users",
                "buildings",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def add_building(self, name):
        with app.app_context():
            cur = db().execute("INSERT INTO buildings(name) VALUES(?)", (name,))
            db().commit()
            return cur.lastrowid

    def add_user(self, *, sub, email, name, role="RA", building_id=None):
        with app.app_context():
            cur = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                (sub, email, name, role, building_id),
            )
            db().commit()
            return cur.lastrowid

    def login_as(self, user_id, csrf="onboarding-csrf", show_help=False):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
            if show_help:
                flask_session["show_role_help"] = True
        return csrf

    def test_login_page_is_minimal(self):
        response = self.request("get", "/")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("RA Duty Picking", page)
        self.assertIn("Continue with Google", page)
        self.assertNotIn("spreadsheet scramble", page)
        self.assertNotIn("feature-grid", page)

    def test_new_google_user_defaults_to_ra_and_reaches_building_picker(self):
        self.add_building("North Hall")
        info = {
            "sub": "new-ra-sub",
            "email": "new.ra@g.rwu.edu",
            "email_verified": True,
            "hd": "g.rwu.edu",
            "name": "New RA",
        }
        with patch.object(oauth.google, "authorize_access_token", return_value={"userinfo": info}):
            response = self.request("get", "/auth/callback", follow_redirects=True)
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose your building", page)
        self.assertIn('data-auto-open="true"', page)
        self.assertIn("Wait for your turn", page)
        with app.app_context():
            user = db().execute(
                "SELECT role,building_id FROM users WHERE google_sub=?",
                ("new-ra-sub",),
            ).fetchone()
            self.assertEqual(user["role"], "RA")
            self.assertIsNone(user["building_id"])

    def test_ra_can_choose_a_building_only_once(self):
        first = self.add_building("North Hall")
        second = self.add_building("South Hall")
        ra_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="RA",
        )
        csrf = self.login_as(ra_id)
        response = self.request(
            "post",
            "/onboarding",
            data={"csrf": csrf, "building_id": first},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))
        with app.app_context():
            user = db().execute("SELECT building_id FROM users WHERE id=?", (ra_id,)).fetchone()
            self.assertEqual(user["building_id"], first)
            event = db().execute(
                "SELECT action FROM audit_log WHERE actor_user_id=? ORDER BY id DESC LIMIT 1",
                (ra_id,),
            ).fetchone()
            self.assertEqual(event["action"], "profile.building.select")

        response = self.request(
            "post",
            "/onboarding",
            data={"csrf": csrf, "building_id": second},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            user = db().execute("SELECT building_id FROM users WHERE id=?", (ra_id,)).fetchone()
            self.assertEqual(user["building_id"], first)

    def test_invalid_building_selection_is_rejected(self):
        ra_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="RA",
        )
        csrf = self.login_as(ra_id)
        response = self.request(
            "post",
            "/onboarding",
            data={"csrf": csrf, "building_id": 999999},
        )
        self.assertEqual(response.status_code, 400)
        with app.app_context():
            user = db().execute("SELECT building_id FROM users WHERE id=?", (ra_id,)).fetchone()
            self.assertIsNone(user["building_id"])

    def test_hra_cannot_use_onboarding_to_change_buildings(self):
        first = self.add_building("North Hall")
        second = self.add_building("South Hall")
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=first,
        )
        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            "/onboarding",
            data={"csrf": csrf, "building_id": second},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))
        with app.app_context():
            user = db().execute("SELECT building_id FROM users WHERE id=?", (hra_id,)).fetchone()
            self.assertEqual(user["building_id"], first)

    def test_hra_dashboard_explains_immediate_session_start(self):
        building_id = self.add_building("North Hall")
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="RA",
            building_id=building_id,
        )
        self.login_as(hra_id, show_help=True)
        response = self.request("get", "/dashboard")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Create and open a duty session", page)
        self.assertIn("Create and open session", page)
        self.assertIn("The first turn starts immediately", page)
        self.assertIn('data-auto-open="true"', page)
        self.assertIn("data-help-open", page)


if __name__ == "__main__":
    unittest.main()
