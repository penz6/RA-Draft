"""Account enable/disable support.

The column is additive so existing SQLite deployments migrate in place.  The
patched ``core.current_user`` is the single authorization boundary used by
route decorators, manager checks, live streams, and lock-time rechecks.
"""

import sqlite3

import core
from flask import session
from core import DB_PATH, configure_connection


def _ensure_disabled_column():
    conn = configure_connection(sqlite3.connect(DB_PATH, timeout=10))
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "disabled" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL "
            "DEFAULT 0 CHECK(disabled IN (0,1))"
        )
        conn.commit()
    conn.close()


_ensure_disabled_column()

_base_current_user = core.current_user


def current_user():
    """Return only an enabled signed-in account.

    Clearing the cookie-backed session here makes disabling authoritative for
    every server-side permission check, including requests that were submitted
    before an administrator changed the account but had not yet acquired a
    database write lock.
    """

    user = _base_current_user()
    if user is not None and bool(user["disabled"]):
        session.clear()
        return None
    return user


core.current_user = current_user

# Apply the single-school authorization and timezone policies before route
# modules import their helpers from core.
import runtime_policy  # noqa: E402,F401
