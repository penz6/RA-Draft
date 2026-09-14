"""Regressions for the final shared palette, readable text and retired controls."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def luminance(value):
    channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


class SoftLightThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.navy = (ROOT / 'static/portal_navy.css').read_text(encoding='utf-8')
        cls.css = (ROOT / 'static/rwu_polish.css').read_text(encoding='utf-8') + '\n' + cls.navy
        cls.colors = dict(re.findall(r'(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;', cls.css))

    def test_main_surfaces_are_light_but_duller_than_previous_palette(self):
        previous = {'--rwu-canvas':'#e4ebf0', '--rwu-paper':'#f2f5f7', '--rwu-inset':'#e9eff3', '--rwu-control':'#f7f9fa'}
        for token, old in previous.items():
            with self.subTest(token=token):
                value = self.colors[token]
                self.assertGreater(luminance(value), 0.70)
                self.assertLess(luminance(value), luminance(old))
        self.assertGreater(luminance(self.colors['--rwu-paper']), luminance(self.colors['--rwu-canvas']))

    def test_text_retains_contrast_on_every_shared_surface(self):
        for foreground in ('--rwu-text','--rwu-muted','--rwu-navy'):
            for background in ('--rwu-canvas','--rwu-paper','--rwu-inset','--rwu-control','--rwu-hover'):
                with self.subTest(foreground=foreground, background=background):
                    self.assertGreaterEqual((luminance(self.colors[background])+.05)/(luminance(self.colors[foreground])+.05), 4.5)

    def test_legacy_components_use_light_surface_tokens(self):
        for declaration in ('--surface:var(--rwu-paper)', '--surface-2:var(--rwu-inset)', '--surface-card:var(--rwu-paper)', '--background:var(--rwu-canvas)', '--ink:var(--rwu-text)', '--muted:var(--rwu-muted)'):
            self.assertIn(declaration, self.css)
        for selector in ('.summary-item','.manager-quick-card','.manager-setting-card','.manager-settings-summary strong','.user-access-controls','.overview-stat'):
            self.assertIn(selector, self.css)

    def test_final_palette_loads_last_for_public_and_authenticated_pages(self):
        base = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
        links = re.findall(r'<link[^>]+>', base)
        self.assertIn('portal_navy.css', links[-1])
        self.assertIn('body.rwu-portal,body.rwu-public', self.navy)
        self.assertNotIn('building_theme_css', base)
        self.assertEqual(self.colors['--rwu-navy'], '#01295f')
        self.assertIn('--hall-accent:var(--rwu-navy)', self.navy)

    def test_calendar_type_colors_are_independent_of_selection(self):
        for kind, color in (('weekday','#dde8f1'),('weekend','#eee3c8')):
            block = re.search(r'\.rwu-portal \.calendar-day\.is-' + kind + r'\s*\{([^}]+)\}', self.navy)
            self.assertIsNotNone(block)
            self.assertIn('--calendar-fill:' + color, block.group(1))
        self.assertIn(':is(:disabled,:hover,:focus-visible,.is-full,.is-self-selectable)', self.css)
        self.assertIn('background:var(--calendar-fill)', self.css)
        self.assertIn('box-shadow:inset 0 0 0 2px var(--hall-accent)', self.css)

    def test_admin_badge_is_muted_green_with_readable_text(self):
        block = re.search(r'\.rwu-portal \.admin-role\s*\{([^}]+)\}', self.navy)
        self.assertIsNotNone(block)
        self.assertIn('color:#285a3f!important', block.group(1))
        self.assertIn('background:#d4e3d9!important', block.group(1))
        self.assertGreaterEqual((luminance('#d4e3d9')+.05)/(luminance('#285a3f')+.05), 4.5)

    def test_shift_text_colors_apply_without_dashboard_wrappers(self):
        expected = {'.rwu-portal .shift-card .shift-date':'--rwu-text', '.rwu-portal .shift-card .shift-location':'--rwu-muted', '.rwu-portal .shift-card .shift-partners':'--rwu-muted', '.rwu-portal .shift-card .shift-location strong':'--rwu-text', '.rwu-portal .shift-card .shift-partners strong':'--rwu-text'}
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for selector, token in expected.items():
            declarations = {}
            for selectors, block in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
                if selector in [item.strip() for item in selectors.split(',')]:
                    for declaration in block.split(';'):
                        name, sep, value = declaration.partition(':')
                        if sep:
                            declarations[name.strip()] = value.strip()
            self.assertEqual(declarations.get('color'), f'var({token})')

    def test_shift_action_text_uses_shared_navy(self):
        selector = '.rwu-portal .shift-card .shift-card-actions .button'
        block = re.search(re.escape(selector) + r'\s*\{([^}]+)\}', self.css)
        self.assertIsNotNone(block)
        self.assertRegex(block.group(1), r'(?:^|;)\s*color:\s*var\(--rwu-blue\)')
        self.assertIn('--rwu-blue:var(--rwu-navy)', self.navy)

    def test_admin_has_no_appearance_editor(self):
        admin = (ROOT / 'templates/admin.html').read_text(encoding='utf-8')
        for retired in ('name="accent_color"','Save color','name="theme_key"','name="icon_svg"','type="file"','multipart/form-data',"url_for('building_icon'"):
            self.assertNotIn(retired, admin)
        self.assertIn('Save name', admin)


if __name__ == '__main__':
    unittest.main()
