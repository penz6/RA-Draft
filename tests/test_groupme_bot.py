"""GroupMe's documented posting contract and deadline-only scheduling."""

import hashlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import requests
from test_admin_management import AdminManagementTestCase
from core import app, db
from deadlines import parse_deadlines
import deadline_reminders
import groupme_bot as bot
from staff_event_schedule import SCHOOL_TIMEZONE


class GroupMeBotTests(unittest.TestCase):
    setUp = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    login_as = AdminManagementTestCase.login_as

    def connection(self):
        hall = self.add_building()
        uid = self.add_user(sub="owner", email="owner@rwu.edu", name="Owner", role="ADMIN")
        with app.app_context():
            db().execute(
                "INSERT INTO building_groupme_bots "
                "(building_id,bot_ciphertext,bot_hash,enabled,configured_by) VALUES(?,?,?,?,?)",
                (hall, bot._cipher().encrypt(b"private-bot-1234").decode(),
                 hashlib.sha256(b"private-bot-1234").hexdigest(), 1, uid),
            )
            db().commit()
        return hall, uid

    def state(self):
        return {"notice": "", "items": parse_deadlines([
            {"title": "Next week", "due_date": "2026-10-26", "description": "Never post this description", "url": "https://example.com/private"},
            {"title": "Last week", "due_date": "2026-10-12"},
            {"title": "Today", "due_date": "2026-10-19"},
            {"title": "In three days", "due_date": "2026-10-22"},
        ])}

    def run_worker(self, *, success=True, now=None, state=None):
        with app.app_context(), patch.object(bot, "load_deadlines", return_value=state or self.state()), \
                patch.object(bot, "post_message", return_value=(success, None if success else "Unavailable")) as post:
            count = bot.send_due_groupme_reminders(now or datetime(2026, 10, 19, 9, tzinfo=SCHOOL_TIMEZONE))
        return count, post

    def settings(self, hall, **data):
        return self.request("post", f"/buildings/{hall}/groupme", data={
            "csrf": "admin-management-csrf", **data,
        })

    def test_admin_all_buildings_and_hra_only_own_building(self):
        hall, admin = self.connection()
        other_hall = self.add_building("Cedar")
        self.assertEqual(self.request("post", f"/buildings/{hall}/groupme").status_code, 302)
        for role in ("RA",):
            uid = self.add_user(sub=role, email=role + "@rwu.edu", name=role, role=role, building_id=hall)
            self.login_as(uid)
            self.assertNotIn(b'id="groupme-heading"', self.request("get", "/deadlines").data)
            for action in ("save", "test", "disconnect"):
                self.assertEqual(self.settings(hall, action=action).status_code, 403)
        self.login_as(admin)
        self.assertEqual(self.request("post", f"/buildings/{hall}/groupme").status_code, 400)
        self.assertEqual(self.settings(99999, action="disconnect").status_code, 404)
        self.assertEqual(self.settings(hall, action="bad-action").status_code, 400)
        page = self.request("get", f"/deadlines?groupme_building={other_hall}")
        self.assertIn(b"GroupMe reminders", page.data)
        self.assertIn(b"Cedar", page.data)
        self.assertIn(f'value="{other_hall}" selected'.encode(), page.data)
        self.assertIn(b"Manual setup (Admin)", page.data)
        hra = self.add_user(sub="HRA", email="HRA@rwu.edu", name="HRA", role="HRA", building_id=hall)
        self.login_as(hra)
        self.assertIn(b'id="groupme-heading"', self.request("get", "/deadlines").data)
        self.assertNotIn(b'name="bot_id"', self.request("get", "/deadlines").data)
        self.assertEqual(self.settings(other_hall, action="disconnect").status_code, 403)
        self.assertEqual(self.settings(hall, bot_id="cannot-set-secret").status_code, 403)
        self.login_as(admin)
        with app.app_context():
            db().execute("UPDATE users SET disabled=1 WHERE id=?", (admin,))
            db().commit()
        self.assertEqual(self.settings(hall, action="disconnect").status_code, 302)

    def test_save_encrypts_secret_never_exposes_it_and_blank_preserves_it(self):
        hall, uid = self.connection()
        self.login_as(uid)
        with patch.object(bot, "post_message") as post:
            response = self.settings(hall, bot_id="replacement-private-1234", groupme_reminders="1")
            self.assertTrue(response.location.endswith(f"/deadlines?groupme_building={hall}"))
            post.assert_not_called()
        with app.app_context():
            saved = dict(db().execute("SELECT * FROM building_groupme_bots WHERE building_id=?", (hall,)).fetchone())
            self.assertNotIn("replacement-private-1234", saved["bot_ciphertext"])
            self.assertEqual(bot._cipher().decrypt(saved["bot_ciphertext"].encode()), b"replacement-private-1234")
            audit = db().execute("SELECT details FROM audit_log WHERE action='deadlines.groupme.save'").fetchone()[0]
            self.assertNotIn("private", audit)
        self.assertNotIn(b"replacement-private", self.request("get", "/deadlines").data)
        self.settings(hall, bot_id="")
        with app.app_context():
            updated = db().execute("SELECT * FROM building_groupme_bots WHERE building_id=?", (hall,)).fetchone()
            self.assertEqual(updated["bot_ciphertext"], saved["bot_ciphertext"])
            self.assertEqual(updated["enabled"], 0)

    def test_invalid_or_duplicate_bot_does_not_change_existing_connection(self):
        hall, uid = self.connection()
        other_hall = self.add_building("Cedar")
        self.login_as(uid)
        for invalid in ("", "short", "https://example.com/bot", "a" * 201):
            self.settings(other_hall, bot_id=invalid, groupme_reminders="1")
        self.settings(other_hall, bot_id="private-bot-1234", groupme_reminders="1")
        with app.app_context():
            self.assertIsNone(db().execute("SELECT * FROM building_groupme_bots WHERE building_id=?", (other_hall,)).fetchone())
            self.assertIsNotNone(bot._connection(hall))

    def test_explicit_connection_test_is_throttled_and_provider_error_is_safe(self):
        hall, uid = self.connection()
        self.login_as(uid)
        with patch.object(bot, "post_message", return_value=(True, None)) as post:
            self.settings(hall, action="test")
            self.settings(hall, action="test")
            self.assertEqual(post.call_count, 1)
            self.assertTrue(post.call_args.args[1].startswith("📅 "))
        with app.app_context():
            db().execute("UPDATE building_groupme_bots SET last_test_at=0 WHERE building_id=?", (hall,))
            db().commit()
        with patch.object(bot, "post_message", return_value=(False, "GroupMe unavailable")):
            self.settings(hall, action="test")
        self.assertIn(b"GroupMe unavailable", self.request("get", "/deadlines").data)

    def test_disconnect_stops_posts_and_reconnecting_same_bot_keeps_receipts(self):
        hall, uid = self.connection()
        self.login_as(uid)
        self.assertEqual(self.run_worker()[0], 2)
        self.settings(hall, action="disconnect")
        self.assertEqual(self.run_worker()[0], 0)
        self.settings(hall, bot_id="private-bot-1234", groupme_reminders="1")
        self.assertEqual(self.run_worker()[0], 0)

    def test_demoted_admin_and_disconnect_between_claim_and_send_stop_posts(self):
        hall, uid = self.connection()
        with app.app_context():
            db().execute("UPDATE users SET role='RA',building_id=? WHERE id=?", (hall, uid))
            db().commit()
        self.assertEqual(self.run_worker()[0], 0)
        with app.app_context():
            db().execute("UPDATE users SET role='ADMIN' WHERE id=?", (uid,))
            db().commit()
        original_claim = bot._claim
        def disconnect_after_claim(*args):
            token = original_claim(*args)
            db().execute("DELETE FROM building_groupme_bots WHERE building_id=?", (hall,))
            db().commit()
            return token
        with patch.object(bot, "_claim", side_effect=disconnect_after_claim):
            count, post = self.run_worker()
        self.assertEqual(count, 0)
        post.assert_not_called()

    def test_one_week_before_and_after_only_with_minimal_calendar_messages(self):
        self.connection()
        count, post = self.run_worker()
        self.assertEqual(count, 2)
        messages = [call.args[1] for call in post.call_args_list]
        self.assertTrue(all(text.startswith("📅 ") and len(text) < 1000 for text in messages))
        self.assertTrue(any("Was due 1 week ago\nLast week" in text for text in messages))
        self.assertTrue(any("Due in 1 week\nNext week" in text for text in messages))
        self.assertTrue(all("description" not in text and "https://" not in text for text in messages))
        self.assertTrue(all("Today" not in text and "In three days" not in text for text in messages))

    def test_receipts_persist_and_prevent_repeated_success(self):
        self.connection()
        self.assertEqual(self.run_worker()[0], 2)
        count, post = self.run_worker()
        self.assertEqual(count, 0)
        post.assert_not_called()

    def test_failure_retries_and_stops_batch_after_first_failure(self):
        self.connection()
        count, post = self.run_worker(success=False)
        self.assertEqual(count, 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.run_worker()[0], 2)

    def test_claim_blocks_overlap_and_expired_claim_recovers(self):
        hall, _ = self.connection()
        now = datetime(2026, 10, 19, 9, tzinfo=SCHOOL_TIMEZONE).astimezone(bot.timezone.utc)
        item = self.state()["items"][0]
        with app.app_context():
            connection = bot._connection(hall)
            self.assertIsNotNone(bot._claim(connection, item, -7, now))
            self.assertIsNone(bot._claim(connection, item, -7, now))
            self.assertIsNotNone(bot._claim(connection, item, -7, now + timedelta(minutes=16)))

    def test_skips_early_stale_disabled_or_disconnected_destinations(self):
        hall, uid = self.connection()
        self.assertEqual(self.run_worker(now=datetime(2026, 10, 19, 8, 59, tzinfo=SCHOOL_TIMEZONE))[0], 0)
        self.assertEqual(self.run_worker(state={"notice": "Feed unavailable", "items": self.state()["items"]})[0], 0)
        with app.app_context():
            db().execute("UPDATE building_groupme_bots SET enabled=0 WHERE building_id=?", (hall,))
            db().commit()
        self.assertEqual(self.run_worker()[0], 0)
        with app.app_context():
            db().execute("UPDATE building_groupme_bots SET enabled=1 WHERE building_id=?", (hall,))
            db().execute("UPDATE users SET disabled=1 WHERE id=?", (uid,))
            db().commit()
        self.assertEqual(self.run_worker()[0], 0)

    def test_calendar_days_across_dst(self):
        self.connection()
        state = {"notice": "", "items": parse_deadlines([{"title": "After DST", "due_date": "2026-11-02"}])}
        self.assertEqual(self.run_worker(now=datetime(2026, 10, 26, 9, tzinfo=SCHOOL_TIMEZONE), state=state)[0], 1)

    def test_hra_connection_pauses_after_building_change(self):
        hall, uid = self.connection()
        with app.app_context():
            db().execute("UPDATE users SET role='HRA',building_id=? WHERE id=?", (hall, uid))
            db().commit()
        self.assertEqual(self.run_worker()[0], 2)
        with app.app_context():
            db().execute("DELETE FROM groupme_deadline_deliveries")
            db().execute("UPDATE users SET building_id=NULL WHERE id=?", (uid,))
            db().commit()
        count, post = self.run_worker()
        self.assertEqual(count, 0)
        post.assert_not_called()

    def test_groupme_runs_with_email_disabled_and_channel_failures_are_isolated(self):
        self.connection()
        with patch.object(deadline_reminders.mail, "MAIL_ENABLED", False), \
                patch.object(bot, "load_deadlines", return_value=self.state()), \
                patch.object(bot, "post_message", return_value=(True, None)) as post, \
                patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 19, 9, tzinfo=SCHOOL_TIMEZONE)
            deadline_reminders.run_reminder_cycle()
            self.assertEqual(post.call_count, 2)
        with patch.object(deadline_reminders, "send_due_reminders", side_effect=RuntimeError("SMTP")), \
                patch.object(bot, "send_due_groupme_reminders") as groupme, \
                self.assertLogs(app.logger, level="ERROR"):
            deadline_reminders.run_reminder_cycle()
            groupme.assert_called_once()

    def test_documented_json_post_accepts_groupme_acknowledgement(self):
        with app.app_context():
            encrypted = bot._cipher().encrypt(b"private-bot-1234").decode()
            response = MagicMock()
            response.__enter__.return_value = response
            for status in (201, 202, 200, 302, 400, 429, 500):
                response.status_code = status
                with patch.object(bot.requests, "post", return_value=response) as post:
                    delivered, error = bot.post_message(encrypted, "📅 Test")
                self.assertEqual(delivered, status in (201, 202))
                post.assert_called_once_with("https://api.groupme.com/v3/bots/post",
                    json={"bot_id": "private-bot-1234", "text": "📅 Test"}, timeout=(3, 10), allow_redirects=False)
                self.assertNotIn("private-bot", error or "")
            with patch.object(bot.requests, "post", side_effect=requests.Timeout("private-bot-1234")):
                self.assertNotIn("private-bot", bot.post_message(encrypted, "📅 Test")[1])
            with patch.object(bot.requests, "post") as post:
                self.assertFalse(bot.post_message("invalid-ciphertext", "📅 Test")[0])
                self.assertFalse(bot.post_message(encrypted, "a" * 1001)[0])
                post.assert_not_called()


del AdminManagementTestCase

if __name__ == "__main__":
    unittest.main()
