import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TEST_DIR = tempfile.mkdtemp(prefix="ra-draft-audit-retention-tests-")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault("DATABASE_PATH", str(Path(TEST_DIR) / "test.db"))

import core  # noqa: E402


class AuditRetentionTestCase(unittest.TestCase):
    def test_default_and_supported_maximum(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUDIT_LOG_MAX_ROWS", None)
            self.assertEqual(core._audit_log_max_rows(), 2_000_000)

        with patch.dict(os.environ, {"AUDIT_LOG_MAX_ROWS": "5000000"}):
            self.assertEqual(core._audit_log_max_rows(), 5_000_000)

        with patch.dict(os.environ, {"AUDIT_LOG_MAX_ROWS": "5000001"}):
            with self.assertRaisesRegex(RuntimeError, "between 100 and 5000000"):
                core._audit_log_max_rows()

    def test_retention_prunes_large_limits_in_batches(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE audit_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL)"
        )

        with patch.object(core, "AUDIT_LOG_MAX_ROWS", 1_000):
            core._install_audit_retention(conn)
            conn.executemany(
                "INSERT INTO audit_log(action) VALUES('test')",
                [()] * 1_009,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
                1_009,
            )
            conn.execute("INSERT INTO audit_log(action) VALUES('test')")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
                1_000,
            )

        conn.close()


if __name__ == "__main__":
    unittest.main()
