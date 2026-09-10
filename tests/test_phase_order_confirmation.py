import test_session_pause
from core import app, db, next_picker


class PhaseOrderConfirmationTests(test_session_pause.SessionPauseTestCase):
    def configure_phase(self, rule='WEEKDAYS_FIRST', capacity=1, end='2026-09-05'):
        with app.app_context():
            db().execute('UPDATE draft_sessions SET start_date=?,end_date=?,date_order=?,capacity=? WHERE id=?',
                         ('2026-09-03', end, rule, capacity, self.session_id))
            db().commit()

    def pick(self, user_id, date, manager=False):
        csrf = self.login_as(self.hra_id if manager else user_id)
        return self.request('post', f'/sessions/{self.session_id}/{"assign" if manager else "choose"}',
                            data={'csrf': csrf, 'user_id': str(user_id), 'duty_date': date},
                            headers={'X-RA-Draft-Async': '1'})

    def state(self):
        with app.app_context():
            return dict(db().execute('SELECT * FROM draft_sessions WHERE id=?', (self.session_id,)).fetchone())

    def confirmation(self, reverse=True, starter=None):
        csrf = self.login_as(self.hra_id)
        return {'csrf': csrf, 'edit_token': self.state()['order_edit_token'], 'action': 'save',
                f'position_{self.ra_one}': '2' if reverse else '1',
                f'position_{self.ra_two}': '1' if reverse else '2',
                'next_user_id': str(starter or self.ra_two)}

    def test_final_weekday_pauses_and_confirmation_reverses_with_selected_starter(self):
        self.configure_phase()
        response = self.pick(self.ra_one, '2026-09-03')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Waiting for HRA to confirm order', response.json['message'])
        self.assertEqual(self.state()['phase_order_state'], 1)
        self.assertEqual(self.state()['picking_paused'], 1)
        self.assertEqual(self.pick(self.ra_two, '2026-09-04').status_code, 409)
        self.assertEqual(self.pick(self.ra_two, '2026-09-04', manager=True).status_code, 409)
        page = self.request('get', f'/sessions/{self.session_id}').get_data(as_text=True)
        self.assertIn('Confirm picking order', page)
        self.assertNotIn('>Resume picking', page)
        fragments = self.request('get', f'/sessions/{self.session_id}/live-fragments').json
        self.assertIn('Waiting for HRA to confirm order', fragments['fragments']['status'])
        page = self.request('get', f'/sessions/{self.session_id}/order').get_data(as_text=True)
        self.assertIn('Confirm picking order', page)
        self.assertIn('Waiting for HRA to confirm the order', page)
        self.assertIn('data-sortable-participants', page)
        self.assertIn('draggable="true"', page)
        self.assertIn(f'name="position_{self.ra_one}"', page)
        self.assertNotIn('type="number" name="position_', page)
        self.assertLess(page.index(f'name="position_{self.ra_two}"'), page.index(f'name="position_{self.ra_one}"'))
        data = self.confirmation(starter=self.ra_one)
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order', data=data).status_code, 302)
        self.assertEqual(self.state()['phase_order_state'], 2)
        self.assertEqual(self.state()['picking_paused'], 0)
        with app.app_context():
            self.assertEqual(next_picker(self.session_id)['id'], self.ra_one)
            self.assertEqual([r[0] for r in db().execute('SELECT user_id FROM session_order ORDER BY position')], [self.ra_two, self.ra_one])
            self.assertEqual(db().execute('SELECT COUNT(*) FROM assignments').fetchone()[0], 1)
        self.assertEqual(self.pick(self.ra_one, '2026-09-04').status_code, 200)
        with app.app_context():
            self.assertEqual(next_picker(self.session_id)['id'], self.ra_two)
        self.assertEqual(self.state()['phase_order_state'], 2)
        self.assertEqual(self.state()['picking_paused'], 0)

    def test_refilling_weekdays_after_confirmation_does_not_reverse_again(self):
        self.configure_phase()
        self.pick(self.ra_one, '2026-09-03')
        data = self.confirmation()
        self.request('post', f'/sessions/{self.session_id}/order', data=data)
        with app.app_context():
            assignment_id = db().execute('SELECT id FROM assignments').fetchone()[0]
        self.request('post', f'/sessions/{self.session_id}/assignments/{assignment_id}/delete',
                     data={'csrf': data['csrf']})
        self.assertEqual(self.pick(self.ra_two, '2026-09-03').status_code, 200)
        self.assertEqual(self.state()['picking_paused'], 0)
        self.assertEqual(self.state()['phase_order_state'], 2)
        with app.app_context():
            self.assertEqual([r[0] for r in db().execute('SELECT user_id FROM session_order ORDER BY position')], [self.ra_two, self.ra_one])

    def test_manager_out_of_turn_pick_triggers_gate_and_custom_order_is_allowed(self):
        self.configure_phase()
        self.assertEqual(self.pick(self.ra_two, '2026-09-03', manager=True).status_code, 200)
        self.assertEqual(self.state()['phase_order_state'], 1)
        data = self.confirmation(reverse=False, starter=self.ra_two)
        self.request('post', f'/sessions/{self.session_id}/order', data=data)
        with app.app_context():
            self.assertEqual([r[0] for r in db().execute('SELECT user_id FROM session_order ORDER BY position')], [self.ra_one, self.ra_two])
            self.assertEqual(next_picker(self.session_id)['id'], self.ra_two)

    def test_review_later_cannot_bypass_confirmation_and_ra_cannot_confirm(self):
        self.configure_phase()
        self.pick(self.ra_one, '2026-09-03')
        data = self.confirmation()
        self.request('post', f'/sessions/{self.session_id}/order', data={**data, 'action': 'cancel'})
        for endpoint, fields in [('picking', {'paused': '0'}), ('status', {'status': 'OPEN'}), ('status', {'status': 'CLOSED'})]:
            self.request('post', f'/sessions/{self.session_id}/{endpoint}', data={'csrf': data['csrf'], **fields})
        self.assertEqual(self.state()['phase_order_state'], 1)
        self.assertEqual(self.state()['picking_paused'], 1)
        self.assertEqual(self.state()['order_edit_token'], data['edit_token'])
        csrf = self.login_as(self.ra_two)
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order', data={**data, 'csrf': csrf}).status_code, 403)
        with app.app_context():
            db().execute("UPDATE users SET role='ADMIN',building_id=NULL WHERE id=?", (self.hra_id,))
            db().commit()
        data = self.confirmation()
        self.request('post', f'/sessions/{self.session_id}/order', data=data)
        self.assertEqual(self.state()['phase_order_state'], 2)

    def test_only_final_weekday_slot_triggers_pause(self):
        self.configure_phase(capacity=2)
        self.pick(self.ra_one, '2026-09-03')
        self.assertEqual(self.state()['phase_order_state'], 0)
        self.pick(self.ra_two, '2026-09-03')
        self.assertEqual(self.state()['phase_order_state'], 1)

    def test_capacity_change_that_fills_weekdays_triggers_confirmation(self):
        self.configure_phase(capacity=2)
        self.pick(self.ra_one, '2026-09-03')
        csrf = self.login_as(self.hra_id)
        self.request('post', f'/sessions/{self.session_id}/date-capacity',
                     data={'csrf': csrf, 'duty_date': '2026-09-03', 'capacity': '1'})
        self.assertEqual(self.state()['phase_order_state'], 1)

    def test_date_kind_override_drives_phase_boundary(self):
        self.configure_phase()
        csrf = self.login_as(self.hra_id)
        self.request('post', f'/sessions/{self.session_id}/date-kind',
                     data={'csrf': csrf, 'duty_date': '2026-09-04', 'date_kind': 'WEEKDAY'})
        self.pick(self.ra_one, '2026-09-03')
        self.assertEqual(self.state()['phase_order_state'], 0)
        self.pick(self.ra_two, '2026-09-04')
        self.assertEqual(self.state()['phase_order_state'], 1)

    def test_complete_and_unphased_sessions_do_not_pause(self):
        for rule, end in [('CHRONOLOGICAL', '2026-09-05'), ('WEEKENDS_FIRST', '2026-09-05'), ('WEEKDAYS_FIRST', '2026-09-03')]:
            with self.subTest(rule=rule):
                self.setUp()
                self.configure_phase(rule=rule, end=end)
                self.pick(self.ra_one, '2026-09-03', manager=True)
                self.assertEqual(self.state()['phase_order_state'], 0)
                self.assertEqual(self.state()['picking_paused'], 0)
