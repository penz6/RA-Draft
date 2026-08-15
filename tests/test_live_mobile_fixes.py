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
    str(Path(tempfile.gettempdir()) / "ra-draft-live-mobile-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402


class LiveMobileFixTestCase(unittest.TestCase):
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
        return getattr(self.client, method)(
            path,
            base_url="https://ci.local",
            **kwargs,
        )

    def add_building(self, name="W"):
        with app.app_context():
            building_id = db().execute(
                "INSERT INTO buildings(name) VALUES(?)",
                (name,),
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

    def login_as(self, user_id, csrf="live-mobile-csrf"):
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
                    "2026-09-01",
                    2,
                    "CHRONOLOGICAL",
                    creator_id,
                ),
            ).lastrowid
            for position, user_id in enumerate(participant_ids, start=1):
                conn.execute(
                    "INSERT INTO session_order(session_id,user_id,position) "
                    "VALUES(?,?,?)",
                    (session_id, user_id, position),
                )
            conn.commit()
            return session_id

    def test_live_versions_change_for_new_sessions_and_turns(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        harry_id = self.add_user(
            sub="harry",
            email="harry@g.rwu.edu",
            name="Harry",
            building_id=building_id,
        )
        penn_id = self.add_user(
            sub="penn",
            email="penn@g.rwu.edu",
            name="Penn",
            building_id=building_id,
        )

        self.login_as(hra_id)
        before_dashboard = self.request("get", "/live-state").get_json()["version"]
        session_id = self.create_session(
            building_id,
            hra_id,
            [harry_id, penn_id],
        )
        after_dashboard = self.request("get", "/live-state").get_json()["version"]
        self.assertNotEqual(before_dashboard, after_dashboard)

        session_page = self.request("get", f"/sessions/{session_id}").get_data(as_text=True)
        self.assertIn("data-live-refresh", session_page)
        self.assertIn(f"/live-state?session_id={session_id}", session_page)
        self.assertIn(f"/live-events?session_id={session_id}", session_page)
        before_turn = self.request(
            "get",
            f"/live-state?session_id={session_id}",
        ).get_json()["version"]

        csrf = self.login_as(harry_id)
        response = self.request(
            "post",
            f"/sessions/{session_id}/choose",
            data={"csrf": csrf, "duty_date": "2026-09-01"},
        )
        self.assertEqual(response.status_code, 302)
        after_turn = self.request(
            "get",
            f"/live-state?session_id={session_id}",
        ).get_json()["version"]
        self.assertNotEqual(before_turn, after_turn)

    def test_live_event_stream_is_authenticated_and_sends_initial_state(self):
        unauthenticated = self.request("get", "/live-events", buffered=False)
        self.assertEqual(unauthenticated.status_code, 401)
        unauthenticated.close()

        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-stream",
            email="stream.hra@rwu.edu",
            name="Stream HRA",
            role="HRA",
            building_id=building_id,
        )
        ra_id = self.add_user(
            sub="ra-stream",
            email="stream.ra@g.rwu.edu",
            name="Stream RA",
            building_id=building_id,
        )
        session_id = self.create_session(building_id, hra_id, [ra_id])
        self.login_as(hra_id)

        response = self.request(
            "get",
            f"/live-events?session_id={session_id}",
            buffered=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        self.assertEqual(response.headers.get("X-Accel-Buffering"), "no")
        self.assertEqual(response.headers.get("Content-Encoding"), "identity")

        stream = iter(response.response)
        retry_chunk = next(stream).decode("utf-8")
        state_chunk = next(stream).decode("utf-8")
        self.assertIn("retry: 1500", retry_chunk)
        self.assertIn("event: state", state_chunk)
        self.assertIn('"version":', state_chunk)
        response.close()

    def test_calendar_summary_uses_first_names_building_star_and_ampersand(self):
        building_id = self.add_building("W")
        hra_id = self.add_user(
            sub="hra",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        harry_id = self.add_user(
            sub="harry",
            email="harry@g.rwu.edu",
            name="Harry Potter",
            building_id=building_id,
        )
        penn_id = self.add_user(
            sub="penn",
            email="penn@g.rwu.edu",
            name="Penn Smith",
            building_id=building_id,
        )
        session_id = self.create_session(
            building_id,
            hra_id,
            [harry_id, penn_id],
        )
        with app.app_context():
            conn = db()
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
                "VALUES(?,?,?,?)",
                (session_id, harry_id, "2026-09-01", hra_id),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
                "VALUES(?,?,?,?)",
                (session_id, penn_id, "2026-09-01", hra_id),
            )
            conn.commit()

        self.login_as(harry_id)
        body = self.request(
            "get",
            f"/calendar/session/{session_id}.ics",
        ).get_data(as_text=True)
        self.assertIn("SUMMARY:W* Harry & Penn", body)
        self.assertNotIn("Potter", body)
        self.assertNotIn("Smith", body)

    def test_mobile_css_keeps_calendar_grid_and_separates_order_controls(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "calendar.css").read_text(encoding="utf-8")
        polling_javascript = (root / "static" / "app.js").read_text(encoding="utf-8")
        event_javascript = (root / "static" / "live_events.js").read_text(encoding="utf-8")
        mobile = css.split("@media(max-width:650px){", 1)[1].split(
            "@media(max-width:420px){",
            1,
        )[0]

        self.assertIn(
            "grid-template-columns:repeat(7,minmax(72px,1fr))",
            mobile,
        )
        self.assertIn(
            "grid-template-columns:32px minmax(0,1fr)",
            mobile,
        )
        self.assertIn(".drag-handle{display:none}", mobile)
        self.assertNotIn(".calendar-grid{display:grid;grid-template-columns:1fr", mobile)
        self.assertIn("window.setInterval(pollLiveState, 2500)", polling_javascript)
        self.assertIn("dataset.liveStateUrl", polling_javascript)
        self.assertIn("new window.EventSource(eventsUrl)", event_javascript)
        self.assertIn("dataset.liveEventsUrl", event_javascript)
        self.assertIn("source.addEventListener(\"update\"", event_javascript)


if __name__ == "__main__":
    unittest.main()
