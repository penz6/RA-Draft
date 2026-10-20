"""Mocked GroupMe authorization, account confirmation, and bot registration."""

import hashlib
from html import unescape
import re
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

from test_admin_management import AdminManagementTestCase
from core import app, db, migrate_schema
import groupme_oauth as oauth
from groupme_bot import _cipher


class GroupMeOAuthTests(unittest.TestCase):
    base_set_up = AdminManagementTestCase.setUp
    request = AdminManagementTestCase.request
    add_building = AdminManagementTestCase.add_building
    add_user = AdminManagementTestCase.add_user
    login_as = AdminManagementTestCase.login_as

    def setUp(self):
        self.base_set_up()
        self.config_patch = patch.dict(app.config, GROUPME_CLIENT_ID="test-groupme-client")
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.hall = self.add_building()
        self.uid = self.add_user(sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=self.hall)
        self.login_as(self.uid)

    def post(self, path, **data):
        return self.request("post", path, data={"csrf": "admin-management-csrf", **data})

    def start(self):
        response = self.post(f"/buildings/{self.hall}/groupme/connect")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.location.endswith("/groupme/authorize"))
        page = self.request("get", response.location)
        self.assertEqual(page.status_code, 200)
        match = re.search(r'id="groupme-authorize-link" href="([^"]+)"', page.get_data(as_text=True))
        self.assertIsNotNone(match)
        authorization_url = unescape(match.group(1))
        self.assertEqual(urlsplit(authorization_url).netloc, "oauth.groupme.com")
        params = parse_qs(urlsplit(authorization_url).query)
        self.assertEqual(params["client_id"], ["test-groupme-client"])
        return params["state"][0]

    def test_authorization_handoff_keeps_strict_csp_and_fallback_link(self):
        self.start()
        page = self.request("get", "/groupme/authorize?next=https://attacker.example")
        self.assertIn("form-action 'self';", page.headers["Content-Security-Policy"])
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertIn(b"Continue to GroupMe", page.data)
        self.assertIn(b"groupme_authorize.js", page.data)
        self.assertNotIn(b"attacker.example", page.data)

    def test_authorization_handoff_requires_an_active_setup_and_honors_expiry(self):
        self.assertTrue(self.request("get", "/groupme/authorize").location.endswith("/deadlines"))
        self.start()
        with app.app_context():
            db().execute("UPDATE groupme_oauth_flows SET expires_at=0")
            db().commit()
        self.assertTrue(self.request("get", "/groupme/authorize").location.endswith("/deadlines"))

    def test_returning_to_handoff_after_authorization_resumes_group_selection(self):
        self.ready()
        self.assertTrue(self.request("get", "/groupme/authorize").location.endswith("/groupme/select"))

    def complete(self, *, state=None):
        def provider(method, path, token, **kwargs):
            self.assertEqual(token, "temporary-account-token")
            if path == "/users/me":
                return {"id": "123", "name": "Jordan HRA"}
            return [{"id": "456", "name": "Maple Staff <group>"}]
        with patch.object(oauth, "_api", side_effect=provider):
            return self.post("/groupme/oauth/complete", access_token="temporary-account-token", state=state or "")

    def ready(self):
        nonce = self.start()
        self.assertTrue(self.complete(state=nonce).location.endswith("/groupme/select"))
        return nonce

    def test_start_is_csrf_protected_and_building_scoped(self):
        self.assertEqual(self.request("post", f"/buildings/{self.hall}/groupme/connect").status_code, 400)
        other = self.add_building("Other")
        self.assertEqual(self.post(f"/buildings/{other}/groupme/connect").status_code, 403)
        ra = self.add_user(sub="ra", email="ra@rwu.edu", name="RA", building_id=self.hall)
        self.login_as(ra)
        self.assertEqual(self.post(f"/buildings/{self.hall}/groupme/connect").status_code, 403)
        self.login_as(self.uid)
        with patch.dict(app.config, GROUPME_CLIENT_ID=""):
            self.assertTrue(self.post(f"/buildings/{self.hall}/groupme/connect").location.endswith("/deadlines"))

    def test_callback_has_no_token_output_no_referrer_and_no_cache(self):
        self.start()
        response = self.request("get", "/groupme/callback?access_token=temporary-account-token")
        self.assertNotIn(b"temporary-account-token", response.data)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotIn(b"base.html", response.data)
        self.assertIn(b"groupme_oauth_callback.js", response.data)

    def test_documented_callback_without_state_requires_explicit_account_confirmation(self):
        self.start()
        self.complete()
        page = self.request("get", "/groupme/select")
        self.assertIn(b"Jordan HRA", page.data)
        self.assertIn(b"Maple Staff &lt;group&gt;", page.data)
        self.assertIn(b'name="confirm_account"', page.data)
        self.assertNotIn(b"temporary-account-token", page.data)
        self.assertEqual(self.post("/groupme/select", group_id="456").status_code, 400)
        self.assertEqual(self.post("/groupme/select", group_id="999", confirm_account="1").status_code, 400)

    def test_matching_state_and_account_create_active_bot_without_read_callback(self):
        nonce = self.ready()
        with app.app_context():
            flow = db().execute("SELECT * FROM groupme_oauth_flows").fetchone()
            self.assertNotEqual(flow["flow_id"], nonce)
            self.assertNotIn("temporary-account-token", flow["token_ciphertext"])
            self.assertEqual(_cipher().decrypt(flow["token_ciphertext"].encode()), b"temporary-account-token")
        with patch.object(oauth, "_api", return_value={"bot": {"bot_id": "registered-bot-1234"}}) as provider:
            response = self.post("/groupme/select", group_id="456", confirm_account="1")
            provider.assert_called_once_with("POST", "/bots", "temporary-account-token", payload={
                "bot": {"name": "RA Deadlines", "group_id": "456", "active": True},
            })
        self.assertTrue(response.location.endswith(f"/deadlines?groupme_building={self.hall}"))
        with app.app_context():
            saved = db().execute("SELECT * FROM building_groupme_bots").fetchone()
            self.assertEqual(saved["enabled"], 1)
            self.assertEqual(saved["configured_by"], self.uid)
            self.assertEqual(saved["group_name"], "Maple Staff <group>")
            self.assertEqual(_cipher().decrypt(saved["bot_ciphertext"].encode()), b"registered-bot-1234")
            self.assertEqual(db().execute("SELECT COUNT(*) FROM groupme_oauth_flows").fetchone()[0], 0)
            audit = str([tuple(r) for r in db().execute("SELECT details FROM audit_log")])
            self.assertNotIn("token", audit)
            self.assertNotIn("registered-bot", audit)
        with self.client.session_transaction() as session:
            self.assertNotIn("groupme_oauth_flow", session)
            self.assertNotIn("groupme_oauth_state", session)
            self.assertNotIn("temporary-account-token", str(dict(session)))
        with patch.object(oauth, "_api") as provider:
            self.post("/groupme/select", group_id="456", confirm_account="1")
            provider.assert_not_called()

    def test_cancel_deletes_temporary_token_and_never_creates_bot(self):
        self.ready()
        with patch.object(oauth, "_api") as provider:
            self.post("/groupme/select", action="cancel")
            provider.assert_not_called()
        with app.app_context():
            self.assertEqual(db().execute("SELECT COUNT(*) FROM groupme_oauth_flows").fetchone()[0], 0)
            self.assertEqual(db().execute("SELECT COUNT(*) FROM building_groupme_bots").fetchone()[0], 0)

    def test_missing_flow_bad_state_expiry_and_changed_building_are_rejected(self):
        with patch.object(oauth, "_api") as provider:
            self.post("/groupme/oauth/complete", access_token="temporary-account-token")
            self.start()
            self.post("/groupme/oauth/complete", access_token="temporary-account-token", state="wrong-state")
            provider.assert_not_called()
        self.start()
        with app.app_context():
            db().execute("UPDATE groupme_oauth_flows SET expires_at=0")
            db().commit()
        with patch.object(oauth, "_api") as provider:
            self.complete()
            provider.assert_not_called()
        self.ready()
        with app.app_context():
            db().execute("UPDATE users SET building_id=NULL WHERE id=?", (self.uid,))
            db().commit()
        with patch.object(oauth, "_api") as provider:
            self.post("/groupme/select", group_id="456", confirm_account="1")
            provider.assert_not_called()

    def test_provider_failure_clears_account_token_and_does_not_enable_connection(self):
        self.start()
        with patch.object(oauth, "_api", side_effect=oauth.GroupMeError("Unavailable")):
            self.post("/groupme/oauth/complete", access_token="temporary-account-token")
        self.ready()
        with patch.object(oauth, "_api", side_effect=oauth.GroupMeError("Unavailable")):
            self.post("/groupme/select", group_id="456", confirm_account="1")
        with app.app_context():
            self.assertEqual(db().execute("SELECT COUNT(*) FROM groupme_oauth_flows").fetchone()[0], 0)
            self.assertEqual(db().execute("SELECT COUNT(*) FROM building_groupme_bots").fetchone()[0], 0)

    def test_api_uses_header_tokens_and_rejects_redirects_without_exposing_provider_body(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.json.return_value = {"response": []}
        with patch.object(oauth.requests, "request", return_value=response) as provider:
            self.assertEqual(oauth._api("GET", "/groups", "secret-token"), [])
        provider.assert_called_once_with("GET", "https://api.groupme.com/v3/groups",
            headers={"X-Access-Token": "secret-token"}, params=None, json=None, timeout=(3, 10), allow_redirects=False)
        response.status_code = 302
        with patch.object(oauth.requests, "request", return_value=response), self.assertRaises(oauth.GroupMeError):
            oauth._api("GET", "/groups", "secret-token")

    def test_existing_connection_change_aborts_before_provider_write(self):
        self.ready()
        with app.app_context():
            db().execute(
                "INSERT INTO building_groupme_bots(building_id,bot_ciphertext,bot_hash,configured_by) VALUES(?,?,?,?)",
                (self.hall, _cipher().encrypt(b"other-bot-1234").decode(), hashlib.sha256(b"other-bot-1234").hexdigest(), self.uid),
            )
            db().commit()
        with patch.object(oauth, "_api") as provider:
            self.post("/groupme/select", group_id="456", confirm_account="1")
            provider.assert_not_called()

    def test_internal_flow_id_is_independent_of_oauth_state(self):
        nonce = self.start()
        with self.client.session_transaction() as session:
            flow_id = session["groupme_oauth_flow"]
            self.assertEqual(session["groupme_oauth_state"], nonce)
        with app.app_context():
            flow = db().execute("SELECT * FROM groupme_oauth_flows").fetchone()
            self.assertEqual(flow["flow_id"], flow_id)
            self.assertNotIn(nonce, str(tuple(flow)))
        # The authorization server sees the nonce, which cannot select a row.
        with self.client.session_transaction() as session:
            session["groupme_oauth_flow"] = nonce
        self.assertNotIn(b'id="groupme-oauth-complete"', self.request("get", "/groupme/callback").data)

    def test_flow_id_without_session_nonce_or_for_another_user_is_rejected(self):
        nonce = self.start()
        with self.client.session_transaction() as session:
            flow_id = session["groupme_oauth_flow"]
            session.pop("groupme_oauth_state")
        self.assertNotIn(b'id="groupme-oauth-complete"', self.request("get", "/groupme/callback").data)
        other = self.add_user(sub="other-hra", email="other-hra@rwu.edu", name="Other HRA", role="HRA", building_id=self.hall)
        self.login_as(other)
        with self.client.session_transaction() as session:
            session["groupme_oauth_flow"] = flow_id
            session["groupme_oauth_state"] = nonce
        self.assertNotIn(b'id="groupme-oauth-complete"', self.request("get", "/groupme/callback").data)

    def test_restarted_attempt_rejects_old_state_and_clears_both_session_values(self):
        old_state = self.start()
        self.start()
        with patch.object(oauth, "_api") as provider:
            self.post("/groupme/oauth/complete", access_token="temporary-account-token", state=old_state)
            provider.assert_not_called()
        with self.client.session_transaction() as session:
            self.assertNotIn("groupme_oauth_flow", session)
            self.assertNotIn("groupme_oauth_state", session)

    def test_schema_upgrade_preserves_flow_records_and_is_idempotent(self):
        self.start()
        with app.app_context():
            conn = db()
            flow_id = conn.execute("SELECT flow_id FROM groupme_oauth_flows").fetchone()[0]
            conn.execute("ALTER TABLE groupme_oauth_flows RENAME COLUMN flow_id TO state_hash")
            migrate_schema(conn)
            migrate_schema(conn)
            self.assertEqual(conn.execute("SELECT flow_id FROM groupme_oauth_flows").fetchone()[0], flow_id)
            self.assertNotIn("state_hash", {row["name"] for row in conn.execute("PRAGMA table_info(groupme_oauth_flows)")})
            conn.commit()


del AdminManagementTestCase

if __name__ == "__main__":
    unittest.main()
