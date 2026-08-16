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
    str(Path(tempfile.gettempdir()) / "ra-draft-session-pause-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db, next_picker  # noqa: E402


class SessionPauseTestCase(unittest.TestCase):
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
                "INSERT INTO buildings(name) VALUES('Maple')"
            ).lastrowid
            self.hra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("hra", "hra@rwu.edu", "Hall HRA", "HRA", building_id),
            ).lastrowid
            self.ra_one = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("ra-one", "one@g.rwu.edu", "Alex", "RA", building_id),
            ).lastrowid
            self.ra_two = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("ra-two", "two@g.rwu.edu", "Blair", "RA", building_id),
            ).lastrowid
            self.session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,1,'CHRONOLOGICAL',1,?)",
                ("Pause test", building_id, "2026-09-01", "2026-09-02", self.hra_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (self.session_id, self.ra_one),
            )
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,2)",
                (self.session_id, self.ra_two),
            )
            conn.commit()

    def login_as(self, user_id, csrf="pause-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(
            path,
            base_url="https://ci.local",
            **kwargs,
        )

    def test_pause_freezes_current_turn_until_resumed(self):
        csrf = self.login_as(self.hra_id)
        response = self.request(
            "post",
            f"/sessions/{self.session_id}/picking",
            data={"csrf": csrf, "paused": "1"},
        )
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            row = db().execute(
                "SELECT picking_paused,current_position FROM draft_sessions WHERE id=?",
                (self.session_id,),
            ).fetchone()
            self.assertEqual(row["picking_paused"], 1)
            self.assertEqual(row["current_position"], 1)
            self.assertEqual(next_picker(self.session_id)["id"], self.ra_one)

        ra_csrf = self.login_as(self.ra_one)
        self.request(
            "post",
            f"/sessions/{self.session_id}/choose",
            data={"csrf": ra_csrf, "duty_date": "2026-09-01"},
        )
        with app.app_context():
            self.assertEqual(
                db().execute(
                    "SELECT COUNT(*) n FROM assignments WHERE session_id=?",
                    (self.session_id,),
                ).fetchone()["n"],
                0,
            )
            self.assertEqual(
                db().execute(
                    "SELECT current_position FROM draft_sessions WHERE id=?",
                    (self.session_id,),
                ).fetchone()["current_position"],
                1,
            )

        csrf = self.login_as(self.hra_id)
        self.request(
            "post",
            f"/sessions/{self.session_id}/skip/{self.ra_one}",
            data={"csrf": csrf},
        )
        self.request(
            "post",
            f"/sessions/{self.session_id}/assign",
            data={
                "csrf": csrf,
                "user_id": str(self.ra_one),
                "duty_date": "2026-09-01",
            },
        )
        page = self.request("get", f"/sessions/{self.session_id}").get_data(as_text=True)
        self.assertIn("Resume picking", page)
        self.assertIn("Current turn · paused", page)
        self.assertNotIn(">Pause</button>", page)

        self.request(
            "post",
            f"/sessions/{self.session_id}/picking",
            data={"csrf": csrf, "paused": "0"},
        )
        ra_csrf = self.login_as(self.ra_one)
        self.request(
            "post",
            f"/sessions/{self.session_id}/choose",
            data={"csrf": ra_csrf, "duty_date": "2026-09-01"},
        )

        with app.app_context():
            row = db().execute(
                "SELECT picking_paused,current_position FROM draft_sessions WHERE id=?",
                (self.session_id,),
            ).fetchone()
            self.assertEqual(row["picking_paused"], 0)
            self.assertEqual(row["current_position"], 2)
            self.assertEqual(
                db().execute(
                    "SELECT COUNT(*) n FROM assignments WHERE session_id=?",
                    (self.session_id,),
                ).fetchone()["n"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
