"""Exercise GitHub feed failures, school-local dates, and both portals."""

import json
import unittest
from datetime import datetime
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from test_admin_management import AdminManagementTestCase
from core import app, db
from deadlines import _cache, load_deadlines, parse_deadlines
from staff_event_schedule import SCHOOL_TIMEZONE


class DeadlineTests(unittest.TestCase):
    base_set_up = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    login_as = AdminManagementTestCase.login_as

    def setUp(self):
        self.base_set_up()
        self.settings = patch.dict(app.config, DEADLINES_JSON_URL="")
        self.settings.start()
        self.addCleanup(self.settings.stop)
        _cache.clear()

    def add_staff(self, prostaff=False):
        building = self.add_building()
        uid = self.add_user(sub="deadline-user", email="deadline@rwu.edu", name="Alex RA",
                            building_id=building)
        if prostaff:
            with app.app_context():
                db().execute("UPDATE users SET is_prostaff=1,password_must_change=0 WHERE id=?", (uid,))
                db().commit()
        self.login_as(uid)
        return uid

    def feed(self):
        return parse_deadlines([
            {"title": "Later", "due_date": "2026-11-06"},
            {"title": "Report", "due_at": "2026-10-20T01:00:00Z", "description": "Submit report"},
            {"title": "Passed", "due_at": "2026-10-19T08:00:00-04:00"},
            {"title": "Today", "due_date": "2026-10-19"},
        ])

    def render_at_fixed_time(self, path):
        with patch("deadlines.load_deadlines", return_value={"items": self.feed(), "notice": ""}), \
                patch("deadlines.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 19, 12, tzinfo=SCHOOL_TIMEZONE)
            clock.strptime = datetime.strptime
            return self.request("get", path)

    def test_ra_list_replaces_widget_and_excludes_passed_deadlines(self):
        self.add_staff()
        page = self.render_at_fixed_time("/dashboard").get_data(as_text=True)
        self.assertIn('data-card="deadlines"', page)
        self.assertNotIn("One-on-one", page)
        self.assertNotIn("Passed", page)
        self.assertLess(page.index(">Report<"), page.index(">Today<"))
        self.assertLess(page.index(">Today<"), page.index(">Later<"))
        self.assertIn("9:00 PM", page)
        self.assertNotIn("Submit report", page)
        calendar = self.render_at_fixed_time("/deadlines?month=2026-10").get_data(as_text=True)
        self.assertIn("Submit report", calendar)
        self.assertIn("Due today", calendar)

    def test_calendar_month_filter_and_full_list(self):
        self.add_staff()
        page = self.render_at_fixed_time("/deadlines?month=2026-11").get_data(as_text=True)
        calendar, listing = page.split('aria-labelledby="upcoming-deadlines-title"')
        self.assertIn("Upcoming deadlines for November 2026", calendar)
        self.assertNotIn(">Report<", calendar)
        self.assertIn(">Later<", calendar)
        self.assertIn(">Report<", listing)
        self.assertNotIn("Passed", page)
        self.assertIn('month=2026-12', page)
        self.assertIn('month=2026-10', page)
        self.assertIn('aria-current="page"', page)

    def test_invalid_months_fall_back_and_calendar_rolls_over_year(self):
        self.add_staff()
        for month in ("bad", "0001-01", "9999-12"):
            page = self.render_at_fixed_time("/deadlines?month=" + month)
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"October 2026", page.data)
        page = self.render_at_fixed_time("/deadlines?month=2026-12")
        self.assertIn(b"month=2027-01", page.data)

    def test_prostaff_can_open_calendar_and_legacy_routes_are_removed(self):
        self.add_staff(prostaff=True)
        page = self.render_at_fixed_time("/deadlines")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'prostaff-tabs', page.data)
        self.assertNotIn(b"one-on-one", page.data)
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
        self.assertTrue({"schedule_one_on_one", "delete_one_on_one", "prostaff_one_on_ones"}.isdisjoint(endpoints))
        self.assertNotIn(b"one-on-one", self.request("get", "/prostaff").data)

    def test_feed_requires_login_and_preserves_first_login_password_gate(self):
        self.assertEqual(self.request("get", "/deadlines").status_code, 302)
        uid = self.add_staff(prostaff=True)
        with app.app_context():
            db().execute("UPDATE users SET password_must_change=1 WHERE id=?", (uid,))
            db().commit()
        response = self.request("get", "/deadlines")
        self.assertTrue(response.location.endswith("/prostaff/set-password"))

    def test_repository_empty_and_failure_states_are_distinct(self):
        self.add_staff()
        self.assertIn(b'data-card="deadlines"', self.request("get", "/dashboard").data)
        with patch("deadlines.load_deadlines", return_value={"items": [], "notice": ""}):
            self.assertIn(b"No upcoming deadlines", self.request("get", "/dashboard").data)
        with patch("deadlines.load_deadlines", return_value={"items": [], "notice": "Temporarily unavailable"}):
            page = self.request("get", "/dashboard")
            self.assertIn(b"Temporarily unavailable", page.data)
            self.assertNotIn(b"No upcoming deadlines", page.data)

    def test_repository_feed_matches_import_cutoff_and_sources(self):
        feed = json.loads((Path(__file__).resolve().parents[1] / "deadlines.json").read_text())
        self.assertEqual(len(feed["deadlines"]), 28)
        self.assertEqual(len(parse_deadlines(feed)), 28)
        for item in feed["deadlines"]:
            self.assertGreaterEqual(date.fromisoformat(item["due_date"]), date(2026, 10, 19))
            self.assertTrue(item["source"]["file"].endswith(".pdf"))
            self.assertGreater(item["source"]["page"], 0)
        with app.app_context(), patch("deadlines.requests.get") as fetch:
            state = load_deadlines()
            self.assertEqual(len(state["items"]), 28)
            self.assertEqual(state["notice"], "")
            fetch.assert_not_called()

    def test_invalid_repository_feed_shows_unavailable(self):
        with patch("deadlines.REPOSITORY_FEED") as feed:
            feed.read_bytes.side_effect = OSError("Missing file")
            self.assertIn("unavailable", load_deadlines()["notice"])

    def test_feed_validation_and_timezone_conversion(self):
        rows = self.feed()
        self.assertEqual(rows[1]["day"].isoformat(), "2026-10-19")
        self.assertEqual(rows[1]["due"].hour, 21)
        self.assertEqual(rows[2]["due"].hour, 23)
        self.assertEqual(parse_deadlines({"deadlines": []}), [])
        for payload in ({}, [None], [{"title": "Missing date"}],
                        [{"title": "", "due_date": "2026-10-20"}],
                        [{"title": "Bad date", "due_date": "2026-02-30"}],
                        [{"title": "Bad link", "due_date": "2026-10-20", "url": "javascript:alert(1)"}]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_deadlines(payload)

    def test_json_content_is_escaped_in_calendar_and_list(self):
        self.add_staff()
        items = parse_deadlines([{"title": "<script>alert(1)</script>", "due_date": "2099-11-01",
                                  "description": "<img src=x onerror=alert(1)>"}])
        with patch("deadlines.load_deadlines", return_value={"items": items, "notice": ""}):
            page = self.request("get", "/deadlines?month=2099-11").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<img src=x", page)

    def response(self, payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.iter_content.return_value = [json.dumps(payload).encode()]
        return response

    def test_github_fetch_is_cached_and_failed_updates_preserve_last_valid_feed(self):
        app.config["DEADLINES_JSON_URL"] = "https://raw.githubusercontent.com/example/repo/main/deadlines.json"
        response = self.response([{"title": "Report", "due_date": "2026-11-01"}])
        with patch("deadlines.requests.get", return_value=response) as fetch, \
                patch("deadlines.monotonic", return_value=0):
            self.assertEqual(len(load_deadlines()["items"]), 1)
            load_deadlines()
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(fetch.call_args.kwargs["allow_redirects"])
        for failed in (self.response({"bad": "schema"}), self.response([{"title": "Broken entry"}])):
            with patch("deadlines.requests.get", return_value=failed), \
                    patch("deadlines.monotonic", return_value=1000):
                _cache[app.config["DEADLINES_JSON_URL"]]["expires"] = 0
                state = load_deadlines()
                self.assertEqual(state["items"][0]["title"], "Report")
                self.assertIn("last saved", state["notice"])
        with patch("deadlines.requests.get", side_effect=requests.Timeout), \
                patch("deadlines.monotonic", return_value=2000):
            self.assertEqual(load_deadlines()["items"][0]["title"], "Report")

    def test_first_fetch_failure_invalid_source_and_oversized_feed(self):
        for source in ("https://github.com/owner/repo/blob/main/deadlines.json", "http://localhost/data", 
                       "https://raw.githubusercontent.com:bad/file"):
            app.config["DEADLINES_JSON_URL"] = source
            with patch("deadlines.requests.get") as fetch:
                self.assertIn("unavailable", load_deadlines()["notice"])
                fetch.assert_not_called()
        app.config["DEADLINES_JSON_URL"] = "https://raw.githubusercontent.com/example/repo/main/deadlines.json"
        with patch("deadlines.requests.get", side_effect=requests.Timeout):
            self.assertEqual(load_deadlines()["items"], [])
        _cache.clear()
        response = self.response([])
        response.iter_content.return_value = [b"x" * (1024 * 1024 + 1)]
        with patch("deadlines.requests.get", return_value=response):
            self.assertIn("unavailable", load_deadlines()["notice"])


del AdminManagementTestCase
