"""Authentication, roles and sensitive reads are enforced by the real HTTP server."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server
from auth import AuthService, AuthError, password_value
from engine import ValidationError
from auth_support import admin_service, session_headers, TEST_PASSWORD


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1791000000
        self.service = AuthService(self.temp.name, clock=lambda: self.now)

    def setup_admin(self):
        return self.service.setup({'setup_code': self.service.setup_path.read_text(),
                                   'username': 'manager', 'password': TEST_PASSWORD})

    def test_setup_proof_required_one_time_and_password_not_stored(self):
        with self.assertRaises(AuthError):
            self.service.setup({'setup_code': 'fake', 'username': 'manager', 'password': TEST_PASSWORD})
        issued = self.setup_admin()
        self.assertFalse(self.service.setup_path.exists())
        self.assertEqual(issued['user']['role'], 'admin')
        self.assertNotIn(TEST_PASSWORD.encode(), self.service.path.read_bytes())
        with self.assertRaises(AuthError):
            self.service.setup({'setup_code': 'already-used', 'username': 'manager', 'password': TEST_PASSWORD})

    def test_short_password_rejected(self):
        for password in ('abc', '              ', None, 'x' * 129):
            with self.assertRaises(ValidationError):
                password_value(password)

    def test_setup_expiry_and_local_rotation_seen(self):
        old = self.service.setup_path.read_text()
        self.now += 1201
        with self.assertRaises(AuthError):
            self.service.setup({'setup_code': old, 'username': 'manager', 'password': TEST_PASSWORD})
        self.service.ensure_setup_code(force=True)
        self.setup_admin()

    def test_throttle_persists_and_expires(self):
        self.setup_admin()
        for _ in range(5):
            with self.assertRaises(AuthError):
                self.service.login({'username': 'manager', 'password': 'wrong'})
        restored = AuthService(self.temp.name, clock=lambda: self.now)
        with self.assertRaises(AuthError) as limited:
            restored.login({'username': 'manager', 'password': TEST_PASSWORD})
        self.assertEqual(limited.exception.status, 429)
        self.now += 301
        self.assertEqual(restored.login({'username': 'manager', 'password': TEST_PASSWORD})['user']['username'], 'manager')

    def test_idle_expiry_and_password_revokes_sessions(self):
        issued = self.setup_admin()
        cookie = 'session=' + issued['session_id']
        session = self.service.authenticate(cookie, 'session')
        self.service.change_password(session, {'current_password': TEST_PASSWORD, 'new_password': 'A-new-test-passphrase-2026!'})
        self.assertIsNone(self.service.authenticate(cookie, 'session'))
        new = self.service.login({'username': 'manager', 'password': 'A-new-test-passphrase-2026!'})
        self.now += 1801
        self.assertIsNone(self.service.authenticate('session=' + new['session_id'], 'session'))

    def test_last_admin_cannot_be_disabled_or_demoted(self):
        self.setup_admin()
        for changes in ({'role': 'viewer'}, {'role': 'admin', 'disabled': True}):
            with self.assertRaises(ValidationError):
                self.service.manage_user('manager', {'username': 'manager', **changes})


class HTTPAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth, self.admin = admin_service(Path(self.temp.name))
        self.patches = [patch.object(server, 'AUTH', self.auth),
                        patch.object(server, 'DB_PATH', Path(self.temp.name) / 'test.sqlite3'),
                        patch.object(server, 'MONITOR', None)]
        for item in self.patches:
            item.start()
        self.http = server.LocalServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def request(self, path, payload=None, session=None, headers=None):
        values = {'Content-Type': 'application/json', 'Origin': self.url}
        if session:
            values.update(session_headers(self.http, session))
        values.update(headers or {})
        request = Request(self.url + path, data=json.dumps(payload).encode() if payload is not None else None, headers=values)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw), response.headers

    def make_user(self, role):
        self.auth.manage_user('test-admin', {'username': role + '-user', 'password': TEST_PASSWORD, 'role': role})
        return self.auth.login({'username': role + '-user', 'password': TEST_PASSWORD})

    def test_sensitive_gets_require_login_not_bootstrap_token(self):
        for path in ('/api/bootstrap', '/api/monitor', '/api/monitor/config', '/api/live', '/api/auth/users'):
            self.assertEqual(self.request(path, headers={'X-App-Token': server.TOKEN})[0], 401)
        self.assertEqual(self.request('/api/health')[0], 200)
        self.assertFalse(self.request('/api/auth/status')[1]['authenticated'])

    def test_login_cookie_flags_and_no_session_id_in_json(self):
        status, data, headers = self.request('/api/auth/login', {'username': 'test-admin', 'password': TEST_PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn('session_id', data)
        self.assertNotIn(TEST_PASSWORD, json.dumps(data))

    def test_cross_origin_and_csrf_are_blocked(self):
        self.assertEqual(self.request('/api/auth/login', {}, headers={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/auth/logout', {}, self.admin, {'X-App-Token': 'wrong'})[0], 403)
        self.assertEqual(self.request('/api/auth/status', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_cookie_alone_cannot_recover_proof_or_access_gets(self):
        headers = {'Cookie': session_headers(self.http, self.admin)['Cookie']}
        status, packet, _ = self.request('/api/auth/status', headers=headers)
        self.assertEqual(status, 200)
        self.assertFalse(packet['authenticated'])
        self.assertIsNone(packet['csrf_token'])
        self.assertIsNone(packet['user'])
        self.assertEqual(self.request('/api/bootstrap', headers=headers)[0], 403)
        self.assertEqual(self.request('/api/auth/status', session=self.admin)[1]['authenticated'], True)
        self.assertEqual(self.request('/api/bootstrap', session=self.admin)[0], 200)
        self.assertEqual(self.request('/api/bootstrap', session=self.admin, headers={'X-App-Token': '\u00e9'})[0], 403)

    def test_roles_enforced_on_server(self):
        viewer = self.make_user('viewer')
        operator = self.make_user('operator')
        self.assertEqual(self.request('/api/bootstrap', session=viewer)[0], 200)
        self.assertEqual(self.request('/api/runs', {}, viewer)[0], 200)
        for path in ('/api/monitor/config', '/api/auth/users'):
            self.assertEqual(self.request(path, session=viewer)[0], 403)
            self.assertEqual(self.request(path, {}, operator)[0], 403)
        for path in ('/api/generate', '/api/workflow', '/api/monitor/ingest', '/api/monitor/correction', '/api/monitor/refresh'):
            self.assertEqual(self.request(path, {}, viewer)[0], 403)
        self.assertEqual(self.request('/api/pilot', {'action': 'save'}, operator)[0], 403)

    def test_viewer_redaction_and_operator_identity_bound(self):
        viewer = self.make_user('viewer')
        status, packet, _ = self.request('/api/bootstrap', session=viewer)
        self.assertEqual(status, 200)
        self.assertNotIn('authorization', packet['demo']['site'])
        self.assertFalse(packet['permissions']['can_operate'])
        self.assertFalse(packet['permissions']['can_configure'])
        operator = self.make_user('operator')
        class MockMonitor:
            def correct(self, payload):
                return payload
        with patch.object(server, 'MONITOR', MockMonitor()):
            status, packet, _ = self.request('/api/monitor/correction', {'actor': 'forged-admin'}, operator)
        self.assertEqual(status, 200)
        self.assertEqual(packet['actor'], 'operator-user')

    def test_viewer_cannot_read_extension_fields_or_notice_reviewer(self):
        from engine import generate
        from workflow import initialize, mutate
        viewer = self.make_user('viewer')
        data = json.loads((server.ROOT / 'data/zju_pilot.json').read_text(encoding='utf-8'))
        data['site']['private_contact'] = 'PRIVATE_PHONE_MARKER'
        data['weather']['private_weather_note'] = 'PRIVATE_WEATHER_MARKER'
        data['points'][0]['private_contact'] = 'PRIVATE_POINT_MARKER'
        data['plans'][0]['private_contact'] = 'PRIVATE_PLAN_MARKER'
        data['private_registry'] = 'PRIVATE_REGISTRY_MARKER'
        status, _, _ = self.request('/api/pilot', {'action': 'save', 'revision': 0, 'data': data}, self.admin)
        self.assertEqual(status, 200)
        status, packet, _ = self.request('/api/bootstrap', session=viewer)
        self.assertEqual(status, 200)
        self.assertNotIn('PRIVATE_', json.dumps(packet))
        self.assertEqual(packet['demo']['points'][0]['id'], data['points'][0]['id'])
        self.assertEqual(len(packet['demo']['plans']), len(data['plans']))
        # Admin still sees its full saved record; viewer projections do not alter it.
        self.assertIn('PRIVATE_PHONE_MARKER', json.dumps(self.request('/api/bootstrap', session=self.admin)[1]))
        result = initialize(generate(data, scope_confirmed=True))
        result['input_snapshot'] = data
        card = result['cards'][0]
        result = mutate(result, 'review', {'revision': 0, 'actor': 'PRIVATE_REVIEWER_MARKER',
            'card_id': card['card_id'], 'state': 'confirmed', 'action': card['action'], 'role': card['role'],
            'field_status': 'closed', 'note': 'PRIVATE_NOTE_MARKER'})
        result = mutate(result, 'notice', {'revision': 1, 'actor': 'PRIVATE_REVIEWER_MARKER', 'card_id': card['card_id']})
        with server.database() as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',
                       ('privacy-test', '2026-10-03', 'rules', json.dumps(data), json.dumps(result)))
        status, packet, _ = self.request('/api/run', {'run_id': 'privacy-test'}, viewer)
        self.assertEqual(status, 200)
        self.assertNotIn('PRIVATE_', json.dumps(packet))
        self.assertNotIn('input_snapshot', packet)
        self.assertNotIn('confirmed_by', packet['notices'][0])

    def test_viewer_keeps_automatic_weather_state(self):
        viewer = self.make_user('viewer')
        class MockMonitor:
            def snapshot(self):
                return {'weather': {'region': '杭州市', 'state': 'rain', 'label': '降雨',
                                    'basis': ['结构化气象观测'], 'providers': [{'status': 'fresh'}]}}
        with patch.object(server, 'MONITOR', MockMonitor()):
            status, packet, _ = self.request('/api/monitor', session=viewer)
        self.assertEqual(status, 200)
        self.assertEqual(packet['weather']['state'], 'rain')
        self.assertEqual(packet['weather']['label'], '降雨')
        self.assertEqual(packet['weather']['providers'][0]['status'], 'fresh')

    def test_logout_and_disabled_account_revoke_access(self):
        viewer = self.make_user('viewer')
        self.auth.manage_user('test-admin', {'username': 'viewer-user', 'role': 'viewer', 'disabled': True})
        self.assertEqual(self.request('/api/bootstrap', session=viewer)[0], 401)
        self.assertEqual(self.request('/api/auth/logout', {}, self.admin)[0], 200)
        self.assertEqual(self.request('/api/bootstrap', session=self.admin)[0], 401)


if __name__ == '__main__':
    unittest.main()
