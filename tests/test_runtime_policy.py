import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from flask import session
from werkzeug.exceptions import Forbidden

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault(
    "DATABASE_PATH",
    str(Path(tempfile.gettempdir()) / "ra-draft-runtime-policy-tests.db"),
)

import portal_app  # noqa: E402,F401
import core  # noqa: E402
import runtime_policy  # noqa: E402
from core import app, db  # noqa: E402


class RuntimePolicyTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        with app.app_context():
            conn = db()
            for table in (
                "audit_log",
                "duty_swap_requests",
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

    def add_building(self, name="Maple"):
        with app.app_context():
            building_id = db().execute(
                "INSERT INTO buildings(name) VALUES(?)", (name,)
            ).lastrowid
            db().commit()
            return building_id

    def add_user(self, *, sub, email, name, role="RA", building_id=None):
        with app.app_context():
            user_id = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                (sub, email, name, role, building_id),
            ).lastrowid
            db().commit()
            return user_id

    def test_admin_inherits_hra_decorator_permission(self):
        admin_id = self.add_user(
            sub="admin",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        ra_id = self.add_user(
            sub="ra",
            email="ra@g.rwu.edu",
            name="RA",
        )
        protected = core.roles("HRA")(lambda: "ok")

        with app.test_request_context("/", base_url="https://ci.local"):
            session["uid"] = admin_id
            self.assertEqual(protected(), "ok")

        with app.test_request_context("/", base_url="https://ci.local"):
            session["uid"] = ra_id
            with self.assertRaises(Forbidden):
                protected()

    def test_upcoming_shifts_use_eastern_date_and_session_times(self):
        self.assertEqual(str(runtime_policy.SCHOOL_TIMEZONE), "America/New_York")
        building_id = self.add_building()
        user_id = self.add_user(
            sub="ra",
            email="ra@g.rwu.edu",
            name="RA",
            building_id=building_id,
        )
        with app.app_context():
            conn = db()
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,shift_start,shift_end,created_by,status"
                ") VALUES(?,?,?,?,?,?,?,'CLOSED')",
                (
                    "Duty",
                    building_id,
                    "2026-08-29",
                    "2026-08-31",
                    "20:30",
                    "06:15",
                    user_id,
                ),
            ).lastrowid
            for duty_date in ("2026-08-29", "2026-08-30", "2026-08-31"):
                conn.execute(
                    "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                    (session_id, user_id, duty_date, user_id),
                )
            conn.commit()

            with patch.object(runtime_policy, "school_today", return_value=date(2026, 8, 30)):
                shifts = core.user_upcoming_shifts(user_id)

        self.assertEqual(
            [shift["duty_date"] for shift in shifts],
            ["2026-08-30", "2026-08-31"],
        )
        self.assertTrue(all(shift["shift_start"] == "20:30" for shift in shifts))
        self.assertTrue(all(shift["shift_end"] == "06:15" for shift in shifts))

    def test_manual_swap_recheck_rejects_reopened_session_under_write_lock(self):
        building_id = self.add_building()
        admin_id = self.add_user(
            sub="admin",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        with app.app_context():
            session_id = db().execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES(?,?,?,?,?,'OPEN')",
                ("Duty", building_id, "2026-09-01", "2026-09-02", admin_id),
            ).lastrowid
            db().commit()

        path = f"/swaps/session/{session_id}/manager-swap"
        with app.test_request_context(path, method="POST", base_url="https://ci.local"):
            conn = db()
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT * FROM users WHERE id=?", (admin_id,)).fetchone()
            row = core.session_row(session_id)
            self.assertEqual(request_endpoint(), "manager_manual_swap")
            self.assertFalse(core.can_manage(user, row))
            conn.rollback()


def request_endpoint():
    from flask import request

    return request.endpoint


if __name__ == "__main__":
    unittest.main()
