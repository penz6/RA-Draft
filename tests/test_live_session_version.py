import os
import re
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
    str(Path(tempfile.gettempdir()) / "ra-draft-live-session-version-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402


class LiveSessionVersionTestCase(unittest.TestCase):
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
                "INSERT INTO buildings(name) VALUES('Maple Hall')"
            ).lastrowid
            admin_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("admin-sub", "admin@rwu.edu", "Penn Potter", "ADMIN", building_id),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,?,?,1,?)",
                (
                    "test 2",
                    building_id,
                    "2026-08-20",
                    "2026-08-26",
                    1,
                    "WEEKDAYS_FIRST",
                    admin_id,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, admin_id),
            )
            conn.commit()
        self.admin_id = admin_id
        self.session_id = session_id
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = admin_id

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def test_rendered_session_version_matches_live_state_for_same_viewer(self):
        page = self.request("get", f"/sessions/{self.session_id}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        match = re.search(r'data-live-version="([0-9a-f]{64})"', html)
        self.assertIsNotNone(match)

        live_state = self.request(
            "get", f"/live-state?session_id={self.session_id}"
        )
        self.assertEqual(live_state.status_code, 200)
        self.assertEqual(match.group(1), live_state.get_json()["version"])


if __name__ == "__main__":
    unittest.main()
