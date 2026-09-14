import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RWUThemeRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        cls.dashboard = (ROOT / "templates" / "dashboard_v2.html").read_text(encoding="utf-8")
        cls.admin = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        cls.theme = (ROOT / "static" / "rwu_theme.css").read_text(encoding="utf-8")
        cls.overrides = (ROOT / "static" / "rwu_theme_overrides.css").read_text(encoding="utf-8")
        cls.banner = (ROOT / "static" / "rwu_banner.css").read_text(encoding="utf-8")
        cls.polish = (ROOT / "static" / "rwu_polish.css").read_text(encoding="utf-8")
        cls.settings = (ROOT / "building_settings.py").read_text(encoding="utf-8")

    def test_sidebar_has_requested_links_and_no_search(self):
        self.assertIn("https://rwu.housing.cloud", self.base)
        self.assertIn("cm.maxient.com/reportingform.php?RogerWilliamsUniv&layout_id=0", self.base)
        self.assertIn(">My Schedule<", self.base)
        self.assertIn(">Duty Swaps<", self.base)
        self.assertIn(">Sessions<", self.base)
        self.assertNotIn('placeholder="Search', self.base)
        self.assertNotIn('type="search"', self.base)

    def test_navigation_uses_house_and_calendar_icons(self):
        self.assertIn('class="rwu-nav-icon"', self.base)
        self.assertIn('M3 10.8 12 3l9 7.8', self.base)
        self.assertIn('M7 2v3M17 2v3M3.5 9h17', self.base)
        self.assertNotIn('<span aria-hidden="true">▣</span><span>My Schedule</span>', self.base)

    def test_sidebar_does_not_render_external_brand_images(self):
        self.assertNotIn("footer-logo-transparent.svg", self.base)
        self.assertNotIn("rwuhawks.com/images/logos", self.base)
        self.assertIn("Residence Life", self.base)

    def test_dashboard_only_uses_current_building(self):
        self.assertIn("me.building_name", self.dashboard)
        self.assertIn("building_profile", self.dashboard)
        self.assertNotIn("Willow Hall", self.dashboard)
        self.assertNotIn("Maple Hall", self.dashboard)
        self.assertNotIn("Thanks for all that you do", self.dashboard)
        self.assertNotIn("Quick Actions", self.dashboard)
        self.assertNotIn("Need help", self.dashboard)

    def test_staff_event_controls_are_building_scoped(self):
        self.assertIn("Staff dinner", self.dashboard)
        self.assertIn("Staff meeting", self.dashboard)
        self.assertNotIn("Community meeting", self.dashboard)
        self.assertIn("update_staff_dinner", self.dashboard)
        self.assertIn("update_staff_meeting", self.dashboard)
        self.assertIn('name="event_at"', self.dashboard)
        self.assertIn('name="event_location"', self.dashboard)
        self.assertIn('actor["building_id"] != building_id', self.settings)
        self.assertIn('@roles("HRA", "ADMIN")', self.settings)

    def test_building_appearance_is_accent_only_in_the_visible_ui(self):
        self.assertIn('name="accent_color"', self.admin)
        self.assertIn('type="color"', self.admin)
        self.assertIn('label:has(select[name="theme_key"])', self.overrides)
        self.assertIn('label:has(input[name="icon_svg"])', self.overrides)
        self.assertIn('rwu-hero-mark{display:none!important}', self.overrides)
        self.assertIn("update_building_appearance", self.admin)
        self.assertNotIn("update_building_appearance", self.dashboard)
        self.assertIn("normalize_accent_color", self.settings)
        self.assertIn("building_theme_css", self.settings)

    def test_admin_can_manage_staff_events_for_each_building(self):
        self.assertIn("rwu-building-events", self.admin)
        self.assertIn("update_staff_dinner", self.admin)
        self.assertIn("update_staff_meeting", self.admin)
        self.assertIn('name="return_to" value="admin"', self.admin)

    def test_calendar_keeps_weekday_and_weekend_colors_visible(self):
        self.assertIn(".calendar-day.is-weekday", self.overrides)
        self.assertIn(".calendar-day.is-weekend", self.overrides)
        self.assertIn("#eef6fb", self.overrides)
        self.assertIn("#fff7e7", self.overrides)

    def test_building_accent_and_uploaded_banner_are_present(self):
        self.assertIn("data:image/jpeg;base64,", self.banner)
        self.assertIn("rwu_banner.css", self.base)
        self.assertIn("rwu_polish.css", self.base)
        self.assertIn("background:transparent", self.polish)
        self.assertIn("--hall-accent", self.settings)

    def test_new_python_modules_parse(self):
        ast.parse(self.settings)
        ast.parse((ROOT / "rwu_brand_assets.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
