"""Consent, banner persistence, reminder timing, and durable delivery claims."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_admin_management import AdminManagementTestCase
from core import app, db
import deadline_reminders as reminders
from deadlines import parse_deadlines
from staff_event_schedule import SCHOOL_TIMEZONE


class DeadlineReminderTests(unittest.TestCase):
    setUp = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    login_as = AdminManagementTestCase.login_as

    def user(self, *, opted_in=False, disabled=False, prostaff=False, sub="reminder"):
        uid = self.add_user(sub=sub, email=sub + "@rwu.edu", name="Alex RA",
                            building_id=self.add_building(sub))
        with app.app_context():
            db().execute(
                "UPDATE users SET deadline_email_opt_in=?,disabled=?,is_prostaff=? WHERE id=?",
                (int(opted_in), int(disabled), int(prostaff), uid),
            )
            db().commit()
        self.login_as(uid)
        return uid

    def state(self):
        return {"notice": "", "items": parse_deadlines([
            {"title": "Today <test>", "due_date": "2026-10-19", "description": "Submit report"},
            {"title": "Three days", "due_date": "2026-10-22"},
            {"title": "Tomorrow", "due_date": "2026-10-20"},
            {"title": "Passed", "due_date": "2026-10-18"},
            {"title": "Earlier today", "due_at": "2026-10-19T08:00:00-04:00"},
        ])}

    def run_worker(self, deliver, now=None, state=None):
        with app.app_context(), patch.object(reminders.mail, "MAIL_ENABLED", True), \
                patch.object(reminders, "load_deadlines", return_value=state or self.state()), \
                patch.object(reminders.mail, "_deliver", side_effect=deliver) as sender:
            sent = reminders.send_due_reminders(now or datetime(2026, 10, 19, 9, tzinfo=SCHOOL_TIMEZONE))
        return sent, sender

    def test_banner_opt_in_updates_checkbox_and_stays_hidden(self):
        uid = self.user()
        page = self.request("get", "/deadlines")
        self.assertIn(b"New:</strong> Get email reminders about upcoming deadlines", page.data)
        self.assertNotIn(b'value="1" checked', page.data)
        response = self.request("post", "/deadlines/email-preferences",
                                data={"csrf": "admin-management-csrf", "email_reminders": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/deadlines"))
        self.client = app.test_client()
        self.login_as(uid)
        page = self.request("get", "/deadlines")
        self.assertIn(b'value="1" checked', page.data)
        self.assertNotIn(b'aria-label="New deadline email reminders"', page.data)
        self.request("post", "/deadlines/email-preferences", data={"csrf": "admin-management-csrf"})
        page = self.request("get", "/deadlines")
        self.assertNotIn(b'value="1" checked', page.data)
        self.assertNotIn(b'aria-label="New deadline email reminders"', page.data)

    def test_dismissal_is_permanent_without_subscribing(self):
        uid = self.user()
        response = self.request("post", "/deadlines/dismiss-banner", data={
            "csrf": "admin-management-csrf", "next": "/deadlines?month=2027-01",
        })
        self.assertTrue(response.location.endswith("/deadlines?month=2027-01"))
        self.client = app.test_client()
        self.login_as(uid)
        self.assertNotIn(b'aria-label="New deadline email reminders"', self.request("get", "/dashboard").data)
        with app.app_context():
            user = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            self.assertEqual(user["deadline_banner_dismissed"], 1)
            self.assertEqual(user["deadline_email_opt_in"], 0)

    def test_preferences_require_auth_and_csrf_and_ignore_other_user_id(self):
        self.assertEqual(self.request("post", "/deadlines/email-preferences").status_code, 302)
        uid = self.user()
        for path in ("/deadlines/email-preferences", "/deadlines/dismiss-banner"):
            self.assertEqual(self.request("post", path, data={"email_reminders": "1"}).status_code, 400)
        other = self.add_user(sub="other", email="other@rwu.edu", name="Other")
        self.request("post", "/deadlines/email-preferences", data={
            "csrf": "admin-management-csrf", "email_reminders": "1", "user_id": other,
        })
        with app.app_context():
            self.assertEqual(db().execute("SELECT deadline_email_opt_in FROM users WHERE id=?", (uid,)).fetchone()[0], 1)
            self.assertEqual(db().execute("SELECT deadline_email_opt_in FROM users WHERE id=?", (other,)).fetchone()[0], 0)

    def test_prostaff_can_save_and_dismiss_but_external_redirects_are_rejected(self):
        self.user(prostaff=True)
        response = self.request("post", "/deadlines/email-preferences", data={
            "csrf": "admin-management-csrf", "email_reminders": "1",
        })
        self.assertTrue(response.location.endswith("/deadlines"))
        for path in ("https://evil.example", "//evil.example", "/\\evil.example", "https://[invalid"):
            response = self.request("post", "/deadlines/dismiss-banner", data={
                "csrf": "admin-management-csrf", "next": path,
            })
            self.assertTrue(response.location.endswith("/dashboard"))

    def test_only_opted_in_enabled_accounts_get_today_and_three_day_digest(self):
        self.user(opted_in=True)
        self.user(sub="not-opted-in")
        self.user(sub="disabled", opted_in=True, disabled=True)
        sent, sender = self.run_worker(lambda messages: 1)
        self.assertEqual(sent, 1)
        message = sender.call_args.args[0][0]
        plain = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertEqual(message["To"], "Alex RA <reminder@rwu.edu>")
        self.assertIn("Due today: Today <test>", plain)
        self.assertIn("Due in 3 days: Three days", plain)
        self.assertNotIn("Tomorrow", plain)
        self.assertNotIn("Passed", plain)
        self.assertNotIn("Earlier today", plain)
        self.assertIn("Today &lt;test&gt;", html)
        self.assertIn("turn off email reminders", html)
        self.assertIn("https://ci.local/deadlines", plain)

    def test_success_is_not_resent_on_later_runs_or_restart(self):
        self.user(opted_in=True)
        self.assertEqual(self.run_worker(lambda messages: 1)[0], 1)
        sent, sender = self.run_worker(lambda messages: 1)
        self.assertEqual(sent, 0)
        sender.assert_not_called()
        # The same deadline gets its separate day-of reminder three days later.
        sent, sender = self.run_worker(lambda messages: 1,
                                      datetime(2026, 10, 22, 9, tzinfo=SCHOOL_TIMEZONE))
        self.assertEqual(sent, 1)
        self.assertIn("Due today: Three days", sender.call_args.args[0][0].get_body(preferencelist=("plain",)).get_content())

    def test_failed_smtp_can_retry_without_marking_sent(self):
        self.user(opted_in=True)
        self.assertEqual(self.run_worker(lambda messages: 0)[0], 0)
        self.assertEqual(self.run_worker(lambda messages: 1)[0], 1)
        self.assertEqual(self.run_worker(lambda messages: 1)[0], 0)

    def test_active_claim_blocks_duplicate_worker_and_expired_claim_recovers(self):
        uid = self.user(opted_in=True)
        now = datetime(2026, 10, 19, 13, tzinfo=timezone.utc)
        candidates = [(next(item for item in self.state()["items"] if item["title"] == "Today <test>"), 0)]
        with app.app_context():
            self.assertEqual(len(reminders._claim(uid, candidates, now)[2]), 1)
            self.assertEqual(reminders._claim(uid, candidates, now)[2], [])
            self.assertEqual(len(reminders._claim(uid, candidates, now + timedelta(minutes=16))[2]), 1)

    def test_skips_before_nine_disabled_mail_and_unavailable_feed(self):
        self.user(opted_in=True)
        sent, sender = self.run_worker(lambda messages: 1, datetime(2026, 10, 19, 8, 59, tzinfo=SCHOOL_TIMEZONE))
        self.assertEqual(sent, 0)
        sender.assert_not_called()
        with app.app_context(), patch.object(reminders.mail, "MAIL_ENABLED", False), \
                patch.object(reminders, "load_deadlines") as feed:
            self.assertEqual(reminders.send_due_reminders(), 0)
            feed.assert_not_called()
        state = self.state()
        state["notice"] = "Showing stale data"
        self.assertEqual(self.run_worker(lambda messages: 1, state=state)[0], 0)

    def test_three_calendar_days_across_dst_change(self):
        self.user(opted_in=True)
        state = {"notice": "", "items": parse_deadlines([{"title": "After DST", "due_date": "2026-11-02"}])}
        sent, _ = self.run_worker(lambda messages: 1, datetime(2026, 10, 30, 9, tzinfo=SCHOOL_TIMEZONE), state)
        self.assertEqual(sent, 1)

    def test_opt_out_after_claim_is_respected_before_smtp(self):
        uid = self.user(opted_in=True)
        original = reminders._claim
        def claim_and_opt_out(*args):
            result = original(*args)
            db().execute("UPDATE users SET deadline_email_opt_in=0 WHERE id=?", (uid,))
            db().commit()
            return result
        with patch.object(reminders, "_claim", side_effect=claim_and_opt_out):
            sent, sender = self.run_worker(lambda messages: 1)
        self.assertEqual(sent, 0)
        sender.assert_not_called()


del AdminManagementTestCase
