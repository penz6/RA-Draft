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
    str(Path(tempfile.gettempdir()) / "ra-draft-session-rule-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, dates_for, db  # noqa: E402


class SessionRuleTestCase(unittest.TestCase):
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

    def add_building(self, name="North Hall"):
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

    def login_as(self, user_id, csrf="session-rule-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def create_session_direct(
        self,
        *,
        building_id,
        creator_id,
        participant_ids,
        capacity=2,
        start_date="2026-09-01",
        end_date="2026-09-03",
        date_order="WEEKDAYS_FIRST",
    ):
        with app.app_context():
            conn = db()
            cur = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,"
                "current_position,created_by"
                ") VALUES(?,?,?,?,?,?,1,?)",
                (
                    "Duty Session",
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

    def test_default_date_order_lists_weekdays_before_weekends(self):
        row = {
            "start_date": "2026-08-14",
            "end_date": "2026-08-17",
            "date_order": "WEEKDAYS_FIRST",
        }
        self.assertEqual(
            dates_for(row),
            ["2026-08-16", "2026-08-17", "2026-08-14", "2026-08-15"],
        )

    def test_hra_can_override_date_order(self):
        row = {
            "start_date": "2026-08-14",
            "end_date": "2026-08-17",
            "date_order": "CHRONOLOGICAL",
        }
        self.assertEqual(
            dates_for(row),
            ["2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"],
        )
        row["date_order"] = "WEEKENDS_FIRST"
        self.assertEqual(
            dates_for(row),
            ["2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"],
        )

    def test_hra_and_admin_can_be_participants_and_pick(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="Hall HRA",
            role="HRA",
            building_id=building_id,
        )
        admin_id = self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Duty Admin",
            role="ADMIN",
            building_id=building_id,
        )
        ra_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="Resident Assistant",
            building_id=building_id,
        )
        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            "/sessions",
            data={
                "csrf": csrf,
                "name": "Mixed Role Draft",
                "start_date": "2026-09-01",
                "end_date": "2026-09-03",
                "capacity": "2",
                "date_order": "CHRONOLOGICAL",
                "participant_ids": [str(hra_id), str(admin_id), str(ra_id)],
                f"order_{hra_id}": "1",
                f"order_{admin_id}": "2",
                f"order_{ra_id}": "3",
            },
        )
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            session_item = db().execute(
                "SELECT * FROM draft_sessions WHERE name='Mixed Role Draft'"
            ).fetchone()
            session_id = session_item["id"]
            roles = [
                item["role"]
                for item in db().execute(
                    "SELECT u.role FROM session_order o "
                    "JOIN users u ON u.id=o.user_id WHERE o.session_id=? "
                    "ORDER BY o.position",
                    (session_id,),
                ).fetchall()
            ]
            self.assertEqual(roles, ["HRA", "ADMIN", "RA"])

        response = self.request(
            "post",
            f"/sessions/{session_id}/choose",
            data={"csrf": csrf, "duty_date": "2026-09-01"},
        )
        self.assertEqual(response.status_code, 302)

        admin_csrf = self.login_as(admin_id)
        response = self.request(
            "post",
            f"/sessions/{session_id}/choose",
            data={"csrf": admin_csrf, "duty_date": "2026-09-02"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            assignments = db().execute(
                "SELECT user_id FROM assignments WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            self.assertEqual(
                [item["user_id"] for item in assignments],
                [hra_id, admin_id],
            )

    def test_cross_building_user_is_filtered_and_capacity_is_clamped(self):
        first = self.add_building("First Hall")
        second = self.add_building("Second Hall")
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=first,
        )
        local_ra = self.add_user(
            sub="local-sub",
            email="local@g.rwu.edu",
            name="Local RA",
            building_id=first,
        )
        other_admin = self.add_user(
            sub="other-admin",
            email="other.admin@rwu.edu",
            name="Other Admin",
            role="ADMIN",
            building_id=second,
        )
        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            "/sessions",
            data={
                "csrf": csrf,
                "name": "Scoped Draft",
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
                "capacity": "2",
                "participant_ids": [str(local_ra), str(other_admin)],
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            session_item = db().execute(
                "SELECT id,capacity FROM draft_sessions WHERE name='Scoped Draft'"
            ).fetchone()
            self.assertIsNotNone(session_item)
            participants = db().execute(
                "SELECT user_id FROM session_order WHERE session_id=?",
                (session_item["id"],),
            ).fetchall()
            self.assertEqual([item["user_id"] for item in participants], [local_ra])
            self.assertEqual(session_item["capacity"], 1)

    def test_per_date_capacity_controls_real_picks(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        first_ra = self.add_user(
            sub="first-ra",
            email="first@g.rwu.edu",
            name="First RA",
            building_id=building_id,
        )
        second_ra = self.add_user(
            sub="second-ra",
            email="second@g.rwu.edu",
            name="Second RA",
            building_id=building_id,
        )
        session_id = self.create_session_direct(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[first_ra, second_ra],
            capacity=2,
            start_date="2026-09-01",
            end_date="2026-09-01",
            date_order="CHRONOLOGICAL",
        )

        csrf = self.login_as(hra_id)
        self.request(
            "post",
            f"/sessions/{session_id}/date-capacity",
            data={"csrf": csrf, "duty_date": "2026-09-01", "capacity": "1"},
        )

        first_csrf = self.login_as(first_ra)
        self.request(
            "post",
            f"/sessions/{session_id}/choose",
            data={"csrf": first_csrf, "duty_date": "2026-09-01"},
        )
        second_csrf = self.login_as(second_ra)
        self.request(
            "post",
            f"/sessions/{session_id}/choose",
            data={"csrf": second_csrf, "duty_date": "2026-09-01"},
        )
        with app.app_context():
            count = db().execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE session_id=?",
                (session_id,),
            ).fetchone()["n"]
            self.assertEqual(count, 1)

        hra_csrf = self.login_as(hra_id)
        self.request(
            "post",
            f"/sessions/{session_id}/date-capacity",
            data={"csrf": hra_csrf, "duty_date": "2026-09-01", "capacity": "2"},
        )
        second_csrf = self.login_as(second_ra)
        self.request(
            "post",
            f"/sessions/{session_id}/choose",
            data={"csrf": second_csrf, "duty_date": "2026-09-01"},
        )
        with app.app_context():
            count = db().execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE session_id=?",
                (session_id,),
            ).fetchone()["n"]
            self.assertEqual(count, 2)

    def test_capacity_cannot_be_lowered_below_existing_assignments(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        first_ra = self.add_user(
            sub="first-ra",
            email="first@g.rwu.edu",
            name="First RA",
            building_id=building_id,
        )
        second_ra = self.add_user(
            sub="second-ra",
            email="second@g.rwu.edu",
            name="Second RA",
            building_id=building_id,
        )
        session_id = self.create_session_direct(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=[first_ra, second_ra],
        )
        with app.app_context():
            conn = db()
            for user_id in (first_ra, second_ra):
                conn.execute(
                    "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
                    "VALUES(?,?,?,?)",
                    (session_id, user_id, "2026-09-01", hra_id),
                )
            conn.commit()

        csrf = self.login_as(hra_id)
        self.request(
            "post",
            f"/sessions/{session_id}/date-capacity",
            data={"csrf": csrf, "duty_date": "2026-09-01", "capacity": "1"},
        )
        with app.app_context():
            override = db().execute(
                "SELECT capacity FROM session_date_capacities "
                "WHERE session_id=? AND duty_date=?",
                (session_id, "2026-09-01"),
            ).fetchone()
            self.assertIsNone(override)

    def test_admin_has_hra_capacity_and_order_controls_for_any_building(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        participant_ids = [hra_id]
        for index in range(1, 4):
            participant_ids.append(
                self.add_user(
                    sub=f"ra-{index}",
                    email=f"ra{index}@g.rwu.edu",
                    name=f"RA {index}",
                    building_id=building_id,
                )
            )
        admin_id = self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        session_id = self.create_session_direct(
            building_id=building_id,
            creator_id=hra_id,
            participant_ids=participant_ids,
        )
        csrf = self.login_as(admin_id)
        self.request(
            "post",
            f"/sessions/{session_id}/date-capacity",
            data={"csrf": csrf, "duty_date": "2026-09-01", "capacity": "4"},
        )
        self.request(
            "post",
            f"/sessions/{session_id}/date-order",
            data={"csrf": csrf, "date_order": "WEEKENDS_FIRST"},
        )
        with app.app_context():
            session_item = db().execute(
                "SELECT date_order FROM draft_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            override = db().execute(
                "SELECT capacity FROM session_date_capacities "
                "WHERE session_id=? AND duty_date=?",
                (session_id, "2026-09-01"),
            ).fetchone()
            self.assertEqual(session_item["date_order"], "WEEKENDS_FIRST")
            self.assertEqual(override["capacity"], 4)

    def test_hra_dashboard_lists_all_building_roles_as_participants(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra-sub",
            email="hra@rwu.edu",
            name="Hall HRA",
            role="HRA",
            building_id=building_id,
        )
        self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Building Admin",
            role="ADMIN",
            building_id=building_id,
        )
        self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="Building RA",
            building_id=building_id,
        )
        self.login_as(hra_id)
        page = self.request("get", "/dashboard").get_data(as_text=True)
        self.assertIn('name="participant_ids"', page)
        self.assertIn("Building RA", page)
        self.assertIn("Hall HRA", page)
        self.assertIn("Building Admin", page)
        self.assertIn('name="date_order"', page)
        self.assertIn("draggable=\"true\"", page)


if __name__ == "__main__":
    unittest.main()
