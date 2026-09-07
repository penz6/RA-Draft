import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault(
    "DATABASE_PATH",
    str(Path(tempfile.gettempdir()) / "ra-draft-pfp-duty-page-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402


class PfpDutyPageTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
        with app.app_context():
            conn = db()
            for table in (
                "audit_log",
                "session_date_overrides",
                "session_date_capacities",
                "session_deferrals",
                "assignments",
                "session_order",
                "draft_sessions",
                "users",
                "buildings",
            ):
                conn.execute(f"DELETE FROM {table}")
            building_id = conn.execute(
                "INSERT INTO buildings(name) VALUES('Bayside Hall')"
            ).lastrowid
            admin_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,picture_url) "
                "VALUES(?,?,?,?,?,?)",
                ("admin-sub", "admin@rwu.edu", "Penn Potter", "ADMIN", building_id, "https://lh3.googleusercontent.com/admin-pic"),
            ).lastrowid
            ra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,picture_url) "
                "VALUES(?,?,?,?,?,?)",
                ("ra-sub", "ra@rwu.edu", "RA Picker", "RA", building_id, "https://lh3.googleusercontent.com/ra-pic"),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,?,?,1,?)",
                (
                    "Fall 2026 Duty",
                    building_id,
                    "2026-09-01",
                    "2026-09-05",
                    1,
                    "CHRONOLOGICAL",
                    admin_id,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, ra_id),
            )
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,2)",
                (session_id, admin_id),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,'2026-09-01',?)",
                (session_id, ra_id, admin_id),
            )
            conn.commit()
            self.session_id = session_id
            self.admin_id = admin_id
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = self.admin_id

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def test_pfp_displayed_on_duty_picking_page(self):
        resp = self.request("get", f"/sessions/{self.session_id}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # Check turn order and current picker contain the profile pictures
        self.assertIn("https://lh3.googleusercontent.com/ra-pic", html)
        self.assertIn("https://lh3.googleusercontent.com/admin-pic", html)
        # Check calendar assignment has assignee pfp
        self.assertIn("calendar-assignee-pfp", html)
        # Check table has inline cell with user pfp
        self.assertIn("user-inline-cell", html)


if __name__ == "__main__":
    unittest.main()
