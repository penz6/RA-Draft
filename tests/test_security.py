import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = tempfile.mkdtemp(prefix="ra-draft-tests-")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(TEST_DIR) / "test.db"))

import portal_app  # noqa: E402,F401
from calendar_routes import ics_escape  # noqa: E402
from core import app, clean_single_line, db, google_identity_allowed, oauth  # noqa: E402


class SecurityTestCase(unittest.TestCase):
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

    def login_as(self, user_id, csrf="test-csrf-token"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def test_single_line_validation_rejects_control_characters(self):
        for value in ("North\rHall", "North\nHall", "North\x00Hall", "North\tHall"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    clean_single_line(value, max_length=80)

    def test_email_verified_must_be_boolean_true(self):
        info = {
            "sub": "123",
            "email": "person@rwu.edu",
            "email_verified": "true",
            "hd": "rwu.edu",
        }
        self.assertFalse(google_identity_allowed(info))
        info["email_verified"] = True
        self.assertTrue(google_identity_allowed(info))

    def test_sql_metacharacters_are_stored_as_data(self):
        admin_id = self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        csrf = self.login_as(admin_id)
        payload = "Hall'); DROP TABLE users;--"
        response = self.request(
            "post",
            "/admin/buildings",
            data={"csrf": csrf, "name": payload},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            stored = db().execute(
                "SELECT name FROM buildings WHERE name=?",
                (payload,),
            ).fetchone()
            self.assertEqual(stored["name"], payload)
            self.assertIsNotNone(db().execute("SELECT COUNT(*) n FROM users").fetchone())

    def test_html_is_autoescaped_in_admin_ui(self):
        admin_id = self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        csrf = self.login_as(admin_id)
        payload = "<script>alert(1)</script>"
        self.request(
            "post",
            "/admin/buildings",
            data={"csrf": csrf, "name": payload},
        )
        response = self.request("get", "/admin")
        page = response.get_data(as_text=True)
        self.assertNotIn(payload, page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_post_without_csrf_is_rejected(self):
        admin_id = self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        self.login_as(admin_id)
        response = self.request(
            "post",
            "/admin/buildings",
            data={"name": "North Hall"},
        )
        self.assertEqual(response.status_code, 400)

    def test_hra_cannot_manage_another_building(self):
        first = self.add_building("First Hall")
        second = self.add_building("Second Hall")
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=first,
        )
        ra_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="RA",
            building_id=second,
        )
        with app.app_context():
            conn = db()
            cur = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by) VALUES(?,?,?,?,?)",
                ("Other Hall Draft", second, "2026-09-01", "2026-09-02", hra_id),
            )
            session_id = cur.lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, ra_id),
            )
            conn.commit()
        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            f"/sessions/{session_id}/assign",
            data={"csrf": csrf, "user_id": ra_id, "duty_date": "2026-09-01"},
        )
        self.assertEqual(response.status_code, 403)

    def test_ical_export_cannot_inject_new_properties(self):
        building_id = self.add_building("Hall\r\nX-EVIL: yes")
        user_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="RA",
            building_id=building_id,
        )
        with app.app_context():
            conn = db()
            cur = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by) VALUES(?,?,?,?,?)",
                ("Draft\r\nBEGIN:VALARM", building_id, "2026-09-01", "2026-09-01", user_id),
            )
            session_id = cur.lastrowid
            assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, user_id, "2026-09-01", user_id),
            )
            conn.commit()
            assignment_id = assignment.lastrowid
        self.login_as(user_id)
        response = self.request("get", f"/calendar/{assignment_id}.ics")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("\r\nX-EVIL: yes", body)
        self.assertNotIn("\r\nBEGIN:VALARM", body)
        self.assertIn("\\nX-EVIL: yes", body)
        self.assertIn("\\nBEGIN:VALARM", body)

    def test_google_callback_creates_user_from_verified_profile(self):
        info = {
            "sub": "google-123",
            "email": "new.user@g.rwu.edu",
            "email_verified": True,
            "hd": "g.rwu.edu",
            "name": "New User",
        }
        with patch.object(oauth.google, "authorize_access_token", return_value={"userinfo": info}):
            response = self.request("get", "/auth/callback")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))
        with app.app_context():
            user = db().execute(
                "SELECT * FROM users WHERE google_sub=?",
                ("google-123",),
            ).fetchone()
            self.assertEqual(user["email"], "new.user@g.rwu.edu")
            self.assertEqual(user["name"], "New User")

    def test_google_callback_rejects_non_rwu_domain(self):
        info = {
            "sub": "google-123",
            "email": "person@gmail.com",
            "email_verified": True,
            "hd": "gmail.com",
            "name": "Person",
        }
        with patch.object(oauth.google, "authorize_access_token", return_value={"userinfo": info}):
            response = self.request("get", "/auth/callback")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            count = db().execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            self.assertEqual(count, 0)

    def test_new_google_subject_cannot_inherit_existing_email_role(self):
        existing_id = self.add_user(
            sub="old-google-sub",
            email="admin@rwu.edu",
            name="Existing Admin",
            role="ADMIN",
        )
        info = {
            "sub": "new-google-sub",
            "email": "admin@rwu.edu",
            "email_verified": True,
            "hd": "rwu.edu",
            "name": "Different Person",
        }
        with patch.object(oauth.google, "authorize_access_token", return_value={"userinfo": info}):
            response = self.request("get", "/auth/callback")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            users = db().execute("SELECT * FROM users").fetchall()
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0]["id"], existing_id)
            self.assertEqual(users[0]["google_sub"], "old-google-sub")

    def test_templates_do_not_disable_autoescaping_or_use_safe_filter(self):
        template_root = Path(__file__).resolve().parents[1] / "templates"
        for path in template_root.glob("*.html"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("|safe", text)
                self.assertNotIn("autoescape false", text.lower())
                for form in re.findall(r"<form\b[^>]*method=\"post\".*?</form>", text, re.S | re.I):
                    self.assertIn('name="csrf"', form)

    def test_python_does_not_interpolate_sql_execute_calls(self):
        root = Path(__file__).resolve().parents[1]
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIsNone(re.search(r"\.execute\(\s*f[\"']", text))
                self.assertIsNone(re.search(r"\.execute\(\s*[^\n]*\.format\(", text))

    def test_ui_has_navigation_logout_and_progressive_enhancement_hooks(self):
        building_id = self.add_building("North Hall")
        admin_id = self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
            building_id=building_id,
        )
        self.login_as(admin_id)
        response = self.request("get", "/dashboard")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/logout"', page)
        self.assertIn('name="csrf"', page)
        self.assertIn("data-session-form", page)
        self.assertIn("data-building-picker", page)
        self.assertIn("/static/app.js", page)

    def test_ics_escape_handles_all_newline_forms(self):
        escaped = ics_escape("one\r\ntwo\rthree\nfour")
        self.assertEqual(escaped, "one\\ntwo\\nthree\\nfour")


if __name__ == "__main__":
    unittest.main()
