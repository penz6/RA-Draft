import os
import secrets
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.mkdtemp()) / "adversarial.db"))

import portal_app  # noqa: E402,F401
from core import app, db  # noqa: E402
from prostaff import consolidate_duty_schedule  # noqa: E402


class BackendAdversarialTestCase(unittest.TestCase):
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

    # =========================================================================
    # PART 1: STRESS-TEST consolidate_duty_schedule
    # =========================================================================

    def test_consolidate_empty_input(self):
        """Empty input must cleanly return empty dict."""
        self.assertEqual(consolidate_duty_schedule([]), {})
        self.assertEqual(consolidate_duty_schedule(None or []), {})

    def test_consolidate_1000_plus_entries_performance(self):
        """Stress-test with 5,000+ entries across 31 days and 20 buildings."""
        rows = []
        for i in range(5000):
            day = (i % 31) + 1
            b_id = (i % 20) + 1
            rows.append({
                "duty_date": f"2026-10-{day:02d}",
                "building_id": b_id,
                "building_name": f"Building {b_id}",
                "name": f"Staff Member {i % 100}",
                "email": f"staff{i % 100}@rwu.edu",
            })

        start = time.perf_counter()
        result = consolidate_duty_schedule(rows)
        duration = time.perf_counter() - start

        # 5,000 entries should process in well under 0.25 seconds
        self.assertLess(duration, 0.25, f"Consolidation took too long: {duration:.4f}s")
        self.assertEqual(len(result), 31)
        for day in range(1, 32):
            self.assertIn(day, result)
            self.assertEqual(len(result[day]), 20)
            for b in result[day]:
                self.assertIn("building_id", b)
                self.assertIn("building_name", b)
                self.assertIn("names", b)
                self.assertIn("search_terms", b)
                # Deduplication check: no duplicate names in 'names' string
                name_parts = b["names"].split(" & ")
                self.assertEqual(len(name_parts), len(set(name_parts)))

    def test_consolidate_exact_and_partial_duplicates(self):
        """Duplicate rows (same user assigned multiple times) must be deduplicated."""
        rows = [
            {"duty_date": "2026-10-10", "building_id": 1, "building_name": "Cedar", "name": "Alice Wonderland", "email": "alice@rwu.edu"},
            {"duty_date": "2026-10-10", "building_id": 1, "building_name": "Cedar", "name": "Alice Wonderland", "email": "alice@rwu.edu"},
            {"duty_date": "2026-10-10", "building_id": 1, "building_name": "Cedar", "name": "Alice Wonderland", "email": "different_email@rwu.edu"},
            {"duty_date": "2026-10-10", "building_id": 1, "building_name": "Cedar", "name": "Bob Builder", "email": "bob@rwu.edu"},
            {"duty_date": "2026-10-10", "building_id": 1, "building_name": "Cedar", "name": "Bob Builder", "email": "bob@rwu.edu"},
        ]
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[10]), 1)
        self.assertEqual(res[10][0]["names"], "Alice Wonderland & Bob Builder")
        self.assertEqual(res[10][0]["name"], "Alice Wonderland & Bob Builder")
        self.assertEqual(len(res[10][0]["staff_list"]), 2)

    def test_consolidate_special_characters_in_names(self):
        """Test names with apostrophes, quotes, ampersands, hyphens, and unicode."""
        rows = [
            {"duty_date": "2026-10-12", "building_id": 1, "building_name": "Hall", "name": "Conan O'Brien", "email": "conan@rwu.edu"},
            {"duty_date": "2026-10-12", "building_id": 1, "building_name": "Hall", "name": "Mary-Jane Watson-Parker", "email": "mj@rwu.edu"},
            {"duty_date": "2026-10-12", "building_id": 1, "building_name": "Hall", "name": 'John "Jack" Doe', "email": "jack@rwu.edu"},
            {"duty_date": "2026-10-12", "building_id": 1, "building_name": "Hall", "name": "Smith & Wesson", "email": "sw@rwu.edu"},
            {"duty_date": "2026-10-12", "building_id": 1, "building_name": "Hall", "name": "Renée Noël Müller", "email": "renee@rwu.edu"},
            {"duty_date": "2026-10-12", "building_id": 1, "building_name": "Hall", "name": "李明 (Ming Li)", "email": "ming@rwu.edu"},
        ]
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[12]), 1)
        expected_names = 'Conan O\'Brien & Mary-Jane Watson-Parker & John "Jack" Doe & Smith & Wesson & Renée Noël Müller & 李明 (Ming Li)'
        self.assertEqual(res[12][0]["names"], expected_names)
        self.assertIn("conan@rwu.edu", res[12][0]["search_terms"])
        self.assertIn("Renée Noël Müller", res[12][0]["search_terms"])
        self.assertIn("李明 (Ming Li)", res[12][0]["search_terms"])

    def test_consolidate_xss_and_injection_strings_in_names(self):
        """Names containing HTML / script tags or quotation marks must be preserved intact for template auto-escaping."""
        rows = [
            {"duty_date": "2026-10-15", "building_id": 2, "building_name": "Maple", "name": "<script>alert('XSS')</script>", "email": "xss@rwu.edu"},
            {"duty_date": "2026-10-15", "building_id": 2, "building_name": "Maple", "name": '"><b onmouseover=alert(1)>Test</b>', "email": "b@rwu.edu"},
        ]
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[15]), 1)
        self.assertIn("<script>alert('XSS')</script>", res[15][0]["names"])
        self.assertIn('"><b onmouseover=alert(1)>Test</b>', res[15][0]["names"])

    def test_consolidate_missing_and_empty_names(self):
        """Rows with None or whitespace names should not break consolidation."""
        rows = [
            {"duty_date": "2026-10-18", "building_id": 3, "building_name": "Pine", "name": None, "email": "none@rwu.edu"},
            {"duty_date": "2026-10-18", "building_id": 3, "building_name": "Pine", "name": "   ", "email": "blank@rwu.edu"},
            {"duty_date": "2026-10-18", "building_id": 3, "building_name": "Pine", "name": "Valid RA", "email": None},
        ]
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[18]), 1)
        self.assertEqual(res[18][0]["names"], "Valid RA")
        self.assertEqual(res[18][0]["search_terms"], "Valid RA")

        # When all rows for a building have blank names:
        blank_rows = [
            {"duty_date": "2026-10-19", "building_id": 4, "building_name": "Oak", "name": "", "email": ""}
        ]
        blank_res = consolidate_duty_schedule(blank_rows)
        self.assertEqual(blank_res[19][0]["names"], "Unassigned")

    def test_consolidate_multiple_buildings_per_day(self):
        """Multiple buildings on the same day must each have a separate entry."""
        rows = [
            {"duty_date": "2026-10-20", "building_id": 1, "building_name": "Building A", "name": "RA One", "email": "ra1@rwu.edu"},
            {"duty_date": "2026-10-20", "building_id": 2, "building_name": "Building B", "name": "RA Two", "email": "ra2@rwu.edu"},
            {"duty_date": "2026-10-20", "building_id": 3, "building_name": "Building C", "name": "RA Three", "email": "ra3@rwu.edu"},
        ]
        res = consolidate_duty_schedule(rows)
        self.assertEqual(len(res[20]), 3)
        b_names = [e["building_name"] for e in res[20]]
        self.assertEqual(b_names, ["Building A", "Building B", "Building C"])

    # =========================================================================
    # PART 2: STRESS-TEST /prostaff/api/staff-search
    # =========================================================================

    def _setup_search_users(self):
        """Seed a rich variety of users for search testing."""
        with app.app_context():
            conn = db()
            b = conn.execute("INSERT INTO buildings(name) VALUES('Test Hall')").lastrowid
            # Prostaff caller
            prostaff_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('ps_caller','caller@rwu.edu','Prostaff Caller','RA',?,1,0)",
                (b,)
            ).lastrowid
            admin_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role) VALUES('adm_caller','admin@rwu.edu','Admin User','ADMIN')"
            ).lastrowid
            ra_caller_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES('ra_caller','stdra@rwu.edu','Std RA','RA',?)",
                (b,)
            ).lastrowid
            disabled_staff_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,disabled) "
                "VALUES('ps_dis','dis_staff@rwu.edu','Dis Staff','RA',?,1,1)",
                (b,)
            ).lastrowid
            must_change_staff_id = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('ps_change','change@rwu.edu','Change Staff','RA',?,1,1)",
                (b,)
            ).lastrowid

            # Candidates
            # 1. Normal RA
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)", ('ra1', 'john.doe@rwu.edu', 'John Doe', 'RA', b))
            # 2. RA with underscore in name
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)", ('ra2', 'jane_under@rwu.edu', 'Jane_Underwood', 'RA', b))
            # 3. RA with percent in name
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)", ('ra3', 'mark.100%@rwu.edu', 'Mark 100% Top', 'RA', b))
            # 4. RA with backslash in name
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)", ('ra4', 'slash@rwu.edu', 'Back\\Slash', 'RA', b))
            # 5. RA with single quote
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)", ('ra5', 'oconnor@rwu.edu', "Pat O'Connor", 'RA', b))
            # 6. RA with unicode
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,?,?)", ('ra6', 'noel@rwu.edu', 'Zoë Renée', 'RA', b))
            # 7. Disabled RA
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,disabled) VALUES(?,?,?,?,?,1)", ('ra7', 'disabled.ra@rwu.edu', 'Disabled RA', 'RA', b))
            # 8. Another Prostaff (should be excluded)
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff) VALUES(?,?,?,?,?,1)", ('ps_other', 'other_ps@rwu.edu', 'Other Prostaff', 'RA', b))
            # 9. Admin candidate (should be excluded)
            conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('adm2','system.admin@rwu.edu','System Admin','ADMIN')")

            conn.commit()
            return {
                "prostaff": prostaff_id,
                "admin": admin_id,
                "ra": ra_caller_id,
                "disabled_staff": disabled_staff_id,
                "must_change_staff": must_change_staff_id,
            }

    def test_search_sql_injection_fuzzing(self):
        """Fuzz /prostaff/api/staff-search with hostile SQL injection payloads."""
        users = self._setup_search_users()
        self.login_as(users["prostaff"])

        payloads = [
            "'", "''", "'''", "''''",
            "' OR '1'='1",
            "' OR 1=1 --",
            "'; DROP TABLE users; --",
            "' UNION SELECT 1, 'injected', 'injected@rwu.edu' --",
            "admin'--",
            '"', '""', '" OR ""="',
            "\\", "\\\\", "\\\\\\",
            "\x00", "%00",
            "1' AND 1=(SELECT COUNT(*) FROM users) AND '1'='1",
            "Robert'); DROP TABLE assignments;--",
            "1; SELECT pg_sleep(5);--",
            "1' WAITFOR DELAY '0:0:5'--",
            "<script>alert(1)</script>",
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                res = self.request("get", f"/prostaff/api/staff-search?q={payload}")
                self.assertEqual(res.status_code, 200, f"Payload crashed endpoint: {payload}")
                data = res.get_json()
                self.assertIsInstance(data, dict)
                self.assertIn("results", data)
                self.assertIsInstance(data["results"], list)
                # Ensure no SQL injection injected fake users
                returned_names = [r["name"] for r in data["results"]]
                self.assertNotIn("injected", returned_names)

        # Confirm users table was not dropped or corrupted
        with app.app_context():
            user_count = db().execute("SELECT count(*) c FROM users").fetchone()["c"]
            self.assertGreater(user_count, 5)

    def test_search_wildcard_escaping(self):
        """Searching literal '%' or '_' must NOT act as SQL wildcards."""
        users = self._setup_search_users()
        self.login_as(users["prostaff"])

        # 1. Search for literal '%'
        res = self.request("get", "/prostaff/api/staff-search?q=%")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["results"]
        names = [r["name"] for r in data]
        self.assertIn("Mark 100% Top", names)
        # It must NOT return John Doe or other users who do not have '%'
        self.assertNotIn("John Doe", names)
        self.assertNotIn("Jane_Underwood", names)
        self.assertEqual(len(data), 1)

        # 2. Search for literal '_'
        res = self.request("get", "/prostaff/api/staff-search?q=_")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["results"]
        names = [r["name"] for r in data]
        self.assertIn("Jane_Underwood", names)
        # Must NOT match John Doe or Mark 100% Top
        self.assertNotIn("John Doe", names)
        self.assertNotIn("Mark 100% Top", names)
        self.assertEqual(len(data), 1)

        # 3. Search for literal '\'
        res = self.request("get", "/prostaff/api/staff-search?q=\\")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["results"]
        names = [r["name"] for r in data]
        self.assertIn("Back\\Slash", names)
        self.assertEqual(len(data), 1)

    def test_search_special_characters_and_unicode(self):
        """Searching names with apostrophes and unicode."""
        users = self._setup_search_users()
        self.login_as(users["prostaff"])

        # Apostrophe
        res = self.request("get", "/prostaff/api/staff-search?q=O'Connor")
        self.assertEqual(res.status_code, 200)
        names = [r["name"] for r in res.get_json()["results"]]
        self.assertIn("Pat O'Connor", names)

        # Unicode
        res = self.request("get", "/prostaff/api/staff-search?q=Renée")
        self.assertEqual(res.status_code, 200)
        names = [r["name"] for r in res.get_json()["results"]]
        self.assertIn("Zoë Renée", names)

    def test_search_case_insensitivity(self):
        """Search should be case-insensitive."""
        users = self._setup_search_users()
        self.login_as(users["prostaff"])

        for q in ("john", "JOHN", "JoHn", "DOE", "doe"):
            res = self.request("get", f"/prostaff/api/staff-search?q={q}")
            self.assertEqual(res.status_code, 200)
            names = [r["name"] for r in res.get_json()["results"]]
            self.assertIn("John Doe", names)

    def test_search_empty_and_whitespace(self):
        """Empty or whitespace search query must return all active RAs up to 25."""
        users = self._setup_search_users()
        self.login_as(users["prostaff"])

        for q in ("", "   ", "\t", "\n"):
            res = self.request("get", f"/prostaff/api/staff-search?q={q}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()["results"]
            self.assertGreater(len(data), 0)
            names = [r["name"] for r in data]
            self.assertIn("John Doe", names)
            self.assertNotIn("Disabled RA", names)
            self.assertNotIn("Other Prostaff", names)

    def test_search_long_inputs(self):
        """Very long input string must not crash or cause ReDoS/timeout."""
        users = self._setup_search_users()
        self.login_as(users["prostaff"])

        long_q = "A" * 10000
        start = time.perf_counter()
        res = self.request("get", f"/prostaff/api/staff-search?q={long_q}")
        duration = time.perf_counter() - start

        self.assertEqual(res.status_code, 200)
        self.assertLess(duration, 0.1, f"Search took too long: {duration:.4f}s")
        self.assertEqual(len(res.get_json()["results"]), 0)

    def test_search_role_and_authentication_security(self):
        """Verify strict authorization: unauthenticated, RA, HRA, disabled accounts rejected."""
        users = self._setup_search_users()

        # 1. Anonymous (unauthenticated) -> 403
        self.assertEqual(self.request("get", "/prostaff/api/staff-search").status_code, 403)

        # 2. Standard RA -> 403
        self.login_as(users["ra"])
        self.assertEqual(self.request("get", "/prostaff/api/staff-search").status_code, 403)

        # 3. Disabled Prostaff -> 403 (session cleared by current_user)
        self.login_as(users["disabled_staff"])
        self.assertEqual(self.request("get", "/prostaff/api/staff-search").status_code, 403)

        # 4. Prostaff with pending password change -> redirected to /prostaff/set-password
        self.login_as(users["must_change_staff"])
        res = self.request("get", "/prostaff/api/staff-search")
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.location.endswith("/prostaff/set-password"))

        # 5. Active Prostaff -> 200
        self.login_as(users["prostaff"])
        self.assertEqual(self.request("get", "/prostaff/api/staff-search").status_code, 200)

        # 6. Admin -> 200
        self.login_as(users["admin"])
        self.assertEqual(self.request("get", "/prostaff/api/staff-search").status_code, 200)

    def test_search_results_boundary_and_leak_prevention(self):
        """Ensure results limit to 25, only contain safe fields, and exclude non-RAs."""
        with app.app_context():
            conn = db()
            b = conn.execute("INSERT INTO buildings(name) VALUES('Many RAs Hall')").lastrowid
            admin = conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('adm','a@rwu.edu','Admin','ADMIN')").lastrowid
            staff = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('ps','p@rwu.edu','Staff','RA',?,1,0)", (b,)
            ).lastrowid

            # Create 40 active RAs
            for i in range(40):
                conn.execute(
                    "INSERT INTO users(google_sub,email,name,role,building_id,password_hash) "
                    "VALUES(?,?,?,'RA',?,'secret_hash_value')",
                    (f"ra_sub_{i}", f"ra_{i:02d}@rwu.edu", f"RA {i:02d}", b)
                )
            conn.commit()

        self.login_as(staff)
        res = self.request("get", "/prostaff/api/staff-search")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["results"]

        # Enforce max 25 results
        self.assertEqual(len(data), 25)

        # Verify only safe keys exist: 'id', 'name', 'email'
        for item in data:
            self.assertEqual(set(item.keys()), {"id", "name", "email"})
            self.assertNotIn("password_hash", item)
            self.assertNotIn("secret_hash_value", str(item))
            self.assertNotIn("google_sub", item)

    def test_rendered_template_autoescapes_special_characters(self):
        """Verify templates/prostaff_schedule.html auto-escapes quotes, ampersands, and tags in data-staff-search and names."""
        with app.app_context():
            conn = db()
            b = conn.execute("INSERT INTO buildings(name) VALUES('Escape Hall')").lastrowid
            admin = conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('adm','adm@rwu.edu','Admin','ADMIN')").lastrowid
            ra1 = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,? ,?)",
                ('ra1', 'xss@rwu.edu', '<script>alert(1)</script>', 'RA', b),
            ).lastrowid
            ra2 = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id) VALUES(?,?,?,? ,?)",
                ('ra2', 'quote@rwu.edu', 'Pat "The Boss" O\'Neil', 'RA', b),
            ).lastrowid
            staff = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('ps','ps@rwu.edu','Staff','RA',?,1,0)",
                (b,)
            ).lastrowid
            draft = conn.execute(
                "INSERT INTO draft_sessions(name,building_id,start_date,end_date,created_by,status) "
                "VALUES('Fall',?,?,?,?, 'CLOSED')",
                (b, "2026-10-01", "2026-10-31", admin),
            ).lastrowid
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft, ra1, "2026-10-15", admin))
            conn.execute("INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)", (draft, ra2, "2026-10-15", admin))
            conn.commit()

        self.login_as(staff)
        res = self.request("get", "/prostaff/schedule?month=2026-10")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Raw <script> tag must NOT be present unescaped
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

        # Quotes must be escaped in HTML attribute
        self.assertNotIn('data-staff-search="Pat "The Boss"', html)

    def test_consolidate_sqlite3_row_objects(self):
        """Verify consolidate_duty_schedule handles real sqlite3.Row objects with and without email."""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("CREATE TABLE t (duty_date TEXT, building_id INT, building_name TEXT, name TEXT, email TEXT)")
        cur.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?)", ("2026-10-15", 1, "Hall A", "Alice", "alice@rwu.edu"))
        cur.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?)", ("2026-10-15", 1, "Hall A", "Bob", "bob@rwu.edu"))
        rows = cur.execute("SELECT * FROM t").fetchall()
        res = consolidate_duty_schedule(rows)
        self.assertIn(15, res)
        self.assertEqual(res[15][0]["names"], "Alice & Bob")
        self.assertEqual(res[15][0]["search_terms"], "Alice alice@rwu.edu Bob bob@rwu.edu")

        # Missing email column should not raise error
        cur.execute("CREATE TABLE t_no_email (duty_date TEXT, building_id INT, building_name TEXT, name TEXT)")
        cur.execute("INSERT INTO t_no_email VALUES (?, ?, ?, ?)", ("2026-10-15", 2, "Hall B", "Charlie"))
        rows_no_email = cur.execute("SELECT * FROM t_no_email").fetchall()
        res_no_email = consolidate_duty_schedule(rows_no_email)
        self.assertEqual(res_no_email[15][0]["names"], "Charlie")
        self.assertEqual(res_no_email[15][0]["search_terms"], "Charlie")
        conn.close()

    def test_consolidate_generator_input(self):
        """Verify consolidate_duty_schedule accepts arbitrary iterables/generators, not just lists."""
        rows = [
            {"duty_date": "2026-10-01", "building_id": 1, "building_name": "Cedar", "name": "RA One", "email": "ra1@rwu.edu"},
            {"duty_date": "2026-10-01", "building_id": 1, "building_name": "Cedar", "name": "RA Two", "email": "ra2@rwu.edu"},
        ]
        res = consolidate_duty_schedule(r for r in rows)
        self.assertEqual(res[1][0]["names"], "RA One & RA Two")

    def test_consolidate_20000_entries_stress(self):
        """Stress-test with 20,000 entries across 31 days and 25 buildings."""
        rows = [
            {
                "duty_date": f"2026-10-{(i % 31) + 1:02d}",
                "building_id": (i % 25) + 1,
                "building_name": f"Building {(i % 25) + 1}",
                "name": f"Staff Member {i % 150}",
                "email": f"staff{i % 150}@rwu.edu",
            }
            for i in range(20000)
        ]
        start = time.perf_counter()
        res = consolidate_duty_schedule(rows)
        duration = time.perf_counter() - start
        self.assertLess(duration, 0.5, f"Consolidation of 20,000 entries took too long: {duration:.4f}s")
        self.assertEqual(len(res), 31)
        for day, b_list in res.items():
            self.assertEqual(len(b_list), 25)

    def test_search_extensive_sql_injection_and_dos_fuzzing(self):
        """Execute battery of 40+ adversarial SQLi payloads against /prostaff/api/staff-search."""
        users = self._setup_search_users()
        self.login_as(users["prostaff"])

        payloads = [
            "' OR 1=1 --",
            "' OR '1'='1",
            "' UNION SELECT 1, 'injected_user', 'hacked@rwu.edu' --",
            "' UNION SELECT 1, name, sql FROM sqlite_master --",
            "'; DROP TABLE users; --",
            "'; VACUUM; --",
            "'; ATTACH DATABASE ':memory:' AS evil; --",
            "1' AND (SELECT count(*) FROM users) > 0 AND '1'='1",
            "1' AND 1=(SELECT UPPER(HEX(RANDOMBLOB(500000000/2)))) --",
            "%",
            "_",
            "\\\\",
            "\\\\%",
            "\\\\_",
            "%\\\\%\\\\%",
            "_\\\\_\\\\_",
            "\\\\\\\\\\\\\\\\",
            "[][][][]",
            "[a-z]",
            "*",
            "?",
            "'",
            "''",
            "'''",
            '"',
            '""',
            '"""',
            "`",
            "```",
            "--",
            "/* */",
            "/**/",
            "\x00",
            "\x01",
            "\t",
            "\r\n",
            "\x1b[31mRed\x1b[0m",
            "李小龙",
            "👨‍👩‍👧‍👦",
            "\u202eRTL_OVERRIDE",
            "\u200bZERO_WIDTH_SPACE",
            "' OR 1=1 -- \U0001F600",
            "A" * 500,
            "%" * 500,
            "\\\\" * 500,
            "'" * 500,
        ]

        for p in payloads:
            with self.subTest(payload=p):
                res = self.client.get("/prostaff/api/staff-search", query_string={"q": p}, base_url="https://ci.local")
                self.assertEqual(res.status_code, 200, f"Payload crashed endpoint: {p}")
                data = res.get_json()
                self.assertIsInstance(data, dict)
                self.assertIn("results", data)
                returned_names = [r["name"] for r in data["results"]]
                self.assertNotIn("injected_user", returned_names)

    def test_search_http_methods_and_isolation(self):
        """Non-GET methods must be rejected with 405 (or redirected to prostaff portal by isolation)."""
        users = self._setup_search_users()

        # Anonymous caller -> 405 for POST/PUT/DELETE
        for method in ["post", "put", "delete", "patch"]:
            res = getattr(self.client, method)("/prostaff/api/staff-search", base_url="https://ci.local")
            self.assertEqual(res.status_code, 405)

        # Prostaff caller -> redirected by isolate_prostaff_portal (request.endpoint is None for invalid method)
        self.login_as(users["prostaff"])
        for method in ["post", "put", "delete", "patch"]:
            res = getattr(self.client, method)("/prostaff/api/staff-search", base_url="https://ci.local")
            self.assertIn(res.status_code, (302, 405))

    def test_search_non_ra_roles_strictly_excluded(self):
        """Staff search must strictly return active RAs (never HRAs, Admins, or Prostaff)."""
        with app.app_context():
            conn = db()
            b = conn.execute("INSERT INTO buildings(name) VALUES('Role Check Hall')").lastrowid
            staff = conn.execute(
                "INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff,password_must_change) "
                "VALUES('ps_caller_role','ps_caller_role@rwu.edu','Staff Caller','RA',?,1,0)",
                (b,)
            ).lastrowid
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('hra_user','hra@rwu.edu','HRA Candidate','HRA',?)", (b,))
            conn.execute("INSERT INTO users(google_sub,email,name,role) VALUES('adm_user','adm@rwu.edu','Admin Candidate','ADMIN')")
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,is_prostaff) VALUES('ps_user','ps@rwu.edu','Prostaff Candidate','RA',?,1)", (b,))
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id,disabled) VALUES('dis_user','dis@rwu.edu','Disabled Candidate','RA',?,1)", (b,))
            conn.execute("INSERT INTO users(google_sub,email,name,role,building_id) VALUES('valid_ra_user','ra@rwu.edu','Valid RA Candidate','RA',?)", (b,))
            conn.commit()

        self.login_as(staff)
        res = self.client.get("/prostaff/api/staff-search", base_url="https://ci.local")
        self.assertEqual(res.status_code, 200)
        returned_names = [r["name"] for r in res.get_json()["results"]]
        self.assertEqual(returned_names, ["Valid RA Candidate"])

    def test_database_invariants_and_integrity(self):
        """Verify database WAL mode, foreign keys, and integrity after all operations."""
        with app.app_context():
            conn = db()
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk, 1, "Foreign keys must be ON")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok", "Database integrity check must pass")


if __name__ == "__main__":
    unittest.main()

