"""Regression checks for the shared light palette and retired appearance UI."""

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
        cls.css = (ROOT / 'static' / 'rwu_polish.css').read_text(encoding='utf-8')
        cls.colors = dict(re.findall(r'(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;', cls.css))

    def test_main_surfaces_are_light_but_not_pure_white(self):
        tokens = ('--rwu-canvas', '--rwu-paper', '--rwu-inset', '--rwu-control')
        for token in tokens:
            with self.subTest(token=token):
                value = self.colors[token]
                self.assertNotEqual(value.lower(), '#ffffff')
                self.assertGreater(luminance(value), 0.70)
                self.assertLess(luminance(value), 0.97)
        self.assertGreater(luminance(self.colors['--rwu-paper']), luminance(self.colors['--rwu-canvas']))

    def test_text_retains_contrast_on_the_muted_surfaces(self):
        backgrounds = ('--rwu-canvas', '--rwu-paper', '--rwu-inset', '--rwu-control', '--rwu-hover')
        for foreground in ('--rwu-text', '--rwu-muted'):
            for background in backgrounds:
                with self.subTest(foreground=foreground, background=background):
                    ratio = (luminance(self.colors[background]) + 0.05) / (luminance(self.colors[foreground]) + 0.05)
                    self.assertGreaterEqual(ratio, 4.5)

    def test_legacy_components_use_light_surface_tokens(self):
        for declaration in (
            '--surface:var(--rwu-paper)',
            '--surface-2:var(--rwu-inset)',
            '--surface-card:var(--rwu-paper)',
            '--background:var(--rwu-canvas)',
            '--ink:var(--rwu-text)',
            '--muted:var(--rwu-muted)',
        ):
            self.assertIn(declaration, self.css)
        for selector in ('.summary-item', '.manager-quick-card', '.manager-setting-card', '.manager-settings-summary strong'):
            self.assertIn(selector, self.css)

    def test_palette_loads_after_legacy_and_other_theme_styles(self):
        base = (ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')
        for earlier in ('style.css', 'portal_refresh.css', 'rwu_theme.css', 'rwu_theme_overrides.css'):
            self.assertLess(base.index(earlier), base.index('rwu_polish.css'))

    def test_calendar_type_colors_are_independent_of_selection(self):
        for kind, color in (('weekday', '#e7f0f7'), ('weekend', '#f5edda')):
            block = re.search(r'\.rwu-portal \.calendar-day\.is-' + kind + r'\s*\{([^}]+)\}', self.css)
            self.assertIsNotNone(block)
            self.assertIn('--calendar-fill:' + color, block.group(1))
        self.assertIn(':is(:disabled,:hover,:focus-visible,.is-full,.is-self-selectable)', self.css)
        self.assertIn('background:var(--calendar-fill)', self.css)
        self.assertIn('box-shadow:inset 0 0 0 2px var(--hall-accent)', self.css)

    def test_admin_badge_overrides_legacy_important_green(self):
        block = re.search(r'\.rwu-portal \.admin-role\s*\{([^}]+)\}', self.css)
        self.assertIsNotNone(block)
        self.assertIn('color:#235f86!important', block.group(1))
        self.assertIn('background:#e1edf5!important', block.group(1))

    def test_shift_text_colors_apply_without_dashboard_wrappers(self):
        # The full schedule uses .upcoming-shifts-card, not .rwu-duty-panel.
        # Require shared selectors rather than another dashboard-only patch.
        expected = {
            '.rwu-portal .shift-card .shift-date': '--rwu-text',
            '.rwu-portal .shift-card .shift-location': '--rwu-muted',
            '.rwu-portal .shift-card .shift-partners': '--rwu-muted',
            '.rwu-portal .shift-card .shift-location strong': '--rwu-text',
            '.rwu-portal .shift-card .shift-partners strong': '--rwu-text',
        }
        css = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for selector, token in expected.items():
            with self.subTest(selector=selector):
                declarations = {}
                for selectors, block in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
                    if selector in [item.strip() for item in selectors.split(',')]:
                        for declaration in block.split(';'):
                            name, sep, value = declaration.partition(':')
                            if sep:
                                declarations[name.strip()] = value.strip()
                self.assertEqual(declarations.get('color'), f'var({token})')

    def test_shift_action_text_does_not_depend_on_custom_accent(self):
        # An admin may choose a very light accent; action labels still need
        # readable text. Borders may continue to use the selected accent.
        selector = '.rwu-portal .shift-card .shift-card-actions .button'
        block = re.search(re.escape(selector) + r'\s*\{([^}]+)\}', self.css)
        self.assertIsNotNone(block)
        self.assertRegex(block.group(1), r'(?:^|;)\s*color:\s*var\(--rwu-blue\)')

    def test_admin_appearance_markup_has_only_an_accent_picker(self):
        admin = (ROOT / 'templates' / 'admin.html').read_text(encoding='utf-8')
        self.assertIn('name="accent_color"', admin)
        self.assertIn('Save color', admin)
        for retired in ('name="theme_key"', 'name="icon_svg"', 'type="file"', 'multipart/form-data', "url_for('building_icon'"):
            self.assertNotIn(retired, admin)


if __name__ == '__main__':
    unittest.main()
