"""Check the full stylesheet cascade on rendered Flask pages, not CSS excerpts."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
from playwright.sync_api import sync_playwright
from swap_building_browser_checks import check_swap_building_selector
from browser_calendar_checks import check_calendar_previews


def run(destination):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(destination)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    failures = []
    checked = 0
    screenshots = destination / 'screenshots'
    screenshots.mkdir(exist_ok=True)
    pages = sorted(destination.glob('*.html'))
    if len(pages) != 20:
        raise AssertionError(f'Expected 20 rendered pages, found {len(pages)}')
    try:
        with sync_playwright() as playwright:
            launch = {'headless': True}
            if os.environ.get('CHROMIUM_PATH'):
                launch['executable_path'] = os.environ['CHROMIUM_PATH']
            browser = playwright.chromium.launch(**launch)
            for width in (1440, 768, 390):
                context = browser.new_context(viewport={'width':width, 'height':1000})
                # No live updates or external traffic in a static fixture.
                context.route('**/live-*', lambda route: route.abort())
                context.route('https://**', lambda route: route.abort())
                page = context.new_page()
                for html in pages:
                    page.goto(f'http://127.0.0.1:{server.server_port}/{html.name}', wait_until='networkidle')
                    result = page.evaluate(r'''() => {
                        const errors = [];
                        const style = (el) => getComputedStyle(el);
                        const expect = (condition, label) => { if (!condition) errors.push(label); };
                        expect(style(document.body).backgroundColor === 'rgb(219, 227, 233)', 'page canvas');
                        expect(style(document.body).getPropertyValue('--hall-accent').trim() === '#01295f', 'fixed navy');
                        expect(!document.querySelector('input[type="color"]'), 'no color picker');
                        expect(!Array.from(document.querySelectorAll('.rwu-nav-link')).some(el => el.textContent.trim() === 'Sessions'), 'no redundant Sessions menu');
                        for (const el of document.querySelectorAll('.card,.overview-stat')) {
                            expect(style(el).backgroundColor === 'rgb(232, 238, 242)', 'muted card: ' + el.className);
                        }
                        for (const el of document.querySelectorAll('.admin-role')) {
                            expect(style(el).color === 'rgb(40, 90, 63)', 'green admin text');
                            expect(style(el).backgroundColor === 'rgb(212, 227, 217)', 'green admin background');
                        }
                        for (const el of document.querySelectorAll('.dashboard-hero,.rwu-dashboard-hero')) {
                            expect(style(el.querySelector('h1')).color === 'rgb(237, 242, 245)', 'readable navy header');
                            expect(style(el).backgroundImage.includes('1, 41, 95'), 'navy header background');
                            expect(getComputedStyle(el,'::after').display !== 'none', 'visible header circles');
                        }
                        for (const el of document.querySelectorAll('.user-name-line strong,.shift-date,.your-turn-copy strong')) {
                            expect(style(el).color === 'rgb(23, 50, 71)', 'readable label: '+el.className+' '+el.textContent.slice(0,25));
                        }
                        for (const el of document.querySelectorAll('.user-access-controls')) {
                            expect(style(el).backgroundColor === 'rgb(222, 231, 238)', 'admin access surface');
                        }
                        for (const el of document.querySelectorAll('.calendar-day.is-weekday:not(.is-no-duty),.calendar-day.is-weekend:not(.is-no-duty)')) {
                            const expected = el.classList.contains('is-weekend') ? 'rgb(238, 227, 200)' : 'rgb(221, 232, 241)';
                            expect(style(el).backgroundColor === expected, 'persistent day color: ' + el.className);
                        }
                        for (const header of document.querySelectorAll('.calendar-weekdays')) {
                            const labels = Array.from(header.children, el => el.textContent.trim());
                            expect(labels[0] === 'Sun' && labels[6] === 'Sat', 'Sunday-first calendar header');
                        }
                        for (const el of document.querySelectorAll('.calendar-day.is-self-assigned')) {
                            expect(style(el).outlineColor === 'rgb(21, 128, 61)', 'green self-assignment outline');
                            expect(style(el).outlineWidth === '3px', 'visible self-assignment outline');
                        }
                        for (const widget of document.querySelectorAll('.deadline-widget')) {
                            const staff = document.querySelector('[data-card="combined-events"]');
                            if (staff) expect(Math.abs(widget.getBoundingClientRect().height - staff.getBoundingClientRect().height) < 1, 'deadline preview matches staff card height');
                            const rows = [...widget.querySelectorAll('.deadline-preview-list li')];
                            expect(rows.length === (staff ? 3 : 4) && rows.every(row => !row.hidden), 'three deadlines with staff events, four without');
                            const other = document.querySelector('.dashboard-schedule-panel .section-head a.button');
                            if (other) {
                                const previewButton = widget.querySelector('a.button');
                                expect(Math.abs(previewButton.getBoundingClientRect().height - other.getBoundingClientRect().height) < 1, 'view all buttons match height');
                                expect(Math.abs(previewButton.getBoundingClientRect().width - other.getBoundingClientRect().width) < 1, 'view all buttons match width');
                            }
                            expect(!widget.querySelector('.deadline-preview-list p'), 'preview has no descriptions');
                            expect(widget.querySelector('.deadline-preview-disclaimer').textContent.includes('*Refer to your AC'), 'disclaimer retained');
                        }
                        for (const link of document.querySelectorAll('.rwu-nav-link[href="/deadlines"]')) {
                            expect(!!link.querySelector('svg.rwu-nav-icon[aria-hidden="true"]'), 'deadline navigation has icon');
                            const swaps = document.querySelector('.rwu-nav-link[href="/swaps"]');
                            if (swaps) expect(swaps.nextElementSibling === link, 'deadlines directly follows duty swaps');
                        }
                        for (const calendar of document.querySelectorAll('.deadline-calendar')) {
                            expect(calendar.querySelectorAll('th').length === 7, 'seven calendar weekdays');
                            expect([...calendar.querySelectorAll('template')].some(t => t.content.querySelector('.deadline-calendar-event')), 'calendar contains deadline details');
                            expect(document.querySelector('.deadline-list').children.length === 4, 'full deadline list populated');
                            expect(document.querySelector('.rwu-nav-link[aria-current="page"]').textContent.includes('Deadlines'), 'active deadline navigation');
                        }
                        for (const form of document.querySelectorAll('[data-deadline-email-form]')) {
                            expect(!form.querySelector('button'), 'email preference has no redundant save button');
                            expect(form.textContent.includes('Changes save automatically.'), 'email preference explains automatic saving');
                        }
                        for (const selector of ['.deadline-calendar td', '.prostaff-calendar-day', '.calendar-cell']) {
                            const cells = [...document.querySelectorAll(selector)];
                            if (cells.length) {
                                const heights = cells.map(cell => cell.getBoundingClientRect().height);
                                expect(Math.max(...heights) - Math.min(...heights) < 1, 'uniform calendar day heights: ' + selector);
                            }
                        }
                        expect(document.documentElement.scrollWidth <= window.innerWidth + 1, 'page horizontal overflow');
                        return errors;
                    }''')
                    failures.extend(f'{html.stem}/{width}: {error}' for error in result)
                    if html.stem == 'swap-home':
                        failures.extend(f'{html.stem}/{width}: {error}' for error in check_swap_building_selector(page))
                    failures.extend(f'{html.stem}/{width}: {error}' for error in check_calendar_previews(page))
                    checked += 1
                    if width in (1440, 390):
                        page.screenshot(path=str(screenshots / f'{html.stem}-{width}.png'), full_page=True)
                    # Test nested forms while open, not only their collapsed shells.
                    expanded_errors = page.evaluate(r'''() => {
                        document.querySelectorAll('details').forEach(el => {el.open = true});
                        const errors = [];
                        for (const form of document.querySelectorAll('.rwu-meeting-form')) {
                            if (getComputedStyle(form).backgroundColor !== 'rgba(0, 0, 0, 0)') {
                                errors.push('event form retains a separate bright background');
                            }
                        }
                        for (const field of document.querySelectorAll('input[name="repeat_weeks"]')) {
                            if (getComputedStyle(field).backgroundColor !== 'rgb(237, 242, 245)') {
                                errors.push('repeat field does not use shared muted control color');
                            }
                        }
                        if (document.documentElement.scrollWidth > innerWidth + 1) errors.push('expanded page horizontal overflow');
                        return errors;
                    }''')
                    failures.extend(f'{html.stem}/{width}: {error}' for error in expanded_errors)
                    if html.stem in ('admin', 'dashboard'):
                        page.screenshot(path=str(screenshots / f'{html.stem}-expanded-{width}.png'), full_page=True)
                    if page.locator('dialog.help-dialog').count():
                        page.locator('dialog.help-dialog').evaluate('(el) => el.showModal()')
                        badge = page.locator('dialog.help-dialog .admin-role')
                        if badge.count() and badge.evaluate('(el) => getComputedStyle(el).color') != 'rgb(40, 90, 63)':
                            failures.append(f'{html.stem}/{width}: help dialog admin badge')
                context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (destination / 'browser-results.json').write_text(json.dumps({'page_checks': checked, 'failures': failures}, indent=2))
    if failures:
        raise AssertionError('\n'.join(failures))
    print(f'Passed {checked} full-page Chromium checks (20 pages at 3 viewport widths).')


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve())
