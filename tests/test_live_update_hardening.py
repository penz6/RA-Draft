import os
import queue
import re
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
    str(Path(tempfile.gettempdir()) / "ra-draft-live-hardening-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402
from live_updates import (  # noqa: E402
    live_event_broker,
    publish_live_topics,
    topics_for_logout,
    topics_for_session,
)


class LiveUpdateHardeningTestCase(unittest.TestCase):
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
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

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
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                (sub, email, name, role, building_id),
            ).lastrowid
            db().commit()
            return user_id

    def login_as(self, user_id, csrf="live-hardening-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def create_session(self, building_id, creator_id, participant_ids):
        with app.app_context():
            conn = db()
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,?,?,1,?)",
                (
                    "Live duty",
                    building_id,
                    "2026-09-01",
                    "2026-09-02",
                    1,
                    "CHRONOLOGICAL",
                    creator_id,
                ),
            ).lastrowid
            for position, user_id in enumerate(participant_ids, start=1):
                conn.execute(
                    "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,?)",
                    (session_id, user_id, position),
                )
            conn.commit()
            return session_id

    def test_topic_broker_only_wakes_matching_session(self):
        first = live_event_broker.subscribe(topics_for_session(1))
        second = live_event_broker.subscribe(topics_for_session(2))
        try:
            publish_live_topics(*topics_for_session(1))
            self.assertIn("session:1", first.get(timeout=0.5))
            with self.assertRaises(queue.Empty):
                second.get(timeout=0.05)
        finally:
            live_event_broker.unsubscribe(first)
            live_event_broker.unsubscribe(second)

    def test_versions_include_names_roles_and_building_names(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        ra_id = self.add_user(
            sub="ra",
            email="ra@g.rwu.edu",
            name="Original Name",
            building_id=building_id,
        )
        session_id = self.create_session(building_id, hra_id, [ra_id])
        self.login_as(hra_id)
        dashboard_before = self.request("get", "/live-state").get_json()["version"]
        session_before = self.request(
            "get", f"/live-state?session_id={session_id}"
        ).get_json()["version"]

        with app.app_context():
            db().execute("UPDATE users SET name='Changed Name',role='HRA' WHERE id=?", (ra_id,))
            db().execute("UPDATE buildings SET name='Renamed Maple' WHERE id=?", (building_id,))
            db().commit()

        self.assertNotEqual(
            dashboard_before,
            self.request("get", "/live-state").get_json()["version"],
        )
        self.assertNotEqual(
            session_before,
            self.request("get", f"/live-state?session_id={session_id}").get_json()["version"],
        )

    def test_only_real_commits_publish(self):
        building_id = self.add_building()
        admin_id = self.add_user(
            sub="admin",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        csrf = self.login_as(admin_id)
        subscriber = live_event_broker.subscribe()
        try:
            response = self.request(
                "post",
                f"/admin/buildings/{building_id}/rename",
                data={"csrf": csrf, "name": "Maple"},
            )
            self.assertEqual(response.status_code, 302)
            with self.assertRaises(queue.Empty):
                subscriber.get(timeout=0.05)

            response = self.request(
                "post",
                f"/admin/buildings/{building_id}/rename",
                data={"csrf": csrf, "name": "Maple Hall"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("dashboard:all", subscriber.get(timeout=0.5))
        finally:
            live_event_broker.unsubscribe(subscriber)

    def test_committed_pick_and_logout_publish_scoped_topics(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-pick",
            email="hra.pick@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        ra_id = self.add_user(
            sub="ra-pick",
            email="ra.pick@g.rwu.edu",
            name="RA",
            building_id=building_id,
        )
        session_id = self.create_session(building_id, hra_id, [ra_id])
        csrf = self.login_as(ra_id)
        session_subscriber = live_event_broker.subscribe(topics_for_session(session_id))
        try:
            response = self.request(
                "post",
                f"/sessions/{session_id}/choose",
                data={"csrf": csrf, "duty_date": "2026-09-01"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn(
                f"session:{session_id}", session_subscriber.get(timeout=0.5)
            )
        finally:
            live_event_broker.unsubscribe(session_subscriber)

        logout_subscriber = live_event_broker.subscribe(topics_for_logout(ra_id))
        try:
            csrf = self.login_as(ra_id)
            response = self.request("post", "/logout", data={"csrf": csrf})
            self.assertEqual(response.status_code, 302)
            self.assertIn(f"logout:{ra_id}", logout_subscriber.get(timeout=0.5))
        finally:
            live_event_broker.unsubscribe(logout_subscriber)

    def test_stream_headers_and_initial_event(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="stream-hra",
            email="stream.hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        self.login_as(hra_id)
        response = self.request("get", "/live-events", buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        cache_control = response.headers.get("Cache-Control", "")
        self.assertIn("no-store", cache_control)
        self.assertIn("no-transform", cache_control)
        self.assertEqual(response.headers.get("X-Accel-Buffering"), "no")
        self.assertEqual(response.headers.get("Content-Encoding"), "identity")
        first_chunk = next(iter(response.response)).decode("utf-8")
        self.assertIn("retry: 1500", first_chunk)
        self.assertIn("event: state", first_chunk)
        response.close()

    def test_rendered_session_version_matches_authorized_live_state(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="version-hra",
            email="version.hra@rwu.edu",
            name="Version HRA",
            role="HRA",
            building_id=building_id,
        )
        ra_id = self.add_user(
            sub="version-ra",
            email="version.ra@g.rwu.edu",
            name="Version RA",
            building_id=building_id,
        )
        session_id = self.create_session(building_id, hra_id, [hra_id, ra_id])
        self.login_as(hra_id)

        page = self.request("get", f"/sessions/{session_id}")
        self.assertEqual(page.status_code, 200)
        match = re.search(
            r'data-live-version="([0-9a-f]{64})"',
            page.get_data(as_text=True),
        )
        self.assertIsNotNone(match)

        live_state = self.request(
            "get", f"/live-state?session_id={session_id}"
        ).get_json()["version"]
        self.assertEqual(match.group(1), live_state)

    def test_source_guards_lifecycles_and_finite_worker_timeout(self):
        root = Path(__file__).resolve().parents[1]
        server = (root / "live_updates.py").read_text(encoding="utf-8")
        client = (root / "static" / "live_stream.js").read_text(encoding="utf-8")
        base = (root / "templates" / "base.html").read_text(encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

        self.assertLess(
            server.index("subscriber = live_event_broker.subscribe"),
            server.index("initial_version = _authorized_version"),
        )
        self.assertIn("SSE_MAX_CONNECTION_SECONDS = 300", server)
        self.assertIn("set_trace_callback(trace)", server)
        self.assertIn("formSnapshots", client)
        self.assertIn("dirtyForms", client)
        self.assertIn("live-update-notice", client)
        self.assertIn("disconnectStream();", client)
        self.assertIn('document.addEventListener("visibilitychange"', client)
        self.assertIn('removeAttribute("data-live-refresh")', client)
        self.assertLess(base.index("live_stream.js"), base.index("app.js"))
        self.assertIn("--timeout 60", dockerfile)
        self.assertIn("--graceful-timeout 30", dockerfile)
        self.assertNotIn("--timeout 0", dockerfile)


if __name__ == "__main__":
    unittest.main()
