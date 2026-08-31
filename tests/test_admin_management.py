import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DIR = tempfile.mkdtemp(prefix="ra-draft-admin-management-tests-")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(TEST_DIR) / "test.db"))

import portal_app  # noqa: E402,F401
from core import app, db, oauth  # noqa: E402


class AdminManagementTestCase(unittest.TestCase):
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

    def add_building(self, name="Maple"):
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

    def login_as(self, user_id, csrf="admin-management-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def add_admin(self):
        return self.add_user(
            sub="admin-sub",
            email="admin@rwu.edu",
            name="Admin User",
            role="ADMIN",
        )

    def test_admin_can_precreate_user_and_google_claims_same_record(self):
        building_id = self.add_building()
        admin_id = self.add_admin()
        csrf = self.login_as(admin_id)

        response = self.request(
            "post",
            "/admin/users",
            data={
                "csrf": csrf,
                "name": "Taylor Manual",
                "email": "taylor@g.rwu.edu",
                "role": "HRA",
                "building_id": str(building_id),
            },
        )
        self.assertEqual(response.status_code, 302)

        with app.app_context():
            provisioned = db().execute(
                "SELECT * FROM users WHERE email=?",
                ("taylor@g.rwu.edu",),
            ).fetchone()
            provisioned_id = provisioned["id"]
            self.assertTrue(provisioned["google_sub"].startswith("manual:"))
            self.assertEqual(provisioned["role"], "HRA")
            self.assertEqual(provisioned["building_id"], building_id)

        profile = {
            "sub": "real-google-sub",
            "email": "taylor@g.rwu.edu",
            "email_verified": True,
            "hd": "g.rwu.edu",
            "name": "Taylor Google",
        }
        with patch.object(
            oauth.google,
            "authorize_access_token",
            return_value={"userinfo": profile},
        ):
            response = self.request("get", "/auth/callback")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))

        with app.app_context():
            linked = db().execute(
                "SELECT * FROM users WHERE email=?",
                ("taylor@g.rwu.edu",),
            ).fetchone()
            self.assertEqual(linked["id"], provisioned_id)
            self.assertEqual(linked["google_sub"], "real-google-sub")
            self.assertEqual(linked["name"], "Taylor Google")
            self.assertEqual(linked["role"], "HRA")
            self.assertEqual(linked["building_id"], building_id)
            actions = [
                row["action"]
                for row in db().execute(
                    "SELECT action FROM audit_log WHERE target_id=? ORDER BY id",
                    (provisioned_id,),
                ).fetchall()
            ]
            self.assertIn("admin.user.create", actions)
            self.assertIn("auth.user_claimed", actions)

    def test_manual_user_requires_an_rwu_google_email(self):
        admin_id = self.add_admin()
        csrf = self.login_as(admin_id)
        response = self.request(
            "post",
            "/admin/users",
            data={
                "csrf": csrf,
                "name": "Outside User",
                "email": "outside@example.com",
                "role": "RA",
                "building_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                db().execute(
                    "SELECT COUNT(*) n FROM users WHERE email=?",
                    ("outside@example.com",),
                ).fetchone()["n"],
                0,
            )

    def test_admin_can_rename_and_delete_unused_building(self):
        building_id = self.add_building("Old Hall")
        admin_id = self.add_admin()
        user_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="Resident Assistant",
            building_id=building_id,
        )
        csrf = self.login_as(admin_id)

        response = self.request(
            "post",
            f"/admin/buildings/{building_id}/rename",
            data={"csrf": csrf, "name": "New Hall"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                db().execute(
                    "SELECT name FROM buildings WHERE id=?",
                    (building_id,),
                ).fetchone()["name"],
                "New Hall",
            )

        response = self.request(
            "post",
            f"/admin/buildings/{building_id}/delete",
            data={"csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertIsNone(
                db().execute(
                    "SELECT 1 FROM buildings WHERE id=?",
                    (building_id,),
                ).fetchone()
            )
            self.assertIsNone(
                db().execute(
                    "SELECT building_id FROM users WHERE id=?",
                    (user_id,),
                ).fetchone()["building_id"]
            )

    def test_building_with_session_history_cannot_be_deleted(self):
        building_id = self.add_building()
        admin_id = self.add_admin()
        with app.app_context():
            db().execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,created_by"
                ") VALUES(?,?,?,?,?)",
                ("Historical", building_id, "2026-09-01", "2026-09-01", admin_id),
            )
            db().commit()
        csrf = self.login_as(admin_id)
        response = self.request(
            "post",
            f"/admin/buildings/{building_id}/delete",
            data={"csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertIsNotNone(
                db().execute(
                    "SELECT 1 FROM buildings WHERE id=?",
                    (building_id,),
                ).fetchone()
            )

    def test_user_delete_is_allowed_without_history_and_blocked_with_history(self):
        building_id = self.add_building()
        admin_id = self.add_admin()
        disposable_id = self.add_user(
            sub="disposable",
            email="disposable@g.rwu.edu",
            name="Disposable User",
        )
        scheduled_id = self.add_user(
            sub="scheduled",
            email="scheduled@g.rwu.edu",
            name="Scheduled User",
            building_id=building_id,
        )
        with app.app_context():
            session_id = db().execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,created_by"
                ") VALUES(?,?,?,?,?)",
                ("Duty", building_id, "2026-09-01", "2026-09-01", admin_id),
            ).lastrowid
            db().execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, scheduled_id),
            )
            db().commit()

        csrf = self.login_as(admin_id)
        response = self.request(
            "post",
            f"/admin/users/{disposable_id}/delete",
            data={"csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertIsNone(
                db().execute(
                    "SELECT 1 FROM users WHERE id=?",
                    (disposable_id,),
                ).fetchone()
            )

        response = self.request(
            "post",
            f"/admin/users/{scheduled_id}/delete",
            data={"csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertIsNotNone(
                db().execute(
                    "SELECT 1 FROM users WHERE id=?",
                    (scheduled_id,),
                ).fetchone()
            )

        response = self.request(
            "post",
            f"/admin/users/{admin_id}/delete",
            data={"csrf": csrf},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertIsNotNone(
                db().execute(
                    "SELECT 1 FROM users WHERE id=?",
                    (admin_id,),
                ).fetchone()
            )

    def test_admin_page_exposes_create_rename_and_delete_controls(self):
        self.add_building()
        admin_id = self.add_admin()
        self.login_as(admin_id)
        page = self.request("get", "/admin").get_data(as_text=True)
        self.assertIn('action="/admin/users"', page)
        self.assertIn("Create user", page)
        self.assertIn("/rename", page)
        self.assertIn("/delete", page)
        self.assertIn("/static/admin.css", page)

    def test_admin_can_see_and_access_all_sessions_not_included_in(self):
        maple_id = self.add_building("Maple Hall")
        oak_id = self.add_building("Oak Hall")
        pine_id = self.add_building("Pine Hall")

        # Admin with no building assignment
        admin_id = self.add_admin()

        # Other users
        hra_oak_id = self.add_user(
            sub="hra-oak",
            email="hra.oak@rwu.edu",
            name="Oak HRA",
            role="HRA",
            building_id=oak_id,
        )
        ra_oak_id = self.add_user(
            sub="ra-oak",
            email="ra.oak@g.rwu.edu",
            name="Oak RA",
            role="RA",
            building_id=oak_id,
        )
        hra_pine_id = self.add_user(
            sub="hra-pine",
            email="hra.pine@rwu.edu",
            name="Pine HRA",
            role="HRA",
            building_id=pine_id,
        )
        ra_pine_id = self.add_user(
            sub="ra-pine",
            email="ra.pine@g.rwu.edu",
            name="Pine RA",
            role="RA",
            building_id=pine_id,
        )

        with app.app_context():
            conn = db()
            # Session 1: Oak Hall (Open) - Admin is not creator or participant
            session1_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,created_by,status"
                ") VALUES(?,?,?,?,?,'OPEN')",
                ("Oak Fall Draft", oak_id, "2026-09-01", "2026-09-05", hra_oak_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session1_id, ra_oak_id),
            )

            # Session 2: Pine Hall (Closed) - Admin is not creator or participant
            session2_id = conn.execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,created_by,status"
                ") VALUES(?,?,?,?,?,'CLOSED')",
                ("Pine Spring Draft", pine_id, "2026-10-01", "2026-10-05", hra_pine_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session2_id, ra_pine_id),
            )
            conn.execute(
                "INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)",
                (session2_id, ra_pine_id, "2026-10-01", hra_pine_id),
            )
            conn.commit()

        # 1. Verify Admin sees all sessions on Dashboard
        self.login_as(admin_id)
        dashboard_res = self.request("get", "/dashboard")
        self.assertEqual(dashboard_res.status_code, 200)
        dashboard_html = dashboard_res.get_data(as_text=True)
        self.assertIn("Oak Fall Draft", dashboard_html)
        self.assertIn("Oak Hall", dashboard_html)
        self.assertIn("Pine Spring Draft", dashboard_html)
        self.assertIn("Pine Hall", dashboard_html)

        # 2. Verify Admin receives all sessions in live fragments
        fragments_res = self.request("get", "/dashboard/live-fragments")
        self.assertEqual(fragments_res.status_code, 200)
        fragments_data = fragments_res.get_json()
        self.assertIn("Oak Fall Draft", fragments_data["fragments"]["sessions"])
        self.assertIn("Pine Spring Draft", fragments_data["fragments"]["sessions"])

        # 3. Verify Admin can view the open session in Oak Hall
        oak_view_res = self.request("get", f"/sessions/{session1_id}")
        self.assertEqual(oak_view_res.status_code, 200)
        oak_view_html = oak_view_res.get_data(as_text=True)
        self.assertIn("Oak Fall Draft", oak_view_html)
        self.assertIn("Oak Hall", oak_view_html)

        # 4. Verify Admin can access live fragments for Oak Hall session
        oak_live_res = self.request("get", f"/sessions/{session1_id}/live-fragments")
        self.assertEqual(oak_live_res.status_code, 200)
        self.assertIn("Oak Fall Draft", oak_live_res.get_json()["fragments"]["heading"])

        # 5. Verify Admin sees all closed sessions in Duty Swaps menu
        swaps_menu_res = self.request("get", "/swaps")
        self.assertEqual(swaps_menu_res.status_code, 200)
        swaps_menu_html = swaps_menu_res.get_data(as_text=True)
        self.assertIn("Pine Spring Draft", swaps_menu_html)
        self.assertIn("Pine Hall", swaps_menu_html)

        # 6. Verify Admin can access the duty swap page for Pine Hall session
        pine_swap_res = self.request("get", f"/swaps/session/{session2_id}")
        self.assertEqual(pine_swap_res.status_code, 200)
        pine_swap_html = pine_swap_res.get_data(as_text=True)
        self.assertIn("Pine Spring Draft", pine_swap_html)

        # 7. Verify Admin can export full session calendar for Pine Hall session
        cal_res = self.request("get", f"/calendar/session/{session2_id}.ics")
        self.assertEqual(cal_res.status_code, 200)
        self.assertIn("Pine Spring Draft", cal_res.get_data(as_text=True))

        # 8. Verify Admin with a specific building assigned still sees all sessions across other buildings
        with app.app_context():
            db().execute("UPDATE users SET building_id=? WHERE id=?", (maple_id, admin_id))
            db().commit()

        dashboard_res2 = self.request("get", "/dashboard")
        self.assertEqual(dashboard_res2.status_code, 200)
        dashboard_html2 = dashboard_res2.get_data(as_text=True)
        self.assertIn("Oak Fall Draft", dashboard_html2)
        self.assertIn("Pine Spring Draft", dashboard_html2)

        swaps_res2 = self.request("get", "/swaps")
        self.assertEqual(swaps_res2.status_code, 200)
        self.assertIn("Pine Spring Draft", swaps_res2.get_data(as_text=True))

    def test_admin_can_impersonate_ra_and_view_regular_ra_ui(self):
        maple_id = self.add_building("Maple Hall")
        oak_id = self.add_building("Oak Hall")

        admin_id = self.add_admin()
        ra_id = self.add_user(
            sub="ra-maple",
            email="ra.maple@g.rwu.edu",
            name="Maple RA",
            role="RA",
            building_id=maple_id,
        )

        with app.app_context():
            conn = db()
            maple_session_id = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('Maple Fall Draft',?,'2026-09-01','2026-09-05',?,'OPEN')",
                (maple_id, admin_id),
            ).lastrowid
            oak_session_id = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('Oak Fall Draft',?,'2026-09-01','2026-09-05',?,'OPEN')",
                (oak_id, admin_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (maple_session_id, ra_id),
            )
            conn.commit()

        # Admin starts impersonating Maple RA
        csrf = self.login_as(admin_id)
        impersonate_res = self.request(
            "post",
            f"/admin/impersonate/{ra_id}",
            data={"csrf": csrf},
        )
        self.assertEqual(impersonate_res.status_code, 302)
        self.assertTrue(impersonate_res.location.endswith("/dashboard"))

        # In dashboard: user sees RA interface
        dashboard_res = self.request("get", "/dashboard")
        self.assertEqual(dashboard_res.status_code, 200)
        dashboard_html = dashboard_res.get_data(as_text=True)

        # 1. Impersonation bar is present
        self.assertIn("Viewing as <strong>Maple RA</strong>", dashboard_html)
        self.assertIn("Signed in as <strong>Admin User</strong>", dashboard_html)
        self.assertIn("/admin/stop-impersonation", dashboard_html)

        # 2. RA views only Maple Hall, not Oak Hall
        self.assertIn("Maple Fall Draft", dashboard_html)
        self.assertNotIn("Oak Fall Draft", dashboard_html)

        # 3. RA does not have Admin nav link or session delete actions
        self.assertNotIn('href="/admin"', dashboard_html)
        self.assertNotIn("Delete session", dashboard_html)

        # 4. In session view: RA does not have session manager controls
        session_view_res = self.request("get", f"/sessions/{maple_session_id}")
        self.assertEqual(session_view_res.status_code, 200)
        session_view_html = session_view_res.get_data(as_text=True)
        self.assertNotIn("Session manager", session_view_html)
        self.assertNotIn("Close session", session_view_html)

        # 5. Exit impersonation
        stop_res = self.request(
            "post",
            "/admin/stop-impersonation",
            data={"csrf": csrf},
        )
        self.assertEqual(stop_res.status_code, 302)
        self.assertTrue(stop_res.location.endswith("/admin"))

        # 6. Admin is fully restored
        admin_dash_res = self.request("get", "/dashboard")
        admin_dash_html = admin_dash_res.get_data(as_text=True)
        self.assertNotIn("Impersonating", admin_dash_html)
        self.assertIn("Maple Fall Draft", admin_dash_html)
        self.assertIn("Oak Fall Draft", admin_dash_html)
        self.assertIn('href="/admin"', admin_dash_html)

    def test_impersonation_security_protections(self):
        building_id = self.add_building()
        admin_id = self.add_admin()
        ra_id = self.add_user(
            sub="ra-sub",
            email="ra@g.rwu.edu",
            name="Regular RA",
            role="RA",
            building_id=building_id,
        )
        disabled_id = self.add_user(
            sub="disabled-ra",
            email="disabled@g.rwu.edu",
            name="Disabled RA",
            role="RA",
            building_id=building_id,
        )
        with app.app_context():
            db().execute("UPDATE users SET disabled=1 WHERE id=?", (disabled_id,))
            db().commit()

        # RA cannot impersonate (403)
        ra_csrf = self.login_as(ra_id)
        res_forbidden = self.request(
            "post",
            f"/admin/impersonate/{admin_id}",
            data={"csrf": ra_csrf},
        )
        self.assertEqual(res_forbidden.status_code, 403)

        # Admin cannot impersonate self
        admin_csrf = self.login_as(admin_id)
        res_self = self.request(
            "post",
            f"/admin/impersonate/{admin_id}",
            data={"csrf": admin_csrf},
        )
        self.assertEqual(res_self.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertNotIn("impersonator_uid", flask_session)

        # Admin cannot impersonate disabled user
        res_disabled = self.request(
            "post",
            f"/admin/impersonate/{disabled_id}",
            data={"csrf": admin_csrf},
        )
        self.assertEqual(res_disabled.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertNotIn("impersonator_uid", flask_session)

        # Verify audit logs
        self.request(
            "post",
            f"/admin/impersonate/{ra_id}",
            data={"csrf": admin_csrf},
        )
        self.request(
            "post",
            "/admin/stop-impersonation",
            data={"csrf": admin_csrf},
        )
        with app.app_context():
            audit_actions = [
                row["action"]
                for row in db().execute("SELECT action FROM audit_log ORDER BY id ASC").fetchall()
            ]
            self.assertIn("admin.impersonate.start", audit_actions)
            self.assertIn("admin.impersonate.stop", audit_actions)


if __name__ == "__main__":
    unittest.main()
