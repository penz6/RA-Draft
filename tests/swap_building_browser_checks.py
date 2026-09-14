"""Geometry checks for the building picker using the full application CSS."""


def check_swap_building_selector(page):
    cards = page.locator('.swap-building-card')
    if not cards.count():
        return ['missing dedicated building selector']

    measure = r'''() => {
        const errors = [];
        const expect = (ok, label) => { if (!ok) errors.push(label); };
        for (const card of document.querySelectorAll('.swap-building-card')) {
            const arrow = card.querySelector('.swap-building-arrow');
            if (!arrow) { errors.push('missing building arrow'); continue; }
            const copy = card.querySelector('.session-card-main');
            const box = card.getBoundingClientRect();
            const icon = arrow.getBoundingClientRect();
            const text = copy.getBoundingClientRect();
            const css = getComputedStyle(card);
            const arrowCss = getComputedStyle(arrow);
            expect(card.tagName === 'A' && /\/swaps\/building\/\d+$/.test(card.getAttribute('href')), 'whole row links to building swaps');
            expect(arrow.getAttribute('aria-hidden') === 'true', 'arrow is decorative');
            expect(icon.width >= 24 && icon.width <= 40 && Math.abs(icon.width - icon.height) < 1, 'compact square arrow');
            expect(arrowCss.borderRadius === '50%', 'circular arrow');
            expect(arrowCss.color === 'rgb(1, 41, 95)', 'navy arrow');
            expect(arrowCss.backgroundColor === 'rgb(216, 228, 239)', 'muted arrow background');
            const inset = parseFloat(css.paddingRight) + parseFloat(css.borderRightWidth);
            expect(Math.abs(box.right - icon.right - inset) <= 1, 'arrow at right edge');
            expect(Math.abs((box.top + box.bottom) / 2 - (icon.top + icon.bottom) / 2) <= 1, 'vertically centered arrow');
            expect(text.right <= icon.left - 8, 'copy separated from arrow');
            expect(card.scrollWidth <= card.clientWidth + 1, 'no card overflow');
            expect(copy.scrollWidth <= copy.clientWidth + 1, 'no text overflow');
            expect(box.height >= 44, 'whole row touch target');
        }
        expect(document.documentElement.scrollWidth <= innerWidth + 1, 'no page overflow');
        return errors;
    }'''
    errors = page.evaluate(measure)
    card = cards.first
    heading = card.locator('h3')
    original = heading.text_content()
    try:
        # Long names must wrap without moving the arrow into a wide grid track.
        heading.evaluate('(el, value) => { el.textContent = value; }',
                         'North Campus Residence ' + 'LongBuildingName' * 8)
        errors.extend('long name: ' + item for item in page.evaluate(measure))
        card.hover()
        errors.extend('hover: ' + item for item in page.evaluate(measure))
        page.keyboard.press('Tab')
        card.focus()
        if not card.evaluate("el => el.matches(':focus-visible') && getComputedStyle(el).outlineStyle === 'solid' && parseFloat(getComputedStyle(el).outlineWidth) >= 2"):
            errors.append('keyboard focus outline')
    finally:
        heading.evaluate('(el, value) => { el.textContent = value; }', original)
        card.evaluate('el => el.blur()')
    return errors
