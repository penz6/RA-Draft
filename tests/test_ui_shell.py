import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UiShellRegressionTests(unittest.TestCase):
    def test_mobile_navigation_is_accessible_and_progressively_enhanced(self):
        base = (ROOT / "templates" / "base.html").read_text()
        script = (ROOT / "static" / "app.js").read_text()
        styles = (ROOT / "static" / "style.css").read_text()
        rwu_styles = (ROOT / "static" / "rwu_theme.css").read_text()

        self.assertIn('aria-controls="primary-navigation"', base)
        self.assertIn('aria-expanded="false"', base)
        self.assertIn('id="primary-navigation"', base)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn(".js .primary-nav{display:none", styles)
        self.assertIn(".js .primary-nav.is-open{display:flex}", styles)
        self.assertIn(".js .primary-nav.is-open{flex-direction:column", styles)
        self.assertIn(".primary-nav .nav-form .button{width:100%}", styles)
        self.assertIn('.closest(".rwu-sidebar")', script)
        self.assertIn(".js .rwu-sidebar.is-open{display:flex}", rwu_styles)
        self.assertIn(".js .rwu-mobile-bar{display:flex}", rwu_styles)
        self.assertIn(".js .rwu-mobile-bar .nav-toggle{display:inline-flex}", rwu_styles)
        self.assertNotIn(".rwu-sidebar:has(", rwu_styles)

    def test_confirmation_handler_covers_live_and_keyboard_submissions(self):
        dashboard = (ROOT / "templates" / "dashboard_v2.html").read_text()
        script = (ROOT / "static" / "app.js").read_text()

        self.assertNotIn("onsubmit=", dashboard)
        self.assertIn('data-confirm="Permanently delete session', dashboard)
        self.assertIn('document.addEventListener("submit"', script)
        self.assertIn("event.submitter?.dataset.confirm", script)
        self.assertIn("event.stopImmediatePropagation()", script)


if __name__ == "__main__":
    unittest.main()
