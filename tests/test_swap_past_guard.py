import os
import tempfile
import unittest
from datetime import date
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
    str(Path(tempfile.gettempdir()) / "ra-draft-swap-past-tests.db"),
)

import portal_app  # noqa: E402,F401
import runtime_policy  # noqa: E402
from core import app, db  # noqa: E402


class SwapPastGuardTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
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

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(
            path,
            base_url="https://ci.local",
            **kwargs,
        )

    def login_as(self, user_id, csrf="past-swap-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def create_fixture(self):
        with app.app_context():
            conn = db()
            building_id = conn.execute(
                "INSERT INTO buildings(name) VALUES('Maple Hall')"
            ).lastrowid
            hra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('hra','hra@rwu.edu','HRA','HRA',?)",
                (building_id,),
            ).lastrowid
            requester_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('requester','requester@g.rwu.edu','Requester','RA',?)",
                (building_id,),
            ).lastrowid
            target_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('target','target@g.rwu.edu','Target','RA',?)",
                (building_id,),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,status,created_by) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "Duty",
                    building_id,
                    "2026-08-29",
                    "2026-08-31",
                    "CLOSED",
                    hra_id,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, requester_id),
            )
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,2)",
                (session_id, target_id),
            )
            past_assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, requester_id, "2026-08-29", hra_id),
            ).lastrowid
            future_assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, target_id, "2026-08-31", hra_id),
            ).lastrowid
            conn.commit()
        return {
            "building_id": building_id,
            "hra_id": hra_id,
            "requester_id": requester_id,
            "target_id": target_id,
            "session_id": session_id,
            "past_assignment": past_assignment,
            "future_assignment": future_assignment,
        }

    def test_ra_cannot_request_swap_with_past_shift(self):
        data = self.create_fixture()
        csrf = self.login_as(data["requester_id"])
        with patch.object(runtime_policy, "school_today", return_value=date(2026, 8, 30)):
            response = self.request(
                "post",
                f"/swaps/session/{data['session_id']}/request",
                data={
                    "csrf": csrf,
                    "my_assignment_ids": [str(data["past_assignment"])],
                    "target_assignment_ids": [str(data["future_assignment"])],
                },
            )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                db().execute("SELECT COUNT(*) n FROM duty_swap_requests").fetchone()["n"],
                0,
            )

    def test_manager_cannot_manually_swap_past_shift(self):
        data = self.create_fixture()
        csrf = self.login_as(data["hra_id"])
        with patch.object(runtime_policy, "school_today", return_value=date(2026, 8, 30)):
            response = self.request(
                "post",
                f"/swaps/session/{data['session_id']}/manager-swap",
                data={
                    "csrf": csrf,
                    "first_assignment_id": str(data["past_assignment"]),
                    "second_assignment_id": str(data["future_assignment"]),
                },
            )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            owners = {
                row["id"]: row["user_id"]
                for row in db().execute(
                    "SELECT id,user_id FROM assignments WHERE id IN (?,?)",
                    (data["past_assignment"], data["future_assignment"]),
                ).fetchall()
            }
            self.assertEqual(owners[data["past_assignment"]], data["requester_id"])
            self.assertEqual(owners[data["future_assignment"]], data["target_id"])

    def test_target_cannot_approve_request_after_shift_becomes_past(self):
        data = self.create_fixture()
        with app.app_context():
            conn = db()
            conn.execute(
                "INSERT INTO duty_swap_requests("
                "session_id,requester_user_id,requester_assignment_id,target_user_id,"
                "target_assignment_id,status,batch_id) VALUES(?,?,?,?,?,'PENDING','stale-target')",
                (
                    data["session_id"],
                    data["requester_id"],
                    data["past_assignment"],
                    data["target_id"],
                    data["future_assignment"],
                ),
            )
            conn.commit()

        csrf = self.login_as(data["target_id"])
        with patch.object(runtime_policy, "school_today", return_value=date(2026, 8, 30)):
            response = self.request(
                "post",
                "/swaps/batch/stale-target/target-review",
                data={"csrf": csrf, "action": "APPROVE"},
            )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            status = db().execute(
                "SELECT status FROM duty_swap_requests WHERE batch_id='stale-target'"
            ).fetchone()["status"]
            self.assertEqual(status, "PENDING")

    def test_hra_cannot_finalize_request_after_shift_becomes_past(self):
        data = self.create_fixture()
        with app.app_context():
            conn = db()
            conn.execute(
                "INSERT INTO duty_swap_requests("
                "session_id,requester_user_id,requester_assignment_id,target_user_id,"
                "target_assignment_id,status,batch_id,target_reviewed_at) "
                "VALUES(?,?,?,?,?,'TARGET_APPROVED','stale-hra',CURRENT_TIMESTAMP)",
                (
                    data["session_id"],
                    data["requester_id"],
                    data["past_assignment"],
                    data["target_id"],
                    data["future_assignment"],
                ),
            )
            conn.commit()

        csrf = self.login_as(data["hra_id"])
        with patch.object(runtime_policy, "school_today", return_value=date(2026, 8, 30)):
            response = self.request(
                "post",
                "/swaps/batch/stale-hra/hra-review",
                data={"csrf": csrf, "action": "APPROVE"},
            )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            status = db().execute(
                "SELECT status FROM duty_swap_requests WHERE batch_id='stale-hra'"
            ).fetchone()["status"]
            self.assertEqual(status, "TARGET_APPROVED")


if __name__ == "__main__":
    unittest.main()
