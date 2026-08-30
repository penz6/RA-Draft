"""Small, predictable retention limits for persistent application data."""

import os
import sqlite3

from core import DB_PATH, configure_connection


def _audit_log_max_rows():
    raw = os.environ.get("AUDIT_LOG_MAX_ROWS", "5000").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("AUDIT_LOG_MAX_ROWS must be an integer.") from exc
    if not 100 <= value <= 100000:
        raise RuntimeError("AUDIT_LOG_MAX_ROWS must be between 100 and 100000.")
    return value


AUDIT_LOG_MAX_ROWS = _audit_log_max_rows()


def _install_audit_retention():
    conn = configure_connection(sqlite3.connect(DB_PATH, timeout=10))
    offset = AUDIT_LOG_MAX_ROWS - 1

    # Trim an existing oversized audit table immediately. SQLite reuses freed
    # pages for future rows, so the database stops growing even though we do not
    # VACUUM on every cleanup.
    conn.execute(
        "DELETE FROM audit_log WHERE id < ("
        "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?"
        ")",
        (offset,),
    )

    # SQLite does not allow bound parameters inside CREATE TRIGGER. The only
    # interpolated value is a range-validated integer; construct the DDL
    # separately so ordinary .execute() calls remain parameterized by policy.
    trigger_sql = """
        CREATE TRIGGER audit_log_retention
        AFTER INSERT ON audit_log
        BEGIN
          DELETE FROM audit_log
          WHERE id < (
            SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET __OFFSET__
          );
        END;
    """.replace("__OFFSET__", str(offset))
    conn.execute("DROP TRIGGER IF EXISTS audit_log_retention")
    conn.executescript(trigger_sql)
    conn.commit()
    conn.close()


_install_audit_retention()
