import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RWUThemeRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
        cls.dashboard = (ROOT / 'templates/dashboard_v2.html').read_text(encoding='utf-8')
        cls.admin = (ROOT / 'templates/admin.html').read_text(encoding='utf-8')
        cls.navy = (ROOT / 'static/portal_navy.css').read_text(encoding='utf-8')
        cls.settings = (ROOT / 'building_settings.py').read_text(encoding='utf-8')

    def test_sidebar_has_requested_links_without_redundant_sessions_or_search(self):
        self.assertIn('https://rwu.housing.cloud', self.base)
        self.assertIn('cm.maxient.com/reportingform.php?RogerWilliamsUniv&layout_id=0', self.base)
        for label in ('Dashboard', 'My Schedule', 'Duty Swaps'):
            self.assertIn('>' + label + '<', self.base)
        self.assertNotIn('>Sessions<', self.base)
        self.assertNotIn('type="search"', self.base)
        self.assertIn('id="sessions"', self.dashboard)

    def test_navigation_uses_house_and_calendar_icons(self):
        self.assertIn('class="rwu-nav-icon"', self.base)
        self.assertIn('M3 10.8 12 3l9 7.8', self.base)
        self.assertIn('M7 2v3M17 2v3M3.5 9h17', self.base)

    def test_sidebar_does_not_render_external_brand_images(self):
        self.assertNotIn('footer-logo-transparent.svg', self.base)
        self.assertNotIn('rwuhawks.com/images/logos', self.base)
        self.assertIn('Residence Life', self.base)

    def test_dashboard_only_uses_current_building(self):
        self.assertIn('me.building_name', self.dashboard)
        self.assertIn('building_profile', self.dashboard)
        for text in ('Willow Hall', 'Maple Hall', 'Thanks for all that you do', 'Quick Actions', 'Need help'):
            self.assertNotIn(text, self.dashboard)

    def test_staff_event_controls_are_building_scoped(self):
        for text in ('Staff Dinner &amp; Meeting', 'update_staff_dinner', 'update_staff_meeting', 'name="event_at"', 'name="event_location"', 'staff_event_repeat.html'):
            self.assertIn(text, self.dashboard)
        self.assertNotIn('Community meeting', self.dashboard)
        self.assertIn('actor["building_id"] != building_id', self.settings)
        self.assertIn('@roles("HRA", "ADMIN")', self.settings)

    def test_building_appearance_is_fixed_navy_without_an_editor(self):
        for text in ('type="color"', 'name="accent_color"', 'name="theme_key"', 'name="icon_svg"', 'update_building_appearance'):
            self.assertNotIn(text, self.admin)
        self.assertNotIn('normalize_accent_color', self.settings)
        self.assertNotIn('def update_building_appearance', self.settings)
        self.assertNotIn('building_theme_css', self.base)
        self.assertIn('PORTAL_NAVY = "#01295f"', self.settings)

    def test_admin_can_manage_staff_events_for_each_building(self):
        for text in ('rwu-building-events', 'update_staff_dinner', 'update_staff_meeting', 'name="return_to" value="admin"', 'staff_event_repeat.html'):
            self.assertIn(text, self.admin)

    def test_shared_navy_circle_header_replaces_photo(self):
        self.assertIn('portal_navy.css', self.base)
        self.assertNotIn('rwu_banner.css', self.base)
        self.assertIn(':is(.dashboard-hero,.rwu-dashboard-hero)::after', self.navy)
        self.assertIn('pointer-events:none', self.navy)

    def test_new_python_modules_parse(self):
        ast.parse(self.settings)
        ast.parse((ROOT / 'staff_event_schedule.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
