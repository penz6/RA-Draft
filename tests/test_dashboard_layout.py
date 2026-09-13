"""Guard the dashboard layout rules without adding browser dependencies to CI."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardLayoutTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = (ROOT / "static" / "portal_refresh.css").read_text(encoding="utf-8")

    def declarations(self, selector):
        """Return the first rule for an exact selector, ignoring formatting."""
        match = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", self.css)
        self.assertIsNotNone(match, f"Missing CSS rule: {selector}")
        return {
            key.strip(): value.strip()
            for declaration in match.group(1).split(";")
            if ":" in declaration
            for key, value in [declaration.split(":", 1)]
        }

    def test_shortcuts_do_not_stretch_to_schedule_height(self):
        row = self.declarations(".dashboard-glance-row")
        panel = self.declarations(".dashboard-actions-panel")
        card = self.declarations(".dashboard-actions-panel .shortcut-card")
        self.assertEqual(row["align-items"], "start")
        self.assertEqual(panel["align-self"], "start")
        self.assertEqual(card["flex"], "0 0 auto")
        self.assertNotIn("height", card)  # Long or enlarged text can still grow.

    def test_grid_tracks_can_shrink_on_small_screens(self):
        row = self.declarations(".dashboard-glance-row")
        grid = self.declarations(".dashboard-schedule-panel .preview-grid")
        self.assertIn("minmax(0,1fr)", row["grid-template-columns"])
        self.assertIn("min(100%,", grid["grid-template-columns"])
        self.assertEqual(self.declarations(".dashboard-schedule-panel")["min-width"], "0")

    def test_date_and_time_have_separate_rows(self):
        head = self.declarations(".dashboard-page .shift-card-head")
        time = self.declarations(".dashboard-page .time-pill")
        self.assertEqual(head["flex-direction"], "column")
        self.assertEqual(head["align-items"], "flex-start")
        self.assertEqual(time["max-width"], "100%")
        self.assertEqual(time["white-space"], "normal")

    def test_mobile_schedule_controls_stay_inline(self):
        actions = self.declarations(".dashboard-page .dashboard-schedule-panel .session-head-actions")
        self.assertEqual(actions["width"], "auto")
        self.assertEqual(actions["flex-direction"], "row")
        heading = self.declarations(".dashboard-page .dashboard-schedule-panel>.section-head")
        self.assertEqual(heading["flex-wrap"], "wrap")

    def test_tablet_and_phone_action_layouts_are_explicit(self):
        self.assertRegex(self.css, r"@media\s*\(max-width:1100px\)\s*\{\s*\.dashboard-glance-row\s*\{[^}]*grid-template-columns:minmax\(0,1fr\)")
        self.assertRegex(self.css, r"@media\s*\(max-width:650px\)\s*\{\s*\.dashboard-actions-panel\s*\{[^}]*grid-template-columns:minmax\(0,1fr\)")

    def test_touch_targets_and_keyboard_focus_are_preserved(self):
        self.assertRegex(self.css, r"@media\s*\(pointer:coarse\)\s*\{\s*\.dashboard-page \.dashboard-schedule-panel \.button\s*\{[^}]*min-height:44px")
        focus = self.declarations(".dashboard-page .shortcut-card:focus-visible")
        self.assertIn("outline", focus)
        self.assertIn("prefers-reduced-motion:reduce", self.css)


if __name__ == "__main__":
    unittest.main()
