import os
import tempfile
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("GOOGLE_CLIENT_ID", "ci-client.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "ci-client-secret")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
os.environ.setdefault("PROXY_HOPS", "0")
os.environ.setdefault(
    "DATABASE_PATH",
    str(Path(tempfile.gettempdir()) / "ra-draft-calendar-summary-tests.db"),
)

from calendar_routes import calendar_summary  # noqa: E402


def test_calendar_summary_uses_building_initial():
    assert calendar_summary("Maple Hall", ["Penn Potter", "Alex Smith"]) == "M* Penn & Alex"


def test_calendar_summary_normalizes_initial_case():
    assert calendar_summary("maple hall", ["Penn Potter"]) == "M* Penn"
