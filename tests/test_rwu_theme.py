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
        cls.banner = (ROOT / "static" / "rwu_banner.css").read_text(encoding="utf-8")
        cls.settings = (ROOT / "building_settings.py").read_text(encoding="utf-8")

    def test_sidebar_has_requested_links_and_no_search(self):
        self.assertIn("https://rwu.housing.cloud", self.base)
        self.assertIn("cm.maxient.com/reportingform.php?RogerWilliamsUniv&layout_id=0", self.base)
        self.assertIn(">My Schedule<", self.base)
        self.assertIn(">Duty Swaps<", self.base)
        self.assertIn(">Sessions<", self.base)
        self.assertNotIn('placeholder="Search', self.base)
        self.assertNotIn('type="search"', self.base)

    def test_official_rwu_brand_assets_are_used(self):
        self.assertIn("www.rwu.edu/themes/custom/rwu", self.base)
        self.assertIn("rwuhawks.com/images/logos", self.base)
        self.assertIn("import rwu_brand_assets", (ROOT / "main.py").read_text(encoding="utf-8"))

    def test_dashboard_only_uses_current_building(self):
        self.assertIn("me.building_name", self.dashboard)
        self.assertIn("building_profile", self.dashboard)
        self.assertNotIn("Willow Hall", self.dashboard)
        self.assertNotIn("Maple Hall", self.dashboard)
        self.assertNotIn("Thanks for all that you do", self.dashboard)
        self.assertNotIn("Quick Actions", self.dashboard)
        self.assertNotIn("Need help", self.dashboard)

    def test_meeting_controls_are_building_scoped(self):
        self.assertIn("update_building_meeting", self.dashboard)
        self.assertIn('name="meeting_at"', self.dashboard)
        self.assertIn('name="meeting_location"', self.dashboard)
        self.assertIn("actor[\"building_id\"] != building_id", self.settings)

    def test_building_appearance_is_admin_managed(self):
        self.assertIn('enctype="multipart/form-data"', self.admin)
        self.assertIn('name="icon_svg"', self.admin)
        self.assertIn('name="theme_key"', self.admin)
        self.assertIn("update_building_appearance", self.admin)
        self.assertNotIn("update_building_appearance", self.dashboard)
        self.assertIn("sanitize_building_svg", self.settings)

    def test_building_themes_and_uploaded_banner_are_present(self):
        self.assertIn(".theme-maple", self.theme)
        self.assertIn(".theme-willow", self.theme)
        self.assertIn(".theme-cedar", self.theme)
        self.assertIn(".theme-stonewall", self.theme)
        self.assertIn(".theme-bayside", self.theme)
        self.assertIn("data:image/jpeg;base64,", self.banner)
        self.assertIn("rwu_banner.css", self.base)

    def test_new_python_modules_parse(self):
        ast.parse(self.settings)
        ast.parse((ROOT / "rwu_brand_assets.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
