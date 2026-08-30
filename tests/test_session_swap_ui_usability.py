import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SessionSwapUIUsabilityTests(unittest.TestCase):
    def test_session_manager_is_prominent_and_advanced_settings_are_grouped(self):
        status_template = (ROOT / "templates" / "session_status_live_v2.html").read_text(encoding="utf-8")
        manager_template = (ROOT / "templates" / "session_manager_v2.html").read_text(encoding="utf-8")
        assignments_template = (ROOT / "templates" / "session_assignments_v2.html").read_text(encoding="utf-8")
        heading_template = (ROOT / "templates" / "session_heading_live_v2.html").read_text(encoding="utf-8")

        self.assertIn("session_manager_v2.html", status_template)
        self.assertIn("Session manager", manager_template)
        self.assertIn("Pause picking", manager_template)
        self.assertIn("Close session", manager_template)
        self.assertIn("Scheduling rules &amp; exceptions", manager_template)
        self.assertIn("Admin danger zone", manager_template)

        self.assertNotIn("Session controls", assignments_template)
        self.assertNotIn("session_hra_controls_v2.html", assignments_template)
        self.assertIn("Calendar exports", heading_template)
        self.assertNotIn("Delete session", heading_template)

    def test_swap_request_ui_is_guided_and_has_live_readiness_summary(self):
        template = (ROOT / "templates" / "swap_page.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "swap_page.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "ui_improvements.css").read_text(encoding="utf-8")

        self.assertIn("Build a swap request", template)
        self.assertIn("swap-workflow-steps", template)
        self.assertIn("swap-builder-row", template)
        self.assertIn("data-swap-summary-title", template)
        self.assertIn("data-swap-summary-copy", template)
        self.assertIn("HRA review queue", template)
        self.assertIn("Session swap history", template)

        self.assertIn("updateSummary", script)
        self.assertIn('row.classList.toggle("is-selected"', script)
        self.assertIn("Choose a partner first", script)
        self.assertIn("Ready to request", script)

        self.assertIn(".session-manager-card", css)
        self.assertIn(".swap-builder-row", css)
        self.assertIn(".swap-attention-grid", css)


if __name__ == "__main__":
    unittest.main()
