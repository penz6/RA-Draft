import os
import sqlite3
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
    str(Path(tempfile.gettempdir()) / "ra-draft-round-robin-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import (  # noqa: E402
    app,
    configure_connection,
    db,
    migrate_schema,
    next_picker,
    selectable_dates,
    session_complete,
    session_row,
)


class RoundRobinCalendarTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
        with app.app_context():
            conn = db()
            for table in (
                "audit_log",
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

    def add_building(self, name="Maple Hall"):
        with app.app_context():
            cur = db().execute("INSERT INTO buildings(name) VALUES(?)", (name,))
            db().commit()
            return cur.lastrowid

    def add_user(self, *, sub, email, name, role="RA", building_id=None):
        with app.app_context():
            cur = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                (sub, email, name, role, building_id),
            )
            db().commit()
            return cur.lastrowid

    def login_as(self, user_id, csrf="round-robin-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def create_session(
        self,
        *,
        building_id,
        creator_id,
        participant_ids,
        capacity=2,
        start_date="2026-09-01",
        end_date="2026-09-02",
        date_order="CHRONOLOGICAL",
    ):
        with app.app_context():
            conn = db()
            cur = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,?,?,1,?)",
                (
                    "Maple Duty",
                    building_id,
                    start_date,
                    end_date,
                    capacity,
                    date_order,
                    creator_id,
                ),
            )
            session_id = cur.lastrowid
            for position, user_id in enumerate(participant_ids, start=1):
                conn.execute(
                    "INSERT INTO session_order(session_id,user_id,position) "
                    "VALUES(?,?,?)",
                    (session_id, user_id, position),
                )
            conn.commit()
            return session_id

    def pick(self, *, user_id, session_id, duty_date):
        csrf = self.login_as(user_id)
        return self.request(
            "post",
            f"/sessions/{session_id}/choose",
            data={"csrf": csrf, "duty_date": duty_date},
        )

    def test_round_robin_repeats_until_every_slot_is_filled(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=building_id
        )
        alex_id = self.add_user(
            sub="alex", email="alex@g.rwu.edu", name="Alex", building_id=building_id
        )
        blair_id = self.add_user(
            sub="blair", email="blair@g.rwu.edu", name="Blair", building_id=building_id
        )
        session_id = self.create_session(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[alex_id, blair_id],
            capacity=2,
        )

        self.assertEqual(self.pick(user_id=alex_id, session_id=session_id, duty_date="2026-09-01").status_code, 302)
        with app.app_context():
            self.assertEqual(next_picker(session_id)["id"], blair_id)

        self.assertEqual(self.pick(user_id=blair_id, session_id=session_id, duty_date="2026-09-01").status_code, 302)
        with app.app_context():
            self.assertEqual(next_picker(session_id)["id"], alex_id)

        self.assertEqual(self.pick(user_id=alex_id, session_id=session_id, duty_date="2026-09-02").status_code, 302)
        with app.app_context():
            self.assertEqual(next_picker(session_id)["id"], blair_id)

        self.assertEqual(self.pick(user_id=blair_id, session_id=session_id, duty_date="2026-09-02").status_code, 302)
        with app.app_context():
            row = session_row(session_id)
            self.assertTrue(session_complete(row))
            self.assertIsNone(next_picker(session_id))
            counts = {
                item["user_id"]: item["n"]
                for item in db().execute(
                    "SELECT user_id,COUNT(*) AS n FROM assignments "
                    "WHERE session_id=? GROUP BY user_id",
                    (session_id,),
                ).fetchall()
            }
            self.assertEqual(counts, {alex_id: 2, blair_id: 2})

    def test_weekday_first_blocks_weekends_until_weekdays_fill(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=building_id
        )
        alex_id = self.add_user(
            sub="alex", email="alex@g.rwu.edu", name="Alex", building_id=building_id
        )
        blair_id = self.add_user(
            sub="blair", email="blair@g.rwu.edu", name="Blair", building_id=building_id
        )
        session_id = self.create_session(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[alex_id, blair_id],
            capacity=1,
            start_date="2026-08-14",
            end_date="2026-08-17",
            date_order="WEEKDAYS_FIRST",
        )

        with app.app_context():
            row = session_row(session_id)
            self.assertEqual(
                selectable_dates(row, alex_id),
                ["2026-08-14", "2026-08-17"],
            )

        self.pick(user_id=alex_id, session_id=session_id, duty_date="2026-08-15")
        with app.app_context():
            self.assertEqual(
                db().execute(
                    "SELECT COUNT(*) AS n FROM assignments WHERE session_id=?",
                    (session_id,),
                ).fetchone()["n"],
                0,
            )

        self.pick(user_id=alex_id, session_id=session_id, duty_date="2026-08-14")
        self.pick(user_id=blair_id, session_id=session_id, duty_date="2026-08-17")
        with app.app_context():
            row = session_row(session_id)
            self.assertEqual(
                selectable_dates(row, alex_id),
                ["2026-08-15", "2026-08-16"],
            )

    def test_manager_pick_consumes_current_turn_and_skip_is_one_round(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=building_id
        )
        alex_id = self.add_user(
            sub="alex", email="alex@g.rwu.edu", name="Alex", building_id=building_id
        )
        blair_id = self.add_user(
            sub="blair", email="blair@g.rwu.edu", name="Blair", building_id=building_id
        )
        session_id = self.create_session(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[alex_id, blair_id],
            capacity=1,
        )
        csrf = self.login_as(hra_id)

        response = self.request(
            "post",
            f"/sessions/{session_id}/assign",
            data={"csrf": csrf, "user_id": alex_id, "duty_date": "2026-09-01"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(next_picker(session_id)["id"], blair_id)

        response = self.request(
            "post",
            f"/sessions/{session_id}/skip/{blair_id}",
            data={"csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(next_picker(session_id)["id"], alex_id)
            event = db().execute(
                "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event["action"], "draft.turn.skip")

    def test_manager_assignment_restores_a_paused_participant(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=building_id
        )
        alex_id = self.add_user(
            sub="alex", email="alex@g.rwu.edu", name="Alex", building_id=building_id
        )
        blair_id = self.add_user(
            sub="blair", email="blair@g.rwu.edu", name="Blair", building_id=building_id
        )
        session_id = self.create_session(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[alex_id, blair_id],
            capacity=1,
        )
        csrf = self.login_as(hra_id)
        self.request(
            "post",
            f"/sessions/{session_id}/pause/{blair_id}",
            data={"csrf": csrf},
        )
        self.request(
            "post",
            f"/sessions/{session_id}/assign",
            data={"csrf": csrf, "user_id": blair_id, "duty_date": "2026-09-02"},
        )
        with app.app_context():
            paused = db().execute(
                "SELECT 1 FROM session_deferrals WHERE session_id=? AND user_id=?",
                (session_id, blair_id),
            ).fetchone()
            self.assertIsNone(paused)
            assignment = db().execute(
                "SELECT 1 FROM assignments WHERE session_id=? AND user_id=? AND duty_date=?",
                (session_id, blair_id, "2026-09-02"),
            ).fetchone()
            self.assertIsNotNone(assignment)

    def test_calendar_page_shows_names_and_manager_actions(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=building_id
        )
        alex_id = self.add_user(
            sub="alex", email="alex@g.rwu.edu", name="Alex", building_id=building_id
        )
        blair_id = self.add_user(
            sub="blair", email="blair@g.rwu.edu", name="Blair", building_id=building_id
        )
        session_id = self.create_session(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[alex_id, blair_id],
        )
        with app.app_context():
            conn = db()
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, alex_id, "2026-09-01", hra_id),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, blair_id, "2026-09-01", hra_id),
            )
            conn.commit()

        self.login_as(hra_id)
        page = self.request("get", f"/sessions/{session_id}").get_data(as_text=True)
        self.assertIn("data-duty-calendar", page)
        self.assertIn("data-calendar-day", page)
        self.assertIn("Alex", page)
        self.assertIn("Blair", page)
        self.assertIn("Pick for them", page)
        self.assertIn("Skip once", page)
        self.assertIn("Download session iCal", page)
        self.assertNotIn("Reference hours", page)

    def test_session_ical_uses_one_all_day_event_per_assigned_date(self):
        building_id = self.add_building("Maple")
        hra_id = self.add_user(
            sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=building_id
        )
        alex_id = self.add_user(
            sub="alex", email="alex@g.rwu.edu", name="Alex", building_id=building_id
        )
        blair_id = self.add_user(
            sub="blair", email="blair@g.rwu.edu", name="Blair", building_id=building_id
        )
        session_id = self.create_session(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[alex_id, blair_id],
        )
        with app.app_context():
            conn = db()
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, alex_id, "2026-09-01", hra_id),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, blair_id, "2026-09-01", hra_id),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session_id, alex_id, "2026-09-02", hra_id),
            )
            conn.commit()

        self.login_as(alex_id)
        response = self.request("get", f"/calendar/session/{session_id}.ics")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body.count("BEGIN:VEVENT"), 2)
        self.assertIn("SUMMARY:Maple: Alex and Blair", body)
        self.assertIn("SUMMARY:Maple: Alex", body)
        self.assertIn("DTSTART;VALUE=DATE:20260901", body)
        self.assertNotIn("Reference hours", body)

    def test_session_setup_is_drag_drop_and_mobile_friendly_without_times(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra", email="hra@rwu.edu", name="HRA", role="HRA", building_id=building_id
        )
        self.add_user(
            sub="alex", email="alex@g.rwu.edu", name="Alex", building_id=building_id
        )
        self.login_as(hra_id)
        page = self.request("get", "/dashboard").get_data(as_text=True)
        self.assertIn('draggable="true"', page)
        self.assertIn("data-move-up", page)
        self.assertIn("data-move-down", page)
        self.assertIn('name="date_order"', page)
        self.assertNotIn("Reference start time", page)
        self.assertNotIn("Reference end time", page)

        root = Path(__file__).resolve().parents[1]
        css = (root / "static" / "calendar.css").read_text(encoding="utf-8")
        javascript = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:650px)", css)
        self.assertIn("grid-template-columns:1fr", css)
        self.assertIn("data-move-up", javascript)
        self.assertIn("data-move-down", javascript)

    def test_legacy_assignment_constraint_migrates_without_losing_data(self):
        conn = configure_connection(sqlite3.connect(":memory:"))
        conn.executescript(
            """
            CREATE TABLE buildings (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE users (
              id INTEGER PRIMARY KEY,
              google_sub TEXT NOT NULL,
              email TEXT NOT NULL,
              name TEXT NOT NULL,
              role TEXT NOT NULL,
              building_id INTEGER
            );
            CREATE TABLE draft_sessions (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              building_id INTEGER NOT NULL,
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              shift_start TEXT NOT NULL DEFAULT '19:00',
              shift_end TEXT NOT NULL DEFAULT '07:00',
              capacity INTEGER NOT NULL DEFAULT 2,
              created_by INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'OPEN',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE session_order (
              session_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              position INTEGER NOT NULL,
              PRIMARY KEY(session_id,user_id),
              UNIQUE(session_id,position)
            );
            CREATE TABLE assignments (
              id INTEGER PRIMARY KEY,
              session_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              duty_date TEXT NOT NULL,
              created_by INTEGER NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(session_id,user_id)
            );
            CREATE TABLE session_date_capacities (
              session_id INTEGER NOT NULL,
              duty_date TEXT NOT NULL,
              capacity INTEGER NOT NULL,
              updated_by INTEGER NOT NULL,
              PRIMARY KEY(session_id,duty_date)
            );
            INSERT INTO buildings VALUES(1,'Maple');
            INSERT INTO users VALUES(1,'sub','ra@g.rwu.edu','RA','RA',1);
            INSERT INTO draft_sessions(
              id,name,building_id,start_date,end_date,capacity,created_by
            ) VALUES(1,'Legacy',1,'2026-09-01','2026-09-02',2,1);
            INSERT INTO session_order VALUES(1,1,1);
            INSERT INTO assignments(
              id,session_id,user_id,duty_date,created_by
            ) VALUES(1,1,1,'2026-09-01',1);
            """
        )
        migrate_schema(conn)
        conn.execute(
            "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
            "VALUES(1,1,'2026-09-02',1)"
        )
        count = conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]
        session_item = conn.execute(
            "SELECT date_order,current_position,capacity FROM draft_sessions WHERE id=1"
        ).fetchone()
        self.assertEqual(count, 2)
        self.assertEqual(session_item["date_order"], "WEEKDAYS_FIRST")
        self.assertEqual(session_item["current_position"], 1)
        self.assertEqual(session_item["capacity"], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
