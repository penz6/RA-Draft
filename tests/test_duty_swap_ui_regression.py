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
        self.assertIn('data-my-raw-date="{{ pick.duty_date }}"', template)
        self.assertNotIn("(function() {", template)

        self.assertIn("item.dataset.userId", script)
        self.assertIn("item.dataset.assignmentId", script)
        self.assertIn("picksByUser[partnerId]", script)
        self.assertIn("eligiblePartnerPicksForRow", script)
        self.assertIn("selectedOutgoingDates", script)
        self.assertIn("remainingMyDates.has(pick.rawDate)", script)
        self.assertIn("selectedTargetIds", script)
        self.assertIn("selectedPartnerOutgoingDates", script)
        self.assertIn("partnerAlreadyWorksMyDate", script)
        self.assertIn("projectedBatchIssue", script)
        self.assertIn("No eligible shifts", script)
        self.assertIn("refreshTargetSelects", script)
        self.assertIn("select.appendChild(opt)", script)
        self.assertNotIn("differentDatePartnerPicks", script)
        self.assertNotIn("partnerAlreadyOnMyDate", script)

    def test_home_navigation_opens_dedicated_duty_swap_menu(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        swap_home = (ROOT / "templates" / "swap_home.html").read_text(encoding="utf-8")

        self.assertIn(">Duty Swaps</a>", base)
        self.assertIn("url_for('swap_home')", base)
        self.assertNotIn("dashboard') }}#duty-swaps", base)
        self.assertIn("Available sessions", swap_home)
        self.assertIn("url_for('swap_page', session_id=s.id)", swap_home)


if __name__ == "__main__":
    unittest.main()
