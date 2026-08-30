import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("MAIL_ENABLED", "0")
os.environ.setdefault(
    "DATABASE_PATH",
    str(Path(tempfile.gettempdir()) / "ra-draft-email-tests.db"),
)

import portal_app  # noqa: E402,F401
import email_notifications  # noqa: E402
import session_status as session_status_module  # noqa: E402
import swap_email_hooks  # noqa: E402
from core import app, db  # noqa: E402


class EmailNotificationTestCase(unittest.TestCase):
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

    def login_as(self, user_id, csrf="email-test-csrf"):
        with self.client.session_transaction() as sess:
            sess["uid"] = user_id
            sess["csrf"] = csrf
        return csrf

    def create_session(self, *, status="CLOSED"):
        with app.app_context():
            conn = db()
            building_id = conn.execute(
                "INSERT INTO buildings(name) VALUES(?)", ("Maple Hall",)
            ).lastrowid
            hra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("hra-email", "hra@g.rwu.edu", "Harper HRA", "HRA", building_id),
            ).lastrowid
            ra1_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("ra-email-1", "alice@g.rwu.edu", "Alice RA", "RA", building_id),
            ).lastrowid
            ra2_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("ra-email-2", "bob@g.rwu.edu", "Bob RA", "RA", building_id),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,status,created_by) "
                "VALUES(?,?,?,?,?,?)",
                ("Fall Duty", building_id, "2026-09-01", "2026-09-10", status, hra_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1),(?,?,2)",
                (session_id, ra1_id, session_id, ra2_id),
            )
            a1 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra1_id, "2026-09-01", hra_id),
            ).lastrowid
            b1 = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, ra2_id, "2026-09-02", hra_id),
            ).lastrowid
            conn.commit()

        return {
            "building_id": building_id,
            "session_id": session_id,
            "hra_id": hra_id,
            "ra1_id": ra1_id,
            "ra2_id": ra2_id,
            "a1": a1,
            "b1": b1,
        }

    def test_final_schedule_email_is_branded_and_links_calendar(self):
        data = self.create_session(status="CLOSED")
        with app.test_request_context("/", base_url="https://ci.local"):
            with patch.object(email_notifications, "MAIL_USERNAME", "ra.draft@gmail.com"):
                with patch.object(email_notifications, "_deliver", return_value=2) as deliver:
                    sent = email_notifications.send_session_closed_notifications(
                        data["session_id"]
                    )

        self.assertEqual(sent, 2)
        messages = deliver.call_args.args[0]
        self.assertEqual(len(messages), 2)
        alice_message = next(
            message for message in messages if "alice@g.rwu.edu" in message["To"]
        )
        self.assertEqual(alice_message["From"], "RA Draft <ra.draft@gmail.com>")
        self.assertIn("Maple Hall duty schedule", alice_message["Subject"])
        html = alice_message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("September 1, 2026", html)
        self.assertIn("/calendar/session/", html)
        self.assertIn("/my-calendar.ics", html)

    def test_closing_session_queues_final_schedule_notification_after_commit(self):
        data = self.create_session(status="OPEN")
        csrf = self.login_as(data["hra_id"])

        with patch.object(
            session_status_module, "send_session_closed_notifications"
        ) as notify:
            response = self.request(
                "post",
                f"/sessions/{data['session_id']}/status",
                data={"csrf": csrf, "status": "CLOSED"},
            )

        self.assertEqual(response.status_code, 302)
        notify.assert_called_once_with(data["session_id"])
        with app.app_context():
            status = db().execute(
                "SELECT status FROM draft_sessions WHERE id=?",
                (data["session_id"],),
            ).fetchone()["status"]
            self.assertEqual(status, "CLOSED")

    def test_swap_hooks_notify_target_hra_and_all_parties(self):
        data = self.create_session(status="CLOSED")

        with patch.object(swap_email_hooks, "MAIL_ENABLED", True), patch.object(
            swap_email_hooks, "send_swap_request_notification"
        ) as request_notify, patch.object(
            swap_email_hooks, "send_hra_swap_review_notification"
        ) as hra_notify, patch.object(
            swap_email_hooks, "send_swap_approved_notifications"
        ) as approved_notify:
            csrf = self.login_as(data["ra1_id"], "alice-csrf")
            response = self.request(
                "post",
                f"/swaps/session/{data['session_id']}/request",
                data={
                    "csrf": csrf,
                    "my_assignment_ids": [str(data["a1"])],
                    "target_assignment_ids": [str(data["b1"])],
                },
            )
            self.assertEqual(response.status_code, 302)

            with app.app_context():
                swap = db().execute(
                    "SELECT batch_id,status FROM duty_swap_requests WHERE session_id=?",
                    (data["session_id"],),
                ).fetchone()
                batch_id = swap["batch_id"]
                self.assertEqual(swap["status"], "PENDING")
            request_notify.assert_called_once_with(batch_id)

            csrf = self.login_as(data["ra2_id"], "bob-csrf")
            response = self.request(
                "post",
                f"/swaps/batch/{batch_id}/target-review",
                data={"csrf": csrf, "action": "APPROVE"},
            )
            self.assertEqual(response.status_code, 302)
            hra_notify.assert_called_once_with(batch_id)

            csrf = self.login_as(data["hra_id"], "hra-csrf")
            response = self.request(
                "post",
                f"/swaps/batch/{batch_id}/hra-review",
                data={"csrf": csrf, "action": "APPROVE"},
            )
            self.assertEqual(response.status_code, 302)
            approved_notify.assert_called_once_with(batch_id, data["hra_id"])


if __name__ == "__main__":
    unittest.main()
