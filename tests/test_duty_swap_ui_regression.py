import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DutySwapUIRegressionTests(unittest.TestCase):
    def test_swap_partner_picker_uses_csp_safe_external_script(self):
        template = (ROOT / "templates" / "swap_page.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "swap_page.js").read_text(encoding="utf-8")

        self.assertIn("filename='swap_page.js'", template)
        self.assertIn("data-swap-partner-pick", template)
        self.assertIn('data-user-id="{{ pick.user_id }}"', template)
        self.assertIn('data-assignment-id="{{ pick.id }}"', template)
        self.assertNotIn("(function() {", template)

        self.assertIn("item.dataset.userId", script)
        self.assertIn("item.dataset.assignmentId", script)
        self.assertIn("picksByUser[partnerId]", script)
        self.assertIn("select.appendChild(opt)", script)

    def test_home_navigation_exposes_duty_swaps_tab(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        dashboard = (ROOT / "templates" / "dashboard_v2.html").read_text(encoding="utf-8")

        self.assertIn(">Duty Swaps</a>", base)
        self.assertIn("#duty-swaps", base)
        self.assertIn('id="duty-swaps"', dashboard)
        self.assertIn(">Duty Swaps</a>", dashboard)


if __name__ == "__main__":
    unittest.main()
