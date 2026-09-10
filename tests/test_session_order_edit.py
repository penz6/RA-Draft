from test_session_pause import SessionPauseTestCase
from core import app, db, next_picker


class SessionOrderEditTests(SessionPauseTestCase):
    def start_edit(self):
        csrf = self.login_as(self.hra_id)
        response = self.request('post', f'/sessions/{self.session_id}/order/start', data={'csrf': csrf})
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            row = db().execute('SELECT * FROM draft_sessions WHERE id=?', (self.session_id,)).fetchone()
            self.assertEqual(row['picking_paused'], 1)
            token = row['order_edit_token']
        return {'csrf': csrf, 'edit_token': token, 'action': 'save',
                f'position_{self.ra_one}': '2', f'position_{self.ra_two}': '1',
                'next_user_id': str(self.ra_one)}

    def test_reorder_preserves_assignments_and_selected_turn_wraps(self):
        with app.app_context():
            db().execute('INSERT INTO assignments(session_id,user_id,duty_date,created_by) VALUES(?,?,?,?)',
                         (self.session_id, self.ra_two, '2026-09-01', self.hra_id))
            db().commit()
        data = self.start_edit()
        page = self.request('get', f'/sessions/{self.session_id}/order')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Save order and resume picking', page.get_data(as_text=True))
        response = self.request('post', f'/sessions/{self.session_id}/order', data=data)
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual([r['user_id'] for r in db().execute('SELECT user_id FROM session_order ORDER BY position')], [self.ra_two, self.ra_one])
            self.assertEqual(next_picker(self.session_id)['id'], self.ra_one)
            self.assertEqual(db().execute('SELECT COUNT(*) FROM assignments').fetchone()[0], 1)
            self.assertEqual(db().execute('SELECT picking_paused FROM draft_sessions').fetchone()[0], 0)
        csrf = self.login_as(self.ra_one)
        self.request('post', f'/sessions/{self.session_id}/choose', data={'csrf': csrf, 'duty_date': '2026-09-02'})
        with app.app_context():
            self.assertEqual(db().execute('SELECT current_position FROM draft_sessions').fetchone()[0], 1)

    def test_invalid_order_next_picker_and_stale_token_keep_paused(self):
        data = self.start_edit()
        for changes in ({f'position_{self.ra_one}': '1'}, {'next_user_id': str(self.hra_id)},
                        {f'position_{self.ra_one}': 'x'}, {'edit_token': 'stale'}):
            response = self.request('post', f'/sessions/{self.session_id}/order', data={**data, **changes})
            self.assertIn(response.status_code, (400, 409))
        with app.app_context():
            db().execute('UPDATE users SET disabled=1 WHERE id=?', (self.ra_one,))
            db().commit()
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order', data=data).status_code, 400)
        with app.app_context():
            self.assertEqual(db().execute('SELECT picking_paused FROM draft_sessions').fetchone()[0], 1)
            self.assertEqual([r[0] for r in db().execute('SELECT user_id FROM session_order ORDER BY position')], [self.ra_one, self.ra_two])

    def test_other_controls_cannot_resume_and_cancel_keeps_paused(self):
        data = self.start_edit()
        for endpoint, fields in [('picking', {'paused': '0'}), ('status', {'status': 'OPEN'}), ('status', {'status': 'CLOSED'})]:
            self.request('post', f'/sessions/{self.session_id}/{endpoint}', data={'csrf': data['csrf'], **fields})
        with app.app_context():
            row = db().execute('SELECT * FROM draft_sessions').fetchone()
            self.assertEqual(row['status'], 'OPEN')
            self.assertEqual(row['picking_paused'], 1)
        self.request('post', f'/sessions/{self.session_id}/order', data={**data, 'action': 'cancel'})
        new_data = self.start_edit()
        self.assertNotEqual(data['edit_token'], new_data['edit_token'])
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order', data=data).status_code, 409)
        self.request('post', f'/sessions/{self.session_id}/order', data={**new_data, 'action': 'cancel'})
        with app.app_context():
            row = db().execute('SELECT * FROM draft_sessions').fetchone()
            self.assertEqual(row['picking_paused'], 1)
            self.assertIsNone(row['order_edit_token'])

    def test_permissions_csrf_closed_session_and_admin_access(self):
        csrf = self.login_as(self.ra_one)
        for path in ('order/start', 'order'):
            self.assertEqual(self.request('post', f'/sessions/{self.session_id}/{path}', data={'csrf': csrf}).status_code, 403)
        self.assertEqual(self.request('get', f'/sessions/{self.session_id}/order').status_code, 403)
        csrf = self.login_as(self.hra_id)
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order/start', data={}).status_code, 400)
        with app.app_context():
            other = db().execute("INSERT INTO buildings(name) VALUES('Other')").lastrowid
            db().execute('UPDATE users SET building_id=? WHERE id=?', (other, self.hra_id))
            db().commit()
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order/start', data={'csrf': csrf}).status_code, 403)
        with app.app_context():
            db().execute("UPDATE users SET role='ADMIN' WHERE id=?", (self.hra_id,))
            db().commit()
        data = self.start_edit()
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order', data=data).status_code, 302)
        with app.app_context():
            db().execute("UPDATE draft_sessions SET status='CLOSED'")
            db().commit()
        self.assertEqual(self.request('post', f'/sessions/{self.session_id}/order/start', data={'csrf': csrf}).status_code, 409)
