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
    str(Path(tempfile.gettempdir()) / "ra-draft-swap-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402


class DutySwapTestCase(unittest.TestCase):
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

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(
            path,
            base_url="https://ci.local",
            **kwargs,
        )

    def add_building(self, name="Maple Hall"):
        with app.app_context():
            cur = db().execute("INSERT INTO buildings(name) VALUES(?)", (name,))
            db().commit()
            return cur.lastrowid

    def add_user(self, *, sub, email, name, role="RA", building_id=None):
        with app.app_context():
            cur = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)",
                (sub, email, name, role, building_id),
            )
            db().commit()
            return cur.lastrowid

    def login_as(self, user_id, csrf="test-swap-csrf"):
        with self.client.session_transaction() as sess:
            sess["uid"] = user_id
            sess["csrf"] = csrf
        return csrf

    def get_csrf(self):
        with self.client.session_transaction() as sess:
            return sess.get("csrf") or "test-swap-csrf"



    def create_closed_session_with_assignments(self):
        building_id = self.add_building("Oak Hall")
        hra_id = self.add_user(
            sub="hra-1", email="hra@g.rwu.edu", name="HRA User", role="HRA", building_id=building_id
        )
        ra1_id = self.add_user(
            sub="ra-1", email="ra1@g.rwu.edu", name="Alice RA", role="RA", building_id=building_id
        )
        ra2_id = self.add_user(
            sub="ra-2", email="ra2@g.rwu.edu", name="Bob RA", role="RA", building_id=building_id
        )

        with app.app_context():
            conn = db()
            cur = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,status,created_by) "
                "VALUES(?,?,?,?,?,?)",
                ("Fall 2026", building_id, "2026-09-01", "2026-09-10", "CLOSED", hra_id),
            )
            session_id = cur.lastrowid

            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1),(?,?,2)",
                (session_id, ra1_id, session_id, ra2_id),
            )

            # Alice assignments: Sept 1, Sept 3
            a1 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra1_id, "2026-09-01", hra_id),
            ).lastrowid
            a2 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra1_id, "2026-09-03", hra_id),
            ).lastrowid

            # Bob assignments: Sept 2, Sept 4
            b1 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra2_id, "2026-09-02", hra_id),
            ).lastrowid
            b2 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra2_id, "2026-09-04", hra_id),
            ).lastrowid

            conn.commit()

        return {
            "building_id": building_id,
            "session_id": session_id,
            "hra_id": hra_id,
            "ra1_id": ra1_id,
            "ra2_id": ra2_id,
            "a1": a1,
            "a2": a2,
            "b1": b1,
            "b2": b2,
        }

    def test_swap_page_requires_closed_session(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]

        # If OPEN, swap_page redirects to view_session
        with app.app_context():
            db().execute("UPDATE draft_sessions SET status='OPEN' WHERE id=?", (session_id,))
            db().commit()

        self.login_as(data["ra1_id"])
        resp = self.request("get", f"/swaps/session/{session_id}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"/sessions/{session_id}", resp.headers["Location"])

        # When CLOSED, swap_page loads with 200
        with app.app_context():
            db().execute("UPDATE draft_sessions SET status='CLOSED' WHERE id=?", (session_id,))
            db().commit()

        resp = self.request("get", f"/swaps/session/{session_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Duty Swaps", resp.get_data(as_text=True))

    def test_full_two_stage_swap_flow(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        ra1_id, ra2_id, hra_id = data["ra1_id"], data["ra2_id"], data["hra_id"]
        a1, b1 = data["a1"], data["b1"]

        # Step 1: Alice submits swap request (Alice's Sept 1 for Bob's Sept 2)
        self.login_as(ra1_id)
        # Establish session CSRF
        self.request("get", f"/swaps/session/{session_id}")
        csrf = self.get_csrf()

        resp = self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={
                "csrf": csrf,
                "my_assignment_ids": [str(a1)],
                "target_assignment_ids": [str(b1)],
            },
        )
        self.assertEqual(resp.status_code, 302)

        # Check DB state: status is PENDING
        with app.app_context():
            swap = db().execute(
                "SELECT * FROM duty_swap_requests WHERE session_id=?",
                (session_id,),
            ).fetchone()
            self.assertIsNotNone(swap)
            self.assertEqual(swap["status"], "PENDING")
            self.assertEqual(swap["requester_user_id"], ra1_id)
            self.assertEqual(swap["target_user_id"], ra2_id)
            batch_id = swap["batch_id"]

        # Step 2: Bob (target) reviews and approves
        self.login_as(ra2_id)
        self.request("get", f"/swaps/session/{session_id}")
        csrf_bob = self.get_csrf()

        resp = self.request(
            "post",
            f"/swaps/batch/{batch_id}/target-review",
            data={"csrf": csrf_bob, "action": "APPROVE"},
        )
        self.assertEqual(resp.status_code, 302)

        # Check DB state: status is TARGET_APPROVED
        with app.app_context():
            swap = db().execute(
                "SELECT * FROM duty_swap_requests WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            self.assertEqual(swap["status"], "TARGET_APPROVED")

        # Step 3: HRA reviews and approves
        self.login_as(hra_id)
        self.request("get", f"/swaps/session/{session_id}")
        csrf_hra = self.get_csrf()

        resp = self.request(
            "post",
            f"/swaps/batch/{batch_id}/hra-review",
            data={
                "csrf": csrf_hra,
                "action": "APPROVE",
                "google_calendar_updated": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)

        # Check DB state: swap is APPROVED and assignments are updated
        with app.app_context():
            swap = db().execute(
                "SELECT * FROM duty_swap_requests WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            self.assertEqual(swap["status"], "APPROVED")

            # Check that assignments swapped user_ids
            assign_a1 = db().execute("SELECT * FROM assignments WHERE id=?", (a1,)).fetchone()
            assign_b1 = db().execute("SELECT * FROM assignments WHERE id=?", (b1,)).fetchone()
            self.assertEqual(assign_a1["user_id"], ra2_id)  # Now belongs to Bob
            self.assertEqual(assign_b1["user_id"], ra1_id)  # Now belongs to Alice

    def test_batch_swap_multiple_dates_same_partner(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        ra1_id, ra2_id, hra_id = data["ra1_id"], data["ra2_id"], data["hra_id"]
        a1, a2, b1, b2 = data["a1"], data["a2"], data["b1"], data["b2"]

        self.login_as(ra1_id)
        self.request("get", f"/swaps/session/{session_id}")
        csrf = self.get_csrf()

        # Alice swaps 2 dates with Bob in a single batch
        resp = self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={
                "csrf": csrf,
                "my_assignment_ids": [str(a1), str(a2)],
                "target_assignment_ids": [str(b1), str(b2)],
            },
        )
        self.assertEqual(resp.status_code, 302)

        with app.app_context():
            swaps = db().execute(
                "SELECT * FROM duty_swap_requests WHERE session_id=?",
                (session_id,),
            ).fetchall()
            self.assertEqual(len(swaps), 2)
            # Both rows share the same batch_id
            self.assertEqual(swaps[0]["batch_id"], swaps[1]["batch_id"])
            batch_id = swaps[0]["batch_id"]

        # Target approves batch
        self.login_as(ra2_id)
        self.request("get", f"/swaps/session/{session_id}")
        resp = self.request(
            "post",
            f"/swaps/batch/{batch_id}/target-review",
            data={"csrf": self.get_csrf(), "action": "APPROVE"},
        )
        self.assertEqual(resp.status_code, 302)

        # HRA approves batch
        self.login_as(hra_id)
        self.request("get", f"/swaps/session/{session_id}")
        resp = self.request(
            "post",
            f"/swaps/batch/{batch_id}/hra-review",
            data={
                "csrf": self.get_csrf(),
                "action": "APPROVE",
                "google_calendar_updated": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)

        with app.app_context():
            for s in db().execute("SELECT status FROM duty_swap_requests WHERE batch_id=?", (batch_id,)):
                self.assertEqual(s["status"], "APPROVED")

    def test_cross_building_swap_rejected(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        ra1_id = data["ra1_id"]
        a1 = data["a1"]

        # Create another building and RA
        bldg2_id = self.add_building("Pine Hall")
        ra3_id = self.add_user(
            sub="ra-3", email="ra3@g.rwu.edu", name="Charlie RA", role="RA", building_id=bldg2_id
        )

        with app.app_context():
            # Add assignment for ra3 in this session (even if invalid building)
            c1 = db().execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra3_id, "2026-09-05", data["hra_id"]),
            ).lastrowid
            db().commit()

        self.login_as(ra1_id)
        self.request("get", f"/swaps/session/{session_id}")
        csrf = self.get_csrf()

        resp = self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={
                "csrf": csrf,
                "my_assignment_ids": [str(a1)],
                "target_assignment_ids": [str(c1)],
            },
        )
        self.assertEqual(resp.status_code, 302)
        with app.app_context():
            swaps = db().execute("SELECT * FROM duty_swap_requests WHERE session_id=?", (session_id,)).fetchall()
            self.assertEqual(len(swaps), 0)


    def test_requester_can_cancel_pending_swap(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        ra1_id = data["ra1_id"]
        a1, b1 = data["a1"], data["b1"]

        self.login_as(ra1_id)
        self.request("get", f"/swaps/session/{session_id}")
        csrf = self.get_csrf()

        self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={
                "csrf": csrf,
                "my_assignment_ids": [str(a1)],
                "target_assignment_ids": [str(b1)],
            },
        )

        with app.app_context():
            swap = db().execute(
                "SELECT * FROM duty_swap_requests WHERE session_id=?", (session_id,)
            ).fetchone()
            batch_id = swap["batch_id"]

        # Requester cancels
        resp = self.request(
            "post",
            f"/swaps/batch/{batch_id}/cancel",
            data={"csrf": csrf},
        )
        self.assertEqual(resp.status_code, 302)

        with app.app_context():
            swap = db().execute(
                "SELECT * FROM duty_swap_requests WHERE batch_id=?", (batch_id,)
            ).fetchone()
            self.assertEqual(swap["status"], "CANCELLED")


    def test_target_rejection_flow(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        ra1_id, ra2_id = data["ra1_id"], data["ra2_id"]
        a1, b1 = data["a1"], data["b1"]

        self.login_as(ra1_id)
        self.request("get", f"/swaps/session/{session_id}")
        self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={"csrf": self.get_csrf(), "my_assignment_ids": [str(a1)], "target_assignment_ids": [str(b1)]},
        )
        with app.app_context():
            swap = db().execute("SELECT * FROM duty_swap_requests WHERE session_id=?", (session_id,)).fetchone()
            batch_id = swap["batch_id"]

        self.login_as(ra2_id)
        self.request("get", f"/swaps/session/{session_id}")
        resp = self.request(
            "post",
            f"/swaps/batch/{batch_id}/target-review",
            data={"csrf": self.get_csrf(), "action": "REJECT"},
        )
        self.assertEqual(resp.status_code, 302)
        with app.app_context():
            swap = db().execute("SELECT * FROM duty_swap_requests WHERE batch_id=?", (batch_id,)).fetchone()
            self.assertEqual(swap["status"], "REJECTED")

    def test_hra_rejection_flow(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        ra1_id, ra2_id, hra_id = data["ra1_id"], data["ra2_id"], data["hra_id"]
        a1, b1 = data["a1"], data["b1"]

        self.login_as(ra1_id)
        self.request("get", f"/swaps/session/{session_id}")
        self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={"csrf": self.get_csrf(), "my_assignment_ids": [str(a1)], "target_assignment_ids": [str(b1)]},
        )
        with app.app_context():
            batch_id = db().execute("SELECT batch_id FROM duty_swap_requests WHERE session_id=?", (session_id,)).fetchone()["batch_id"]

        self.login_as(ra2_id)
        self.request("get", f"/swaps/session/{session_id}")
        self.request(
            "post",
            f"/swaps/batch/{batch_id}/target-review",
            data={"csrf": self.get_csrf(), "action": "APPROVE"},
        )

        self.login_as(hra_id)
        self.request("get", f"/swaps/session/{session_id}")
        resp = self.request(
            "post",
            f"/swaps/batch/{batch_id}/hra-review",
            data={"csrf": self.get_csrf(), "action": "REJECT"},
        )
        self.assertEqual(resp.status_code, 302)
        with app.app_context():
            swap = db().execute("SELECT * FROM duty_swap_requests WHERE batch_id=?", (batch_id,)).fetchone()
            self.assertEqual(swap["status"], "REJECTED")
            # Assignments should not have changed
            assign_a1 = db().execute("SELECT user_id FROM assignments WHERE id=?", (a1,)).fetchone()
            self.assertEqual(assign_a1["user_id"], ra1_id)

    def test_batch_with_multiple_different_partners_rejected(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        building_id = data["building_id"]
        ra1_id, ra2_id = data["ra1_id"], data["ra2_id"]
        a1, a2, b1 = data["a1"], data["a2"], data["b1"]

        ra3_id = self.add_user(
            sub="ra-3", email="ra3@g.rwu.edu", name="Charlie RA", role="RA", building_id=building_id
        )
        with app.app_context():
            c1 = db().execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra3_id, "2026-09-06", data["hra_id"]),
            ).lastrowid
            db().commit()

        self.login_as(ra1_id)
        self.request("get", f"/swaps/session/{session_id}")
        # Trying to submit a single batch swapping a1 with Bob (b1) and a2 with Charlie (c1)
        resp = self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={
                "csrf": self.get_csrf(),
                "my_assignment_ids": [str(a1), str(a2)],
                "target_assignment_ids": [str(b1), str(c1)],
            },
        )
        self.assertEqual(resp.status_code, 302)
        with app.app_context():
            swaps = db().execute("SELECT * FROM duty_swap_requests WHERE session_id=?", (session_id,)).fetchall()
            self.assertEqual(len(swaps), 0)

    def test_async_swap_action_response(self):
        data = self.create_closed_session_with_assignments()
        session_id = data["session_id"]
        ra1_id, ra2_id = data["ra1_id"], data["ra2_id"]
        a1, b1 = data["a1"], data["b1"]

        self.login_as(ra1_id)
        self.request("get", f"/swaps/session/{session_id}")
        resp = self.request(
            "post",
            f"/swaps/session/{session_id}/request",
            data={
                "csrf": self.get_csrf(),
                "my_assignment_ids": [str(a1)],
                "target_assignment_ids": [str(b1)],
            },
            headers={"X-RA-Draft-Async": "1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json["ok"])
        self.assertIn("Swap request submitted", resp.json["message"])


if __name__ == "__main__":
    unittest.main()
