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
    str(Path(tempfile.gettempdir()) / "ra-draft-swap-navigation-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402


class SwapNavigationValidationTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
        with app.app_context():
            conn = db()
            for table in (
                "audit_log",
                "duty_swap_requests",
                "assignments",
                "session_order",
                "draft_sessions",
                "users",
                "buildings",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

        self.data = self._create_session()

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def login_as(self, user_id):
        with self.client.session_transaction() as sess:
            sess["uid"] = user_id
            sess["csrf"] = "swap-validation-csrf"

    def _create_session(self):
        with app.app_context():
            conn = db()
            building_id = conn.execute("INSERT INTO buildings(name) VALUES('Maple Hall')").lastrowid
            hra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                ("hra", "hra@g.rwu.edu", "HRA User", "HRA", building_id),
            ).lastrowid
            admin_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                ("admin", "admin@rwu.edu", "Admin User", "ADMIN", None),
            ).lastrowid
            alice_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                ("alice", "alice@g.rwu.edu", "Alice RA", "RA", building_id),
            ).lastrowid
            bob_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                ("bob", "bob@g.rwu.edu", "Bob RA", "RA", building_id),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,status,created_by) "
                "VALUES(?,?,?,?,?,?)",
                ("Fall Duty", building_id, "2026-09-01", "2026-09-10", "CLOSED", hra_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1),(?,?,2)",
                (session_id, alice_id, session_id, bob_id),
            )
            alice_sep1 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, alice_id, "2026-09-01", hra_id),
            ).lastrowid
            alice_sep3 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, alice_id, "2026-09-03", hra_id),
            ).lastrowid
            bob_sep1 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, bob_id, "2026-09-01", hra_id),
            ).lastrowid
            bob_sep4 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, bob_id, "2026-09-04", hra_id),
            ).lastrowid
            conn.commit()
            return {
                "session_id": session_id,
                "hra_id": hra_id,
                "admin_id": admin_id,
                "alice_id": alice_id,
                "bob_id": bob_id,
                "alice_sep1": alice_sep1,
                "alice_sep3": alice_sep3,
                "bob_sep1": bob_sep1,
                "bob_sep4": bob_sep4,
            }

    def _post_swap(self, my_assignment, target_assignment):
        return self._post_swap_batch([my_assignment], [target_assignment])

    def _post_swap_batch(self, my_assignments, target_assignments):
        return self.request(
            "post",
            f"/swaps/session/{self.data['session_id']}/request",
            data={
                "csrf": "swap-validation-csrf",
                "my_assignment_ids": [str(item) for item in my_assignments],
                "target_assignment_ids": [str(item) for item in target_assignments],
            },
            headers={"X-RA-Draft-Async": "1"},
        )

    def _post_manager_swap(self, first_assignment, second_assignment):
        return self.request(
            "post",
            f"/swaps/session/{self.data['session_id']}/manager-swap",
            data={
                "csrf": "swap-validation-csrf",
                "first_assignment_id": str(first_assignment),
                "second_assignment_id": str(second_assignment),
                "google_calendar_updated": "1",
            },
            headers={"X-RA-Draft-Async": "1"},
        )

    def test_duty_swap_tab_has_dedicated_menu_route(self):
        self.login_as(self.data["alice_id"])
        response = self.request("get", "/swaps")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Available sessions", page)
        self.assertIn("Fall Duty", page)
        self.assertIn(f"/swaps/session/{self.data['session_id']}", page)

    def test_manager_manual_swap_controls_are_manager_only(self):
        self.login_as(self.data["hra_id"])
        response = self.request("get", f"/swaps/session/{self.data['session_id']}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Manual duty swap", page)
        self.assertIn("manager-swap", page)

        self.login_as(self.data["alice_id"])
        response = self.request("get", f"/swaps/session/{self.data['session_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Manual duty swap", response.get_data(as_text=True))

    def test_hra_can_manually_swap_two_participants(self):
        self.login_as(self.data["hra_id"])
        response = self._post_manager_swap(self.data["alice_sep3"], self.data["bob_sep4"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        with app.app_context():
            first = db().execute(
                "SELECT user_id,duty_date FROM assignments WHERE id=?",
                (self.data["alice_sep3"],),
            ).fetchone()
            second = db().execute(
                "SELECT user_id,duty_date FROM assignments WHERE id=?",
                (self.data["bob_sep4"],),
            ).fetchone()
            swap = db().execute(
                "SELECT status,reviewed_by FROM duty_swap_requests WHERE session_id=?",
                (self.data["session_id"],),
            ).fetchone()
            audit_row = db().execute(
                "SELECT action FROM audit_log WHERE action='swap.manager_manual' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        self.assertEqual((first["user_id"], first["duty_date"]), (self.data["bob_id"], "2026-09-03"))
        self.assertEqual((second["user_id"], second["duty_date"]), (self.data["alice_id"], "2026-09-04"))
        self.assertEqual(swap["status"], "APPROVED")
        self.assertEqual(swap["reviewed_by"], self.data["hra_id"])
        self.assertIsNotNone(audit_row)

    def test_admin_can_manually_swap_without_building_assignment(self):
        self.login_as(self.data["admin_id"])
        response = self._post_manager_swap(self.data["alice_sep3"], self.data["bob_sep4"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        with app.app_context():
            reviewer = db().execute(
                "SELECT reviewed_by FROM duty_swap_requests WHERE session_id=?",
                (self.data["session_id"],),
            ).fetchone()["reviewed_by"]
        self.assertEqual(reviewer, self.data["admin_id"])

    def test_ra_cannot_use_manager_manual_swap_endpoint(self):
        self.login_as(self.data["alice_id"])
        response = self._post_manager_swap(self.data["alice_sep3"], self.data["bob_sep4"])
        self.assertEqual(response.status_code, 403)

    def test_manager_manual_swap_rejects_same_date_and_duplicate_results(self):
        self.login_as(self.data["hra_id"])
        response = self._post_manager_swap(self.data["alice_sep1"], self.data["bob_sep1"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("same duty date", response.get_json()["message"])

        response = self._post_manager_swap(self.data["alice_sep3"], self.data["bob_sep1"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("assigned twice", response.get_json()["message"])

    def test_same_date_pair_is_rejected(self):
        self.login_as(self.data["alice_id"])
        response = self._post_swap(self.data["alice_sep1"], self.data["bob_sep1"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("same duty date", response.get_json()["message"])

    def test_single_pair_cannot_leave_either_ra_with_duplicate_date(self):
        self.login_as(self.data["alice_id"])

        response = self._post_swap(self.data["alice_sep3"], self.data["bob_sep1"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("assigned twice", response.get_json()["message"])

        response = self._post_swap(self.data["alice_sep1"], self.data["bob_sep4"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("assigned twice", response.get_json()["message"])

    def test_different_dates_remain_swappable(self):
        self.login_as(self.data["alice_id"])
        response = self._post_swap(self.data["alice_sep3"], self.data["bob_sep4"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_admin_can_give_final_swap_approval_for_any_building(self):
        self.login_as(self.data["alice_id"])
        response = self._post_swap(self.data["alice_sep3"], self.data["bob_sep4"])
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            batch_id = db().execute(
                "SELECT batch_id FROM duty_swap_requests WHERE session_id=? LIMIT 1",
                (self.data["session_id"],),
            ).fetchone()["batch_id"]

        self.login_as(self.data["bob_id"])
        response = self.request(
            "post",
            f"/swaps/batch/{batch_id}/target-review",
            data={"csrf": "swap-validation-csrf", "action": "APPROVE"},
            headers={"X-RA-Draft-Async": "1"},
        )
        self.assertEqual(response.status_code, 200)

        self.login_as(self.data["admin_id"])
        response = self.request(
            "post",
            f"/swaps/batch/{batch_id}/hra-review",
            data={
                "csrf": "swap-validation-csrf",
                "action": "APPROVE",
                "google_calendar_updated": "1",
            },
            headers={"X-RA-Draft-Async": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        with app.app_context():
            status = db().execute(
                "SELECT status FROM duty_swap_requests WHERE batch_id=? LIMIT 1",
                (batch_id,),
            ).fetchone()["status"]
        self.assertEqual(status, "APPROVED")

    def test_multi_pair_batch_may_free_dates_that_would_be_duplicates_individually(self):
        self.login_as(self.data["alice_id"])
        response = self._post_swap_batch(
            [self.data["alice_sep1"], self.data["alice_sep3"]],
            [self.data["bob_sep4"], self.data["bob_sep1"]],
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        with app.app_context():
            batch_id = db().execute(
                "SELECT batch_id FROM duty_swap_requests WHERE session_id=? LIMIT 1",
                (self.data["session_id"],),
            ).fetchone()["batch_id"]

        self.login_as(self.data["bob_id"])
        response = self.request(
            "post",
            f"/swaps/batch/{batch_id}/target-review",
            data={"csrf": "swap-validation-csrf", "action": "APPROVE"},
            headers={"X-RA-Draft-Async": "1"},
        )
        self.assertEqual(response.status_code, 200)

        self.login_as(self.data["hra_id"])
        response = self.request(
            "post",
            f"/swaps/batch/{batch_id}/hra-review",
            data={
                "csrf": "swap-validation-csrf",
                "action": "APPROVE",
                "google_calendar_updated": "1",
            },
            headers={"X-RA-Draft-Async": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        with app.app_context():
            alice_dates = {
                row["duty_date"]
                for row in db().execute(
                    "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=?",
                    (self.data["session_id"], self.data["alice_id"]),
                ).fetchall()
            }
            bob_dates = {
                row["duty_date"]
                for row in db().execute(
                    "SELECT duty_date FROM assignments WHERE session_id=? AND user_id=?",
                    (self.data["session_id"], self.data["bob_id"]),
                ).fetchall()
            }

        self.assertEqual(alice_dates, {"2026-09-01", "2026-09-04"})
        self.assertEqual(bob_dates, {"2026-09-01", "2026-09-03"})


if __name__ == "__main__":
    unittest.main()
