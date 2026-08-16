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
    str(Path(tempfile.gettempdir()) / "ra-draft-review-fixes-tests.db"),
)

import portal_app  # noqa: E402,F401
from calendar_routes import fold_ical_line  # noqa: E402
from core import app, db, session_row  # noqa: E402
from live_updates import session_state_version  # noqa: E402


class ReviewFixesTestCase(unittest.TestCase):
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
            conn.commit()

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def add_building(self, name="Maple Hall"):
        with app.app_context():
            building_id = db().execute(
                "INSERT INTO buildings(name) VALUES(?)", (name,)
            ).lastrowid
            db().commit()
            return building_id

    def add_user(self, *, sub, email, name, role="RA", building_id=None):
        with app.app_context():
            user_id = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                (sub, email, name, role, building_id),
            ).lastrowid
            db().commit()
            return user_id

    def login_as(self, user_id, csrf="review-fixes-csrf", show_help=False):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
            if show_help:
                flask_session["show_role_help"] = True
        return csrf

    def create_session(
        self,
        building_id,
        creator_id,
        participant_ids,
        *,
        capacity=1,
        status="OPEN",
    ):
        with app.app_context():
            conn = db()
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by,status"
                ") VALUES(?,?,?,?,?,?,1,?,?)",
                (
                    "Review session",
                    building_id,
                    "2026-09-01",
                    "2026-09-02",
                    capacity,
                    "CHRONOLOGICAL",
                    creator_id,
                    status,
                ),
            ).lastrowid
            for position, user_id in enumerate(participant_ids, start=1):
                conn.execute(
                    "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,?)",
                    (session_id, user_id, position),
                )
            conn.commit()
            return session_id

    def test_role_help_only_describes_the_signed_in_role(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-help",
            email="hra.help@rwu.edu",
            name="Hall Director",
            role="HRA",
            building_id=building_id,
        )
        self.login_as(hra_id, show_help=True)
        page = self.request("get", "/dashboard").get_data(as_text=True)
        self.assertIn("Your HRA role", page)
        self.assertIn("Create and manage duty sessions for your assigned building", page)
        self.assertNotIn("Everything an RA can do", page)
        self.assertNotIn("Everything an HRA can do", page)
        self.assertNotIn("RAs cannot", page)

    def test_closed_session_blocks_manager_assignment_and_hides_turn_actions(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-closed",
            email="hra.closed@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        ra_id = self.add_user(
            sub="ra-closed",
            email="ra.closed@g.rwu.edu",
            name="RA",
            building_id=building_id,
        )
        session_id = self.create_session(
            building_id,
            hra_id,
            [ra_id],
            status="CLOSED",
        )
        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            f"/sessions/{session_id}/assign",
            data={"csrf": csrf, "user_id": ra_id, "duty_date": "2026-09-01"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            count = db().execute(
                "SELECT COUNT(*) n FROM assignments WHERE session_id=?",
                (session_id,),
            ).fetchone()["n"]
            self.assertEqual(count, 0)

        page = self.request("get", f"/sessions/{session_id}").get_data(as_text=True)
        self.assertNotIn("Pick for them", page)
        self.assertIn("This session is closed", page)
        self.assertIn("Next picker when reopened", page)

    def test_capacity_equal_to_default_removes_override(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-cap",
            email="hra.cap@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        first_id = self.add_user(
            sub="ra-cap-1",
            email="ra.cap1@g.rwu.edu",
            name="First",
            building_id=building_id,
        )
        second_id = self.add_user(
            sub="ra-cap-2",
            email="ra.cap2@g.rwu.edu",
            name="Second",
            building_id=building_id,
        )
        session_id = self.create_session(
            building_id,
            hra_id,
            [first_id, second_id],
            capacity=2,
        )
        with app.app_context():
            db().execute(
                "INSERT INTO session_date_capacities(session_id,duty_date,capacity,updated_by) "
                "VALUES(?,?,?,?)",
                (session_id, "2026-09-01", 1, hra_id),
            )
            db().commit()

        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            f"/sessions/{session_id}/date-capacity",
            data={"csrf": csrf, "duty_date": "2026-09-01", "capacity": "2"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            override = db().execute(
                "SELECT 1 FROM session_date_capacities WHERE session_id=? AND duty_date=?",
                (session_id, "2026-09-01"),
            ).fetchone()
            self.assertIsNone(override)

    def test_ical_folding_honors_utf8_octet_limit(self):
        original = "SUMMARY:" + ("Å" * 80) + " Penn"
        folded = fold_ical_line(original)
        self.assertGreater(len(folded), 1)
        for line in folded:
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        for continuation in folded[1:]:
            self.assertTrue(continuation.startswith(" "))
        unfolded = folded[0] + "".join(item[1:] for item in folded[1:])
        self.assertEqual(unfolded, original)

    def test_session_fingerprint_requires_viewer(self):
        building_id = self.add_building()
        admin_id = self.add_user(
            sub="admin-version",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
            building_id=building_id,
        )
        session_id = self.create_session(building_id, admin_id, [admin_id])
        with app.app_context():
            row = session_row(session_id)
            with self.assertRaises(TypeError):
                session_state_version(row)

    def test_live_dirty_check_waits_for_programmatic_form_changes(self):
        source = (
            Path(__file__).resolve().parents[1] / "static" / "live_stream.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'if (form) window.setTimeout(() => syncFormDirty(form), 0);',
            source,
        )

    def test_render_routes_use_explicit_read_snapshots(self):
        root = Path(__file__).resolve().parents[1]
        dashboard_source = (root / "portal_app.py").read_text(encoding="utf-8")
        session_source = (root / "session_view.py").read_text(encoding="utf-8")
        self.assertIn('conn.execute("BEGIN")', dashboard_source)
        self.assertIn('conn.execute("BEGIN")', session_source)
        self.assertIn("live_version = dashboard_state_version(user)", dashboard_source)
        self.assertIn("live_version = session_state_version(row, user)", session_source)


if __name__ == "__main__":
    unittest.main()
