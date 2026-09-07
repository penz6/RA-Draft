import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault(
    "DATABASE_PATH",
    str(Path(tempfile.gettempdir()) / "ra-draft-security-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db, ics_escape, oauth  # noqa: E402


class SecurityTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
        with app.app_context():
            conn = db()
            for table in (
                "audit_log",
                "assignments",
                "session_order",
                "draft_sessions",
                "users",
                "buildings",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def add_building(self, name):
        with app.app_context():
            cur = db().execute("INSERT INTO buildings(name) VALUES(?)", (name,))
            db().commit()
            return cur.lastrowid

    def add_user(self, sub, email, name, role="RA", building_id=None):
        with app.app_context():
            cur = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                (sub, email, name, role, building_id),
            )
            db().commit()
            return cur.lastrowid

    def login_as(self, user_id):
        with self.client.session_transaction() as sess:
            sess["uid"] = user_id

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def test_session_cookie_security_flags(self):
        response = self.request("get", "/")
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Secure", cookie)

    def test_security_headers_present_and_strict(self):
        response = self.request("get", "/")
        headers = response.headers
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
        self.assertIn("max-age=31536000", headers.get("Strict-Transport-Security", ""))
        self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))

    def test_csrf_protection_blocks_state_modifications(self):
        building_id = self.add_building("North Hall")
        admin_id = self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
            building_id=building_id,
        )
        self.login_as(admin_id)

        response = self.request("post", "/admin/buildings", data={"name": "Attacked Hall"})
        self.assertEqual(response.status_code, 400)

        with self.client.session_transaction() as sess:
            csrf = sess["csrf_token"]
        response = self.request(
            "post",
            "/admin/buildings",
            data={"name": "Attacked Hall", "csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)

    def test_unauthenticated_protected_route_redirects_to_login(self):
        response = self.request("get", "/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/login"))

    def test_role_enforcement_prevents_ra_access_to_admin_panel(self):
        building_id = self.add_building("North Hall")
        ra_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="RA User",
            role="RA",
            building_id=building_id,
        )
        self.login_as(ra_id)
        response = self.request("get", "/admin")
        self.assertEqual(response.status_code, 403)

    def test_hra_cannot_create_session_in_foreign_building(self):
        building_a = self.add_building("Hall A")
        building_b = self.add_building("Hall B")
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@g.rwu.edu",
            name="HRA User",
            role="HRA",
            building_id=building_a,
        )
        ra_b_id = self.add_user(
            sub="ra-b-sub",
            email="ra.b@g.rwu.edu",
            name="RA Hall B",
            role="RA",
            building_id=building_b,
        )
        self.login_as(hra_id)
        with self.client.session_transaction() as sess:
            csrf = sess["csrf_token"]

        payload = {
            "csrf": csrf,
            "name": "Intruder Session",
            "building_id": str(building_b),
            "start_date": "2026-09-01",
            "end_date": "2026-09-03",
            "capacity": "1",
            "date_order": "CHRONOLOGICAL",
            "participant_ids": [str(ra_b_id)],
            f"order_{ra_b_id}": "1",
        }
        response = self.request("post", "/sessions/new", data=payload)
        self.assertEqual(response.status_code, 403)
        with app.app_context():
            count = db().execute("SELECT COUNT(*) n FROM draft_sessions").fetchone()["n"]
            self.assertEqual(count, 0)

    def test_session_actions_enforce_single_active_picker(self):
        building_id = self.add_building("Cedar Hall")
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@g.rwu.edu",
            name="HRA Cedar",
            role="HRA",
            building_id=building_id,
        )
        ra_1 = self.add_user(
            sub="ra1-sub",
            email="ra1@g.rwu.edu",
            name="RA One",
            role="RA",
            building_id=building_id,
        )
        ra_2 = self.add_user(
            sub="ra2-sub",
            email="ra2@g.rwu.edu",
            name="RA Two",
            role="RA",
            building_id=building_id,
        )

        with app.app_context():
            conn = db()
            cur = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,capacity,date_order,created_by,current_position) "
                "VALUES('Fall Picks',?,?,?,1,'CHRONOLOGICAL',?,1)",
                (building_id, "2026-09-01", "2026-09-02", hra_id),
            )
            session_id = cur.lastrowid
            conn.execute("INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)", (session_id, ra_1))
            conn.execute("INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,2)", (session_id, ra_2))
            conn.commit()

        self.login_as(ra_2)
        with self.client.session_transaction() as sess:
            csrf = sess["csrf_token"]
        response = self.request(
            "post",
            f"/sessions/{session_id}/pick",
            data={"duty_date": "2026-09-01", "csrf": csrf},
        )
        self.assertEqual(response.status_code, 403)

        self.login_as(ra_1)
        with self.client.session_transaction() as sess:
            csrf = sess["csrf_token"]
        response = self.request(
            "post",
            f"/sessions/{session_id}/pick",
            data={"duty_date": "2026-09-01", "csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            assignment = db().execute("SELECT * FROM assignments WHERE session_id=?", (session_id,)).fetchone()
            self.assertIsNotNone(assignment)
            self.assertEqual(assignment["user_id"], ra_1)

    def test_calendar_export_escapes_crlf_injection(self):
        building_id = self.add_building("Willow Hall")
        user_id = self.add_user(
            sub="evil-sub",
            email="evil@g.rwu.edu",
            name="Evil RA\r\nX-EVIL: yes\r\nBEGIN:VALARM",
            role="RA",
            building_id=building_id,
        )
        with app.app_context():
            conn = db()
            sess = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,capacity,date_order,created_by,current_position) "
                "VALUES('Injected Session',?,?,?,1,'CHRONOLOGICAL',?,1)",
                (building_id, "2026-09-01", "2026-09-02", user_id),
            )
            session_id = sess.lastrowid
            assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, user_id, "2026-09-01", user_id),
            )
            conn.commit()
            assignment_id = assignment.lastrowid
        self.login_as(user_id)
        response = self.request("get", f"/calendar/session/{session_id}.ics")
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
            "picture": "https://lh3.googleusercontent.com/a/avatar-123",
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
            self.assertEqual(user["picture_url"], "https://lh3.googleusercontent.com/a/avatar-123")

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

    def test_content_security_policy_allows_google_profile_pictures(self):
        response = self.request("get", "/")
        csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("googleusercontent.com", csp)
        self.assertIn("img-src", csp)

    def test_profile_picture_rendered_in_base_navigation(self):
        building_id = self.add_building("Maple Hall")
        user_id = self.add_user(
            sub="pfp-user",
            email="pfp@g.rwu.edu",
            name="PFP User",
            building_id=building_id,
        )
        with app.app_context():
            db().execute(
                "UPDATE users SET picture_url=? WHERE id=?",
                ("https://lh3.googleusercontent.com/a/pfp-avatar", user_id),
            )
            db().commit()
        self.login_as(user_id)
        response = self.request("get", "/dashboard")
        html = response.get_data(as_text=True)
        self.assertIn('src="https://lh3.googleusercontent.com/a/pfp-avatar"', html)
        self.assertIn('referrerpolicy="no-referrer"', html)
        self.assertIn("user-pfp", html)

    def test_admin_impersonation_option_rendered_above_save_changes(self):
        building_id = self.add_building("Maple Hall")
        admin_id = self.add_user(
            sub="admin-user-sub",
            email="admin@rwu.edu",
            name="Admin User",
            role="ADMIN",
            building_id=building_id,
        )
        ra_id = self.add_user(
            sub="ra-target",
            email="target.ra@g.rwu.edu",
            name="Target RA",
            building_id=building_id,
        )
        self.login_as(admin_id)
        response = self.request("get", "/admin")
        html = response.get_data(as_text=True)

        # Confirm impersonation form is present for Target RA
        impersonate_str = f'/admin/impersonate/{ra_id}'
        save_changes_str = "Save changes"
        self.assertIn(impersonate_str, html)
        self.assertIn(save_changes_str, html)

        # Confirm impersonation appears before (above) Save changes for this user card
        impersonate_pos = html.find(impersonate_str)
        save_changes_pos = html.find(save_changes_str, impersonate_pos)
        self.assertNotEqual(impersonate_pos, -1)
        self.assertNotEqual(save_changes_pos, -1)
        self.assertLess(impersonate_pos, save_changes_pos)


if __name__ == "__main__":
    unittest.main()
