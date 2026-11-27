import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.mkdtemp()) / "prostaff.db"))

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402
from prostaff import _duty_display_date, consolidate_duty_schedule  # noqa: E402
from staff_event_schedule import SCHOOL_TIMEZONE  # noqa: E402


class ProstaffTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SERVER_NAME="ci.local")
        self.client = app.test_client()
        with app.app_context():
            conn = db()
            for table in ("audit_log", "assignments", "session_order", "draft_sessions", "users", "buildings"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url="https://ci.local", **kwargs)

    def login_as(self, uid, csrf="csrf-token"):
        with self.client.session_transaction() as sess:
            sess["uid"], sess["csrf"] = uid, csrf

    def test_only_google_sign_in_is_available(self):
        page = self.request("get", "/")
        self.assertIn(b"Continue with Google", page.data)
        self.assertNotIn(b"Prostaff sign in", page.data)
        for path in ("/prostaff/login", "/prostaff/set-password"):
            for method in ("get", "post"):
                self.assertEqual(self.request(method, path).status_code, 404)
        for path in ("/admin/prostaff", "/admin/prostaff/1/reset-password"):
            self.assertEqual(self.request("post", path).status_code, 404)

    def test_prostaff_is_redirected_away_from_ra_routes_and_can_search_schedule(self):
        with app.app_context():
            conn = db()
            building = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            admin = conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('admin','admin@rwu.edu','Admin','ADMIN')").lastrowid
            ra = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra','ra@rwu.edu','Alex RA','RA',?)", (building,)).lastrowid
            staff = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) VALUES('ps','ps@example.edu','Pro Staff','RA',?,1,0)", (building,)).lastrowid
            draft = conn.execute("INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) VALUES('Fall',?,?,?,?, 'CLOSED')", (building, date.today().isoformat(), date.today().isoformat(), admin)).lastrowid
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft, ra, date.today().isoformat(), admin))
            conn.commit()
        self.login_as(staff)
        self.assertTrue(self.request("get", "/dashboard").location.endswith("/prostaff"))
        page = self.request("get", "/prostaff?q=Alex").get_data(as_text=True)
        self.assertIn("Alex RA", page)
        self.assertIn("Maple", page)
        self.assertNotIn("Duty Swaps", page)

        calendar = self.request("get", f"/prostaff/schedule?month={date.today():%Y-%m}")
        self.assertEqual(calendar.status_code, 200)
        self.assertIn(b"Duty schedule for", calendar.data)
        self.assertIn(b"Alex RA", calendar.data)
        deadlines = self.request("get", "/deadlines")
        self.assertEqual(deadlines.status_code, 200)
        self.assertIn(b"Upcoming deadlines for", deadlines.data)

    def test_prostaff_duty_swaps_are_read_only_and_building_scoped(self):
        with app.app_context():
            conn = db()
            maple = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            cedar = conn.execute("INSERT INTO buildings(name) VALUES('Cedar')").lastrowid
            admin = conn.execute(
                "INSERT INTO users(google_sub,email,name,role) "
                "VALUES('swap-admin','swap-admin@rwu.edu','Swap Admin','ADMIN')"
            ).lastrowid
            staff = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('swap-staff','swap-staff@rwu.edu','Area Coordinator','RA',?,1,0)",
                (maple,),
            ).lastrowid
            admin_lite = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,admin_lite) "
                "VALUES('swap-admin-lite','swap-admin-lite@rwu.edu','Maple Admin Lite','HRA',?,1)",
                (maple,),
            ).lastrowid

            maple_requester = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('maple-requester','maple-requester@rwu.edu','Maple Requester','RA',?)",
                (maple,),
            ).lastrowid
            maple_target = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('maple-target','maple-target@rwu.edu','Maple Target','RA',?)",
                (maple,),
            ).lastrowid
            cedar_requester = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('cedar-requester','cedar-requester@rwu.edu','Cedar Requester','RA',?)",
                (cedar,),
            ).lastrowid
            cedar_target = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('cedar-target','cedar-target@rwu.edu','Cedar Target','RA',?)",
                (cedar,),
            ).lastrowid

            maple_session = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('Maple Fall',?,'2026-10-01','2026-10-31',?,'CLOSED')",
                (maple, admin),
            ).lastrowid
            cedar_session = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('Cedar Fall',?,'2026-10-01','2026-10-31',?,'CLOSED')",
                (cedar, admin),
            ).lastrowid

            maple_req_assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (maple_session, maple_requester, "2026-10-10", admin),
            ).lastrowid
            maple_target_assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (maple_session, maple_target, "2026-10-11", admin),
            ).lastrowid
            cedar_req_assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (cedar_session, cedar_requester, "2026-10-12", admin),
            ).lastrowid
            cedar_target_assignment = conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (cedar_session, cedar_target, "2026-10-13", admin),
            ).lastrowid

            conn.execute(
                "INSERT INTO duty_swap_requests("
                "session_id,requester_user_id,requester_assignment_id,target_user_id,"
                "target_assignment_id,status,batch_id"
                ") VALUES(?,?,?,?,?,'PENDING','maple-batch')",
                (
                    maple_session,
                    maple_requester,
                    maple_req_assignment,
                    maple_target,
                    maple_target_assignment,
                ),
            )
            conn.execute(
                "INSERT INTO duty_swap_requests("
                "session_id,requester_user_id,requester_assignment_id,target_user_id,"
                "target_assignment_id,status,batch_id,reviewed_by,reviewed_at,target_reviewed_at,created_at"
                ") VALUES(?,?,?,?,?,'APPROVED','fast-approved',?,CURRENT_TIMESTAMP,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (
                    maple_session,
                    maple_requester,
                    maple_req_assignment,
                    maple_target,
                    maple_target_assignment,
                    admin,
                ),
            )
            manual_swap = conn.execute(
                "INSERT INTO duty_swap_requests("
                "session_id,requester_user_id,requester_assignment_id,target_user_id,"
                "target_assignment_id,status,batch_id,reviewed_by,reviewed_at,target_reviewed_at,created_at"
                ") VALUES(?,?,?,?,?,'APPROVED','manual-swap',?,CURRENT_TIMESTAMP,"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (
                    maple_session,
                    maple_requester,
                    maple_req_assignment,
                    maple_target,
                    maple_target_assignment,
                    admin,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO audit_log(actor_user_id,action,target_type,target_id) "
                "VALUES(?,'swap.manager_manual','swap_request',?)",
                (admin, manual_swap),
            )
            conn.execute(
                "INSERT INTO duty_swap_requests("
                "session_id,requester_user_id,requester_assignment_id,target_user_id,"
                "target_assignment_id,status,batch_id"
                ") VALUES(?,?,?,?,?,'APPROVED','cedar-batch')",
                (
                    cedar_session,
                    cedar_requester,
                    cedar_req_assignment,
                    cedar_target,
                    cedar_target_assignment,
                ),
            )
            conn.commit()

        self.login_as(staff)
        response = self.request("get", "/prostaff/swaps")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Duty swaps", html)
        self.assertIn("Maple duty swaps", html)
        self.assertIn("Maple Requester", html)
        self.assertIn("Maple Target", html)
        self.assertIn("Waiting for recipient", html)
        self.assertIn("Manual swap by Swap Admin", html)
        self.assertIn("Requested by Maple Requester", html)
        self.assertIn("Oct 10", html)
        self.assertIn("Oct 11", html)
        self.assertNotIn("Cedar Requester", html)
        self.assertNotIn("Cedar Target", html)
        self.assertNotIn("Approve request", html)
        self.assertNotIn("Reject", html)

        self.login_as(admin_lite)
        admin_lite_response = self.request("get", "/prostaff/swaps")
        self.assertEqual(admin_lite_response.status_code, 200)
        admin_lite_html = admin_lite_response.get_data(as_text=True)
        self.assertIn("Maple Requester", admin_lite_html)
        self.assertNotIn("Cedar Requester", admin_lite_html)

        self.login_as(maple_requester)
        self.assertEqual(self.request("get", "/prostaff/swaps").status_code, 403)

    def test_duty_roster_uses_previous_date_until_eight_am(self):
        self.assertEqual(
            _duty_display_date(datetime(2026, 10, 15, 7, 59, tzinfo=SCHOOL_TIMEZONE)),
            date(2026, 10, 14),
        )
        self.assertEqual(
            _duty_display_date(datetime(2026, 10, 15, 8, 0, tzinfo=SCHOOL_TIMEZONE)),
            date(2026, 10, 15),
        )

    def test_early_morning_dashboard_labels_and_shows_previous_duty_night(self):
        with app.app_context():
            conn = db()
            building = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            admin = conn.execute(
                "INSERT INTO users(google_sub,email,name,role) VALUES('night-admin','night-admin@rwu.edu','Admin','ADMIN')"
            ).lastrowid
            prior_ra = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES('prior-ra','prior@rwu.edu','Prior Night RA','RA',?)",
                (building,),
            ).lastrowid
            current_ra = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES('current-ra','current@rwu.edu','Current Night RA','RA',?)",
                (building,),
            ).lastrowid
            draft = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('October',?,'2026-10-01','2026-10-31',?,'CLOSED')",
                (building, admin),
            ).lastrowid
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (draft, prior_ra, "2026-10-14", admin),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (draft, current_ra, "2026-10-15", admin),
            )
            conn.commit()
        self.login_as(admin)

        class EarlyMorning(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 15, 7, 30, tzinfo=tz)

        with patch("prostaff.datetime", EarlyMorning):
            page = self.request("get", "/prostaff")
        self.assertIn(b"Prior Night RA", page.data)
        self.assertNotIn(b"Current Night RA", page.data)
        self.assertIn(b"Wed, Oct 14", page.data)
        self.assertIn(b"previous duty night until 8:00 AM", page.data)

    def test_consolidate_duty_schedule_helper(self):
        # Empty input
        self.assertEqual(consolidate_duty_schedule([]), {})

        # Single staff
        rows = [
            {"duty_date": "2026-10-15", "building_id": 1, "building_name": "Maple", "name": "Jane Doe", "email": "jdoe@rwu.edu"}
        ]
        res = consolidate_duty_schedule(rows)
        self.assertIn(15, res)
        self.assertEqual(len(res[15]), 1)
        self.assertEqual(res[15][0]["building_name"], "Maple")
        self.assertEqual(res[15][0]["names"], "Jane Doe")
        self.assertEqual(res[15][0]["search_terms"], "Jane Doe jdoe@rwu.edu")

        # Two staff in same building
        rows = [
            {"duty_date": "2026-10-15", "building_id": 1, "building_name": "Maple", "name": "Jane Doe", "email": "jdoe@rwu.edu"},
            {"duty_date": "2026-10-15", "building_id": 1, "building_name": "Maple", "name": "John Smith", "email": "jsmith@rwu.edu"},
        ]
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[15]), 1)
        self.assertEqual(res[15][0]["names"], "Jane Doe & John Smith")
        self.assertEqual(res[15][0]["search_terms"], "Jane Doe jdoe@rwu.edu John Smith jsmith@rwu.edu")

        # Duplicate row for same staff
        rows.append({"duty_date": "2026-10-15", "building_id": 1, "building_name": "Maple", "name": "Jane Doe", "email": "jdoe@rwu.edu"})
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[15]), 1)
        self.assertEqual(res[15][0]["names"], "Jane Doe & John Smith")

        # Multiple buildings on same date
        rows.append({"duty_date": "2026-10-15", "building_id": 2, "building_name": "Cedar", "name": "Taylor RA", "email": "taylor@rwu.edu"})
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[15]), 2)
        self.assertEqual(res[15][1]["building_name"], "Cedar")
        self.assertEqual(res[15][1]["names"], "Taylor RA")

    def test_duty_calendar_building_entry_consolidation_and_time_omission(self):
        with app.app_context():
            conn = db()
            b_maple = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            b_cedar = conn.execute("INSERT INTO buildings(name) VALUES('Cedar')").lastrowid
            admin = conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('admin','admin@rwu.edu','Admin','ADMIN')").lastrowid
            ra1 = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra1','ra1@rwu.edu','Alex RA','RA',?)", (b_maple,)).lastrowid
            ra2 = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra2','ra2@rwu.edu','Sam RA','RA',?)", (b_maple,)).lastrowid
            ra3 = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra3','ra3@rwu.edu','Taylor RA','RA',?)", (b_cedar,)).lastrowid
            staff = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) VALUES('ps','ps@example.edu','Pro Staff','RA',?,1,0)", (b_maple,)).lastrowid

            draft1 = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,shift_start,shift_end,created_by,status) "
                "VALUES('Fall Maple',?,'2026-10-01','2026-10-31','19:00','08:00',?,'CLOSED')",
                (b_maple, admin),
            ).lastrowid
            draft2 = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,shift_start,shift_end,created_by,status) "
                "VALUES('Fall Cedar',?,'2026-10-01','2026-10-31','20:00','07:00',?,'CLOSED')",
                (b_cedar, admin),
            ).lastrowid

            # Day 2026-10-15: Alex RA & Sam RA in Maple, Taylor RA in Cedar
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft1, ra1, "2026-10-15", admin))
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft1, ra2, "2026-10-15", admin))
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft2, ra3, "2026-10-15", admin))
            conn.commit()

        self.login_as(staff)
        res = self.request("get", "/prostaff/schedule?month=2026-10")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Consolidated names joined with ampersand
        self.assertIn("Alex RA &amp; Sam RA", html)
        self.assertIn("Taylor RA", html)

        # Omission of shift times
        self.assertNotIn("7:00 PM", html)
        self.assertNotIn("8:00 AM", html)
        self.assertNotIn("20:00", html)

        # Empty day indication preserved
        self.assertIn('<small class="empty-copy">No duty</small>', html)

        # Exactly 1 entry for Maple on Day 15
        self.assertEqual(html.count("<span>Alex RA &amp; Sam RA</span>"), 1)

    def test_prostaff_and_admin_lite_see_campus_wide_duty_coverage(self):
        today = date.today()
        with app.app_context():
            conn = db()
            maple = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            cedar = conn.execute("INSERT INTO buildings(name) VALUES('Cedar')").lastrowid
            admin = conn.execute(
                "INSERT INTO users(google_sub,email,name,role) "
                "VALUES('coverage-admin','coverage-admin@rwu.edu','Coverage Admin','ADMIN')"
            ).lastrowid
            maple_ra = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('coverage-maple','maple@rwu.edu','Maple Coverage RA','RA',?)",
                (maple,),
            ).lastrowid
            cedar_ra = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) "
                "VALUES('coverage-cedar','cedar@rwu.edu','Cedar Coverage RA','RA',?)",
                (cedar,),
            ).lastrowid
            prostaff = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('coverage-staff','staff@example.edu','Coverage Staff','RA',?,1,0)",
                (maple,),
            ).lastrowid
            admin_lite = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,admin_lite) "
                "VALUES('coverage-lite','lite@rwu.edu','Coverage Admin Lite','HRA',?,1)",
                (maple,),
            ).lastrowid
            for name, building, assignee in (
                ("Maple Duty", maple, maple_ra),
                ("Cedar Duty", cedar, cedar_ra),
            ):
                draft = conn.execute(
                    "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                    "VALUES(?,?,?,?,?,'CLOSED')",
                    (name, building, today.isoformat(), today.isoformat(), admin),
                ).lastrowid
                conn.execute(
                    "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                    (draft, assignee, today.isoformat(), admin),
                )
            conn.commit()

        for viewer in (prostaff, admin_lite):
            self.login_as(viewer)
            with patch("prostaff._duty_display_date", return_value=today):
                overview = self.request("get", "/prostaff").get_data(as_text=True)
            calendar = self.request(
                "get", f"/prostaff/schedule?month={today:%Y-%m}"
            ).get_data(as_text=True)
            for expected in ("Maple Coverage RA", "Cedar Coverage RA"):
                self.assertIn(expected, overview)
                self.assertIn(expected, calendar)

    def test_duty_calendar_includes_ra_hra_and_admin_assignments(self):
        with app.app_context():
            conn = db()
            building = conn.execute("INSERT INTO buildings(name) VALUES('Maple')").lastrowid
            creator = conn.execute(
                "INSERT INTO users(google_sub,email,name,role) "
                "VALUES('creator','creator@rwu.edu','Schedule Creator','ADMIN')"
            ).lastrowid
            assignees = []
            for sub, email, name, role in (
                ("ra-duty", "ra-duty@rwu.edu", "Assigned RA", "RA"),
                ("hra-duty", "hra-duty@rwu.edu", "Assigned HRA", "HRA"),
                ("admin-duty", "admin-duty@rwu.edu", "Assigned Admin", "ADMIN"),
            ):
                assignees.append(conn.execute(
                    "INSERT INTO users(google_sub,email,name,role,building_id) "
                    "VALUES(?,?,?,?,?)",
                    (sub, email, name, role, building),
                ).lastrowid)
            staff = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('calendar-staff','calendar-staff@example.edu','Calendar Staff','RA',?,1,0)",
                (building,),
            ).lastrowid
            draft = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('October',?,'2026-10-01','2026-10-31',?,'CLOSED')",
                (building, creator),
            ).lastrowid
            for assignee in assignees:
                conn.execute(
                    "INSERT INTO assignments(session_id,user_id,duty_date,created_by) "
                    "VALUES(?,?,'2026-10-15',?)",
                    (draft, assignee, creator),
                )
            conn.commit()

        self.login_as(staff)
        response = self.request("get", "/prostaff/schedule?month=2026-10")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Assigned Admin &amp; Assigned HRA &amp; Assigned RA", html)

    def test_prostaff_staff_search_api(self):
        with app.app_context():
            conn = db()
            b = conn.execute("INSERT INTO buildings(name) VALUES('Oak')").lastrowid
            conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('adm','adm@rwu.edu','Admin User','ADMIN')")
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('hra','hra@rwu.edu','HRA User','HRA',?)", (b,))
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('r1','alex@rwu.edu','Alex Smith','RA',?)", (b,))
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('r2','sam@rwu.edu','Sam Taylor','RA',?)", (b,))
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,disabled) VALUES('dis','dis@rwu.edu','Disabled RA','RA',?,1)", (b,))
            staff = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) VALUES('stf','stf@rwu.edu','Staff User','RA',?,1,0)", (b,)).lastrowid
            conn.commit()

        self.login_as(staff)
        # Search by name substring
        res = self.request("get", "/prostaff/api/staff-search?q=alex")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("results", data)
        names = [r["name"] for r in data["results"]]
        self.assertIn("Alex Smith", names)
        self.assertNotIn("Sam Taylor", names)

        # Search by email substring
        res = self.request("get", "/prostaff/api/staff-search?q=sam@rwu")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        names = [r["name"] for r in data["results"]]
        self.assertIn("Sam Taylor", names)
        self.assertNotIn("Alex Smith", names)

        # Case-insensitive search
        res = self.request("get", "/prostaff/api/staff-search?q=ALEX")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        names = [r["name"] for r in data["results"]]
        self.assertIn("Alex Smith", names)

        # Disabled and prostaff users excluded
        res = self.request("get", "/prostaff/api/staff-search")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        names = [r["name"] for r in data["results"]]
        self.assertIn("Alex Smith", names)
        self.assertIn("Sam Taylor", names)
        self.assertIn("HRA User", names)
        self.assertIn("Admin User", names)
        self.assertNotIn("Disabled RA", names)
        self.assertNotIn("Staff User", names)

    def test_prostaff_staff_search_authorization_and_portal_isolation(self):
        with app.app_context():
            conn = db()
            b = conn.execute("INSERT INTO buildings(name) VALUES('Pine')").lastrowid
            ra = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra_u','ra_u@rwu.edu','Standard RA','RA',?)", (b,)).lastrowid
            staff = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) VALUES('staff_u','staff_u@rwu.edu','Staff','RA',?,1,0)", (b,)).lastrowid
            conn.commit()

        # Unauthenticated: 403
        res = self.request("get", "/prostaff/api/staff-search")
        self.assertEqual(res.status_code, 403)

        # Standard RA: 403
        self.login_as(ra)
        res = self.request("get", "/prostaff/api/staff-search")
        self.assertEqual(res.status_code, 403)

        # Prostaff: allowed without redirection
        self.login_as(staff)
        res = self.request("get", "/prostaff/api/staff-search")
        self.assertEqual(res.status_code, 200)

    def test_duty_calendar_autocomplete_and_data_attributes(self):
        with app.app_context():
            conn = db()
            b = conn.execute("INSERT INTO buildings(name) VALUES('Birch')").lastrowid
            admin = conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('admin','admin@rwu.edu','Admin','ADMIN')").lastrowid
            ra = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra','birch_ra@rwu.edu','Birch RA','RA',?)", (b,)).lastrowid
            staff = conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) VALUES('ps','ps@example.edu','Pro Staff','RA',?,1,0)", (b,)).lastrowid
            draft = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('Fall',?,?,?,?, 'CLOSED')",
                (b, date.today().isoformat(), date.today().isoformat(), admin),
            ).lastrowid
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft, ra, date.today().isoformat(), admin))
            conn.commit()

        self.login_as(staff)
        res = self.request("get", f"/prostaff/schedule?month={date.today():%Y-%m}")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        self.assertIn("data-duty-calendar", html)
        self.assertIn("data-staff-search", html)
        self.assertIn('list="staff-autocomplete"', html)
        self.assertIn('<datalist id="staff-autocomplete"></datalist>', html)
        # Staff suggestions are fetched on demand instead of embedding every
        # active account (twice) in every calendar response.
        self.assertNotIn('<option value="Birch RA">', html)
        self.assertIn("data-duty-event", html)
        self.assertIn('data-staff-search="Birch RA birch_ra@rwu.edu"', html)


if __name__ == "__main__":
    unittest.main()
