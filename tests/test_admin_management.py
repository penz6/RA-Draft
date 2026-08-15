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


if __name__ == "__main__":
    unittest.main()
