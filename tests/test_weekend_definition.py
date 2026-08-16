import os
import tempfile
import unittest
from pathlib import Path

TEST_DIR = tempfile.mkdtemp(prefix="ra-draft-weekend-definition-tests-")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(TEST_DIR) / "test.db"))

import portal_app  # noqa: E402,F401
from core import (  # noqa: E402
    DATE_KIND_WEEKDAY,
    DATE_KIND_WEEKEND,
    app,
    db,
    effective_date_kind,
    selectable_dates,
    session_row,
)


class WeekendDefinitionTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
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
            building_id = conn.execute(
                "INSERT INTO buildings(name) VALUES('Maple')"
            ).lastrowid
            hra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("hra", "hra@rwu.edu", "HRA", "HRA", building_id),
            ).lastrowid
            ra_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES(?,?,?,?,?)",
                ("ra", "ra@g.rwu.edu", "RA", "RA", building_id),
            ).lastrowid
            session_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,1,'WEEKDAYS_FIRST',1,?)",
                ("Weekend definition", building_id, "2026-08-14", "2026-08-16", hra_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, ra_id),
            )
            conn.commit()
        self.ra_id = ra_id
        self.session_id = session_id

    def test_friday_and_saturday_are_weekends_and_sunday_is_weekday(self):
        with app.app_context():
            row = session_row(self.session_id)
            self.assertEqual(effective_date_kind(row, "2026-08-14"), DATE_KIND_WEEKEND)
            self.assertEqual(effective_date_kind(row, "2026-08-15"), DATE_KIND_WEEKEND)
            self.assertEqual(effective_date_kind(row, "2026-08-16"), DATE_KIND_WEEKDAY)

    def test_weekdays_first_unlocks_sunday_before_friday_and_saturday(self):
        with app.app_context():
            row = session_row(self.session_id)
            self.assertEqual(selectable_dates(row, self.ra_id), ["2026-08-16"])


if __name__ == "__main__":
    unittest.main()
