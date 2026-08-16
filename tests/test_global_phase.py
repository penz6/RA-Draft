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
    str(Path(tempfile.gettempdir()) / "ra-draft-global-phase-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db, next_picker, selectable_dates, session_row  # noqa: E402


class GlobalPhaseTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
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

    def test_participant_is_skipped_when_only_other_people_can_fill_weekday_phase(self):
        with app.app_context():
            conn = db()
            building_id = conn.execute(
                "INSERT INTO buildings(name) VALUES(?)",
                ("Maple",),
            ).lastrowid
            hra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("hra", "hra@rwu.edu", "HRA", "HRA", building_id),
            ).lastrowid
            alex_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("alex", "alex@g.rwu.edu", "Alex", "RA", building_id),
            ).lastrowid
            blair_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("blair", "blair@g.rwu.edu", "Blair", "RA", building_id),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,?,?,1,?)",
                (
                    "Global phase",
                    building_id,
                    "2026-08-14",
                    "2026-08-17",
                    2,
                    "WEEKDAYS_FIRST",
                    hra_id,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, alex_id),
            )
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,2)",
                (session_id, blair_id),
            )
            for user_id, duty_date in (
                (alex_id, "2026-08-16"),
                (alex_id, "2026-08-17"),
                (blair_id, "2026-08-16"),
            ):
                conn.execute(
                    "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
                    "VALUES(?,?,?,?)",
                    (session_id, user_id, duty_date, hra_id),
                )
            conn.commit()

            row = session_row(session_id)
            self.assertEqual(selectable_dates(row, alex_id), [])
            self.assertEqual(selectable_dates(row, blair_id), ["2026-08-17"])
            self.assertEqual(next_picker(session_id)["id"], blair_id)


if __name__ == "__main__":
    unittest.main()
