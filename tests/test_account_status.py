import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault(
    "DATABASE_PATH",
    str(Path(tempfile.gettempdir()) / "ra-draft-account-status-tests.db"),
)

import portal_app  # noqa: E402,F401
from core import app, db, next_picker, oauth  # noqa: E402


class AccountStatusTestCase(unittest.TestCase):
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
            user_id = db().execute(
                "INSERT INTO buildings(name) VALUES(?)",
                (name,),
            ).lastrowid
            db().commit()
            return user_id

    def add_user(
        self,
        *,
        sub,
        email,
        name,
        role="RA",
        building_id=None,
        disabled=0,
    ):
        with app.app_context():
            user_id = db().execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,disabled) "
                "VALUES(?,?,?,?,?,?)",
                (sub, email, name, role, building_id, disabled),
            ).lastrowid
            db().commit()
            return user_id

    def login_as(self, user_id, csrf="account-status-csrf"):
        with self.client.session_transaction() as flask_session:
            flask_session["uid"] = user_id
            flask_session["csrf"] = csrf
        return csrf

    def test_ra_cannot_forge_admin_or_manager_actions(self):
        building_id = self.add_building()
        ra_id = self.add_user(
            sub="ra",
            email="ra@g.rwu.edu",
            name="RA",
            building_id=building_id,
        )
        with app.app_context():
            session_id = db().execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,created_by"
                ") VALUES(?,?,?,?,?)",
                ("Duty", building_id, "2026-09-01", "2026-09-01", ra_id),
            ).lastrowid
            db().execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (session_id, ra_id),
            )
            db().commit()

        csrf = self.login_as(ra_id)
        self.assertEqual(
            self.request(
                "post",
                "/admin/buildings",
                data={"csrf": csrf, "name": "Unauthorized Hall"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.request(
                "post",
                f"/sessions/{session_id}/picking",
                data={"csrf": csrf, "paused": "1"},
            ).status_code,
            403,
        )
        with app.app_context():
            self.assertIsNone(
                db().execute(
                    "SELECT 1 FROM buildings WHERE name='Unauthorized Hall'"
                ).fetchone()
            )
            self.assertEqual(
                db().execute(
                    "SELECT picking_paused FROM draft_sessions WHERE id=?",
                    (session_id,),
                ).fetchone()["picking_paused"],
                0,
            )

    def test_admin_can_disable_and_reenable_user(self):
        building_id = self.add_building()
        admin_id = self.add_user(
            sub="admin",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        ra_id = self.add_user(
            sub="ra-disabled",
            email="disabled@g.rwu.edu",
            name="Disabled RA",
            building_id=building_id,
        )

        csrf = self.login_as(admin_id)
        response = self.request(
            "post",
            f"/admin/users/{ra_id}/status",
            data={"csrf": csrf, "disabled": "1"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                db().execute("SELECT disabled FROM users WHERE id=?", (ra_id,)).fetchone()[
                    "disabled"
                ],
                1,
            )

        # Even a browser that still has an old signed-in cookie is rejected by
        # the server-side current_user boundary.
        self.login_as(ra_id)
        blocked = self.request("get", "/dashboard")
        self.assertEqual(blocked.status_code, 302)
        self.assertTrue(blocked.location.endswith("/login"))
        with self.client.session_transaction() as flask_session:
            self.assertNotIn("uid", flask_session)

        profile = {
            "sub": "ra-disabled",
            "email": "disabled@g.rwu.edu",
            "email_verified": True,
            "hd": "g.rwu.edu",
            "name": "Disabled RA",
        }
        with patch.object(
            oauth.google,
            "authorize_access_token",
            return_value={"userinfo": profile},
        ):
            blocked_login = self.request("get", "/auth/callback")
        self.assertEqual(blocked_login.status_code, 302)
        self.assertTrue(blocked_login.location.endswith("/"))

        csrf = self.login_as(admin_id)
        response = self.request(
            "post",
            f"/admin/users/{ra_id}/status",
            data={"csrf": csrf, "disabled": "0"},
        )
        self.assertEqual(response.status_code, 302)
        self.login_as(ra_id)
        self.assertEqual(self.request("get", "/dashboard").status_code, 200)

    def test_admin_cannot_disable_self_or_leave_no_enabled_admin(self):
        admin_id = self.add_user(
            sub="admin",
            email="admin@rwu.edu",
            name="Admin",
            role="ADMIN",
        )
        disabled_admin_id = self.add_user(
            sub="disabled-admin",
            email="disabled.admin@rwu.edu",
            name="Disabled Admin",
            role="ADMIN",
            disabled=1,
        )
        csrf = self.login_as(admin_id)

        response = self.request(
            "post",
            f"/admin/users/{admin_id}/status",
            data={"csrf": csrf, "disabled": "1"},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                db().execute("SELECT disabled FROM users WHERE id=?", (admin_id,)).fetchone()[
                    "disabled"
                ],
                0,
            )

        response = self.request(
            "post",
            f"/admin/users/{admin_id}",
            data={"csrf": csrf, "role": "RA", "building_id": ""},
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(
                db().execute("SELECT role FROM users WHERE id=?", (admin_id,)).fetchone()[
                    "role"
                ],
                "ADMIN",
            )
            self.assertEqual(
                db().execute(
                    "SELECT disabled FROM users WHERE id=?", (disabled_admin_id,)
                ).fetchone()["disabled"],
                1,
            )

    def test_disabled_participant_is_skipped_and_not_offered_for_new_sessions(self):
        building_id = self.add_building()
        hra_id = self.add_user(
            sub="hra",
            email="hra@rwu.edu",
            name="HRA",
            role="HRA",
            building_id=building_id,
        )
        disabled_id = self.add_user(
            sub="disabled-ra",
            email="disabled.ra@g.rwu.edu",
            name="Disabled Existing Participant",
            building_id=building_id,
            disabled=1,
        )
        active_id = self.add_user(
            sub="active-ra",
            email="active.ra@g.rwu.edu",
            name="Active RA",
            building_id=building_id,
        )

        with app.app_context():
            existing_session_id = db().execute(
                "INSERT INTO draft_sessions("
                "name,building_id,start_date,end_date,capacity,date_order,current_position,created_by"
                ") VALUES(?,?,?,?,?,?,1,?)",
                (
                    "Existing duty",
                    building_id,
                    "2026-09-01",
                    "2026-09-02",
                    1,
                    "CHRONOLOGICAL",
                    hra_id,
                ),
            ).lastrowid
            db().execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,1)",
                (existing_session_id, disabled_id),
            )
            db().execute(
                "INSERT INTO session_order(session_id,user_id,position) VALUES(?,?,2)",
                (existing_session_id, active_id),
            )
            db().commit()
            self.assertEqual(next_picker(existing_session_id)["id"], active_id)

        csrf = self.login_as(hra_id)
        response = self.request(
            "post",
            "/sessions",
            data={
                "csrf": csrf,
                "name": "New duty",
                "building_id": str(building_id),
                "start_date": "2026-10-01",
                "end_date": "2026-10-01",
                "capacity": "1",
                "date_order": "CHRONOLOGICAL",
                "participant_ids": [str(disabled_id), str(active_id)],
                f"order_{disabled_id}": "1",
                f"order_{active_id}": "2",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            newest = db().execute(
                "SELECT id FROM draft_sessions WHERE name='New duty'"
            ).fetchone()["id"]
            participants = [
                row["user_id"]
                for row in db().execute(
                    "SELECT user_id FROM session_order WHERE session_id=? ORDER BY position",
                    (newest,),
                ).fetchall()
            ]
            self.assertEqual(participants, [active_id])


if __name__ == "__main__":
    unittest.main()
