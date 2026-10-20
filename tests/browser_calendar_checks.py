"""Exercise day details without changing assignment or calendar navigation actions."""


def check_calendar_previews(page):
    errors = []
    if page.locator('.deadline-calendar').count():
        target = page.locator('.calendar-date-preview').filter(has_text='Monthly report').first
        expected = 'Submit your programming report'
    elif page.locator('.prostaff-duty-calendar').count():
        target = page.locator('.prostaff-calendar-day[data-day="1"]')
        expected = 'Cedar'
    elif page.locator('.calendar-cell').count():
        target = page.locator('.calendar-cell[data-calendar-readonly]').first
        expected = None
    else:
        return errors
    if not target.count():
        return ['calendar detail trigger missing']
    heights = page.locator('.deadline-calendar td,.prostaff-calendar-day,.calendar-cell').evaluate_all('(cells) => cells.map(cell => cell.getBoundingClientRect().height)')
    target.hover()
    panel = page.locator('#calendar-day-details')
    panel.wait_for(state='visible')
    if expected and expected not in panel.inner_text():
        errors.append('hover preview omits complete details')
    bounds = panel.bounding_box()
    viewport = page.viewport_size
    if not (bounds['x'] >= 0 and bounds['y'] >= 0 and bounds['x'] + bounds['width'] <= viewport['width'] + 1 and bounds['y'] + bounds['height'] <= viewport['height'] + 1):
        errors.append('day details leave the viewport')
    page.keyboard.press('Escape')
    panel.wait_for(state='hidden')
    target.evaluate('(el) => { document.activeElement?.blur(); el.focus(); }')
    panel.wait_for(state='visible')
    page.keyboard.press('Escape')
    panel.wait_for(state='hidden')
    target.click()
    panel.wait_for(state='visible')
    panel.get_by_role('button', name='Close day details').click()
    panel.wait_for(state='hidden')
    after = page.locator('.deadline-calendar td,.prostaff-calendar-day,.calendar-cell').evaluate_all('(cells) => cells.map(cell => cell.getBoundingClientRect().height)')
    if heights != after:
        errors.append('opening details changes calendar cell heights')
    if page.locator('a[href*="hawklink.rwu.edu"]').count():
        errors.append('HawkLink URLs remain visible')
    calendar = page.locator('[data-duty-calendar][data-self-user-id]')
    if calendar.count():
        enabled = calendar.locator('[data-calendar-day]:not([disabled])')
        if calendar.get_attribute('data-self-user-id') == calendar.get_attribute('data-current-user-id') and enabled.count():
            calendar.locator('[data-calendar-day][data-self-selectable="true"]:not([disabled])').first.click()
            dialog = calendar.locator('[data-self-pick-dialog]')
            dialog.wait_for(state='visible')
            dialog.get_by_role('button', name='Cancel', exact=True).click()
            dialog.wait_for(state='hidden')
        elif page.locator('[data-manager-pick]').count() and enabled.count():
            page.locator('[data-manager-pick]').first.click()
            enabled.first.click()
            dialog = calendar.locator('[data-manager-pick-dialog]')
            dialog.wait_for(state='visible')
            dialog.get_by_role('button', name='Cancel', exact=True).click()
            calendar.locator('[data-manager-cancel]').click()
    return errors
