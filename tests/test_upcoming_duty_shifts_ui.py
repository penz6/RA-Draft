import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpcomingDutyShiftsUITestCase(unittest.TestCase):
    def test_dashboard_shows_only_next_three_with_full_list_link(self):
        template = (ROOT / "templates" / "dashboard_v2.html").read_text(encoding="utf-8")
        self.assertIn("{% for shift in upcoming_shifts[:3] %}", template)
        self.assertIn("url_for('upcoming_duty_shifts')", template)
        self.assertIn(">View all</a>", template)

    def test_full_upcoming_shift_page_uses_complete_list(self):
        route = (ROOT / "session_view.py").read_text(encoding="utf-8")
        template = (ROOT / "templates" / "upcoming_duty_shifts.html").read_text(encoding="utf-8")

        self.assertIn('@app.route("/my-duty-shifts")', route)
        self.assertIn("shifts = user_upcoming_shifts(user[\"id\"])", route)
        self.assertIn('render_template(\n        "upcoming_duty_shifts.html"', route)
        self.assertIn("{% for shift in upcoming_shifts %}", template)
        self.assertNotIn("upcoming_shifts[:3]", template)
        self.assertIn("url_for('dashboard')", template)


if __name__ == "__main__":
    unittest.main()
