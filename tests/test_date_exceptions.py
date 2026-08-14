import os
import tempfile
import unittest
from pathlib import Path

TEST_DIR = tempfile.mkdtemp(prefix="ra-draft-date-exception-tests-")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(TEST_DIR) / "test.db"))

import portal_app  # noqa: E402,F401
from core import (  # noqa: E402
    DATE_KIND_NO_DUTY,
    DATE_KIND_WEEKDAY,
    app,
    db,
    effective_capacity,
    effective_date_kind,
    next_picker,
    selectable_dates,
    session_complete,
    session_row,
    total_slots,
)
from date_exceptions import DATE_KIND_WEEKEND  # noqa: E402


class DateExceptionTestCase(unittest.TestCase):
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
        return getattr(self.client, method)(
            path,
            base_url="https://ci.local",
            **kwargs,
        )

    def login_as(self, user_id, csrf="date-exception-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def setup_session(self, *, start="2026-08-14", end="2026-08-16", capacity=1):
        with app.app_context():
            conn = db()
            building_id = conn.execute(
                "INSERT INTO buildings(name) VALUES(?)",
                ("Maple",),
            ).lastrowid
            hra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("hra", "hra@rwu.edu", "Hall HRA", "HRA", building_id),
            ).lastrowid
            ra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("ra", "ra@g.rwu.edu", "Alex", "RA", building_id),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,?,'WEEKDAYS_FIRST',1,?)",
                ("Edge Cases", building_id, start, end, capacity, hra_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, ra_id),
            )
            conn.commit()
            return building_id, hra_id, ra_id, session_id

    def set_kind(self, session_id, actor_id, duty_date, date_kind):
        csrf = self.login_as(actor_id)
        return self.request(
            "post",
            f"/sessions/{session_id}/date-kind",
            data={
                "csrf": csrf,
                "duty_date": duty_date,
                "date_kind": date_kind,
            },
        )

    def test_hra_can_treat_a_weekend_as_a_weekday(self):
        _building_id, hra_id, ra_id, session_id = self.setup_session()
        self.set_kind(session_id, hra_id, "2026-08-14", "NO_DUTY")
        self.set_kind(session_id, hra_id, "2026-08-15", "WEEKDAY")

        with app.app_context():
            row = session_row(session_id)
            self.assertEqual(
                effective_date_kind(row, "2026-08-15"),
                DATE_KIND_WEEKDAY,
            )
            self.assertEqual(selectable_dates(row, ra_id), ["2026-08-15"])

    def test_no_duty_date_is_not_a_slot_or_turn(self):
        _building_id, hra_id, ra_id, session_id = self.setup_session(
            start="2026-08-14",
            end="2026-08-14",
            capacity=1,
        )
        self.set_kind(session_id, hra_id, "2026-08-14", "NO_DUTY")

        with app.app_context():
            row = session_row(session_id)
            self.assertEqual(
                effective_date_kind(row, "2026-08-14"),
                DATE_KIND_NO_DUTY,
            )
            self.assertEqual(effective_capacity(row, "2026-08-14"), 0)
            self.assertEqual(total_slots(row), 0)
            self.assertTrue(session_complete(row))
            self.assertIsNone(next_picker(session_id))

        self.login_as(ra_id)
        page = self.request("get", f"/sessions/{session_id}").get_data(as_text=True)
        self.assertIn("No one needed", page)
        self.assertIn("is-no-duty", page)
        self.assertIn("No staffing required", page)

    def test_assigned_date_cannot_be_marked_no_duty(self):
        _building_id, hra_id, ra_id, session_id = self.setup_session()
        with app.app_context():
            conn = db()
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
                "VALUES(?,?,?,?)",
                (session_id, ra_id, "2026-08-14", hra_id),
            )
            conn.commit()

        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            f"/sessions/{session_id}/date-kind",
            data={
                "csrf": csrf,
                "duty_date": "2026-08-14",
                "date_kind": "NO_DUTY",
            },
            follow_redirects=True,
        )
        self.assertIn("Remove the 1 existing assignment", response.get_data(as_text=True))
        with app.app_context():
            override = db().execute(
                "SELECT date_kind FROM session_date_overrides "
                "WHERE session_id=? AND duty_date=?",
                (session_id, "2026-08-14"),
            ).fetchone()
            assignment_count = db().execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE session_id=?",
                (session_id,),
            ).fetchone()["n"]
            self.assertIsNone(override)
            self.assertEqual(assignment_count, 1)

    def test_admin_has_the_same_date_treatment_control_for_any_building(self):
        _building_id, _hra_id, _ra_id, session_id = self.setup_session()
        with app.app_context():
            admin_id = db().execute(
                "INSERT INTO users(google_sub,email,name,role) VALUES(?,?,?,?)",
                ("admin", "admin@rwu.edu", "Admin", "ADMIN"),
            ).lastrowid
            db().commit()

        response = self.set_kind(session_id, admin_id, "2026-08-15", "WEEKDAY")
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            override = db().execute(
                "SELECT date_kind FROM session_date_overrides "
                "WHERE session_id=? AND duty_date=?",
                (session_id, "2026-08-15"),
            ).fetchone()
            self.assertEqual(override["date_kind"], DATE_KIND_WEEKDAY)

    def test_calendar_default_removes_manual_classification(self):
        _building_id, hra_id, _ra_id, session_id = self.setup_session()
        self.set_kind(session_id, hra_id, "2026-08-15", "WEEKDAY")
        self.set_kind(session_id, hra_id, "2026-08-15", "AUTO")

        with app.app_context():
            row = session_row(session_id)
            override = db().execute(
                "SELECT date_kind FROM session_date_overrides "
                "WHERE session_id=? AND duty_date=?",
                (session_id, "2026-08-15"),
            ).fetchone()
            self.assertIsNone(override)
            self.assertEqual(
                effective_date_kind(row, "2026-08-15"),
                DATE_KIND_WEEKEND,
            )


if __name__ == "__main__":
    unittest.main()
