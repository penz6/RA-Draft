import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.mkdtemp()) / "prostaff.db"))

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402
from prostaff import _duty_display_date  # noqa: E402
from staff_event_schedule import SCHOOL_TIMEZONE  # noqa: E402


class ProstaffTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
        with app.app_context():
            conn = db()
            for table in ("audit_log", "assignments", "session_order", "draft_sessions", "users", "buildings"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def add_admin(self):
        with app.app_context():
            uid = db().execute("INSERT INTO users(google_sub,email,name,role) VALUES('admin','admin@rwu.edu','Admin','ADMIN')").lastrowid
            db().commit()
            return uid

    def login_as(self, uid, csrf="csrf-token"):
        with self.client.session_transaction() as sess:
            sess["uid"], sess["csrf"] = uid, csrf

    def test_admin_provisions_and_first_login_requires_new_password(self):
        with app.app_context():
            building = db().execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            db().commit()
        admin = self.add_admin()
        self.login_as(admin)
        response = self.request("post", "/admin/prostaff", data={
            "csrf": "csrf-token", "name": "Pat Staff", "email": "pat@example.edu",
            "temporary_password": "temporary-pass-123",
            "building_id": str(building),
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            user = db().execute("SELECT * FROM users WHERE email='pat@example.edu'").fetchone()
            self.assertEqual(user["is_prostaff"], 1)
            self.assertEqual(user["building_id"], building)
            self.assertEqual(user["password_must_change"], 1)

        response = self.request("post", f"/admin/impersonate/{user['id']}",
                                data={"csrf": "csrf-token"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["uid"], user["id"])
            self.assertEqual(sess["impersonator_uid"], admin)
        # A pending first-login password does not prevent the administrator
        # from inspecting the dashboard, but password changes remain blocked.
        self.assertEqual(self.request("get", "/prostaff").status_code, 200)
        self.assertEqual(self.request("post", "/prostaff/set-password", data={
            "csrf": "csrf-token", "password": "admin-must-not-set-this",
            "password_confirm": "admin-must-not-set-this",
        }).status_code, 403)
        self.assertEqual(self.request("post", "/admin/stop-impersonation",
                                      data={"csrf": "csrf-token"}).status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["uid"], admin)
            self.assertNotIn("impersonator_uid", sess)

        self.request("post", "/logout", data={"csrf": "csrf-token"})
        login_page = self.request("get", "/prostaff/login")
        with self.client.session_transaction() as sess:
            csrf = sess["csrf"]
        response = self.request("post", "/prostaff/login", data={
            "csrf": csrf, "email": "pat@example.edu", "password": "temporary-pass-123",
        })
        self.assertTrue(response.location.endswith("/prostaff/set-password"))
        with self.client.session_transaction() as sess:
            csrf = sess["csrf"]
        response = self.request("post", "/prostaff/set-password", data={
            "csrf": csrf, "password": "a-new-secure-password", "password_confirm": "a-new-secure-password",
        })
        self.assertTrue(response.location.endswith("/prostaff"))

    def test_prostaff_is_redirected_away_from_ra_routes_and_can_search_schedule(self):
        with app.app_context():
            conn = db()
            building = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            admin = conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('admin','admin@rwu.edu','Admin','ADMIN')").lastrowid
            ra = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra','ra@rwu.edu','Alex RA','RA',?)", (building,)).lastrowid
            staff = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) VALUES('ps','ps@example.edu','Pro Staff','RA',?,1,0)", (building,)).lastrowid
            draft = conn.execute("INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) VALUES('Fall',?,?,?,?, 'CLOSED')", (building, date.today().isoformat(), date.today().isoformat(), admin)).lastrowid
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft, ra, date.today().isoformat(), admin))
            conn.commit()
        self.login_as(staff)
        self.assertTrue(self.request("get", "/dashboard").location.endswith("/prostaff"))
        page = self.request("get", "/prostaff?q=Alex").get_data(as_text=True)
        self.assertIn("Alex RA", page)
        self.assertIn("Maple", page)
        self.assertNotIn("Duty Swaps", page)

        calendar = self.request("get", f"/prostaff/schedule?month={date.today():%Y-%m}")
        self.assertEqual(calendar.status_code, 200)
        self.assertIn(b"Duty schedule for", calendar.data)
        self.assertIn(b"Alex RA", calendar.data)
        one_on_ones = self.request("get", "/prostaff/one-on-ones")
        self.assertEqual(one_on_ones.status_code, 200)
        self.assertIn(b"Set a one-on-one time", one_on_ones.data)

    def test_repeated_bad_passwords_temporarily_lock_local_login(self):
        with app.app_context():
            building = db().execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            user = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,"
                "password_hash,password_must_change) VALUES('ps','ps@example.edu','Pro Staff',"
                "'RA',?,1,?,0)",
                (building, generate_password_hash("correct-password-123")),
            ).lastrowid
            db().commit()

        self.request("get", "/prostaff/login")
        with self.client.session_transaction() as sess:
            csrf = sess["csrf"]
        for _ in range(5):
            response = self.request("post", "/prostaff/login", data={
                "csrf": csrf, "email": "ps@example.edu", "password": "wrong-password",
            })
            self.assertEqual(response.status_code, 401)
        response = self.request("post", "/prostaff/login", data={
            "csrf": csrf, "email": "ps@example.edu", "password": "correct-password-123",
        })
        self.assertEqual(response.status_code, 401)
        with app.app_context():
            locked = db().execute(
                "SELECT prostaff_locked_until FROM users WHERE id=?", (user,)
            ).fetchone()
            self.assertIsNotNone(locked["prostaff_locked_until"])

    def test_admin_can_reset_prostaff_password(self):
        with app.app_context():
            building = db().execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            staff = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,"
                "password_hash,password_must_change,prostaff_failed_logins,prostaff_locked_until) "
                "VALUES('reset-ps','reset@example.edu','Reset Staff','RA',?,1,?,0,4,?)",
                (building, generate_password_hash("old-password-123"),
                 "2099-01-01T00:00:00+00:00"),
            ).lastrowid
            db().commit()
        self.login_as(self.add_admin())
        response = self.request(
            "post", f"/admin/prostaff/{staff}/reset-password",
            data={"csrf": "csrf-token", "temporary_password": "new-temporary-456"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            updated = db().execute("SELECT * FROM users WHERE id=?", (staff,)).fetchone()
            self.assertTrue(check_password_hash(updated["password_hash"], "new-temporary-456"))
            self.assertEqual(updated["password_must_change"], 1)
            self.assertEqual(updated["prostaff_failed_logins"], 0)
            self.assertIsNone(updated["prostaff_locked_until"])

    def test_duty_roster_uses_previous_date_until_eight_am(self):
        self.assertEqual(
            _duty_display_date(datetime(2026, 10, 15, 7, 59, tzinfo=SCHOOL_TIMEZONE)),
            date(2026, 10, 14),
        )
        self.assertEqual(
            _duty_display_date(datetime(2026, 10, 15, 8, 0, tzinfo=SCHOOL_TIMEZONE)),
            date(2026, 10, 15),
        )

    def test_early_morning_dashboard_labels_and_shows_previous_duty_night(self):
        with app.app_context():
            conn = db()
            building = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            admin = conn.execute(
                "INSERT INTO users(google_sub,email,name,role) VALUES('night-admin','night-admin@rwu.edu','Admin','ADMIN')"
            ).lastrowid
            prior_ra = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES('prior-ra','prior@rwu.edu','Prior Night RA','RA',?)",
                (building,),
            ).lastrowid
            current_ra = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES('current-ra','current@rwu.edu','Current Night RA','RA',?)",
                (building,),
            ).lastrowid
            draft = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('October',?,'2026-10-01','2026-10-31',?,'CLOSED')",
                (building, admin),
            ).lastrowid
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (draft, prior_ra, "2026-10-14", admin),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (draft, current_ra, "2026-10-15", admin),
            )
            conn.commit()
        self.login_as(admin)

        class EarlyMorning(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 15, 7, 30, tzinfo=tz)

        with patch("prostaff.datetime", EarlyMorning):
            page = self.request("get", "/prostaff")
        self.assertIn(b"Prior Night RA", page.data)
        self.assertNotIn(b"Current Night RA", page.data)
        self.assertIn(b"Wed, Oct 14", page.data)
        self.assertIn(b"previous duty night until 8:00 AM", page.data)


if __name__ == "__main__":
    unittest.main()
