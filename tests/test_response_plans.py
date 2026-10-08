"""Draft provenance, immutable versions, and real authenticated HTTP boundaries."""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import response_plans as rp
import server
from auth_support import admin_service, session_headers, TEST_PASSWORD
from engine import ValidationError


NOW = 1791000000


def dataset():
    data = json.loads((server.ROOT / 'data/zju_pilot.json').read_text(encoding='utf-8'))
    data['site'].update(name='测试楼宇', status='verified', scope='测试楼宇一层及已确认入口',
                        authorization={'status': 'confirmed', 'approver_role': '主管',
                                       'reference': '本地测试凭据', 'confirmed_at': rp.iso(NOW - 300)})
    data['points'] = data['points'][:2]
    for p in data['points']:
        p.update(name='测试点位 ' + p['id'], source='本地核验记录', status='verified',
                 verified_by='管理人员', verified_at=rp.iso(NOW - 200))
    data['plans'] = data['plans'][:2]
    for r in data['plans']:
        r.update(source='测试用已存预案', role='值班人员', status='verified', verified_by='主管',
                 verified_at=rp.iso(NOW - 200), valid_until=rp.iso(NOW + 1000))
    return data


def monitor():
    return {'served_at': rp.iso(NOW), 'sources': [
        {'id': 'cam-1', 'name': '入口监控', 'point_id': 'ZJG-P01', 'kind': 'camera', 'status': 'fresh',
         'observed_at': rp.iso(NOW - 1), 'max_age_seconds': 60,
         'url': 'https://secret-camera.example/private?token=secret', 'url_env': 'CAMERA_PASSWORD',
         'latest': {'summary': 'PRIVATE_RAW_MODEL_TEXT', 'frame_sha256': 'secret-hash'}}],
        'points': [{'point_id': 'ZJG-P01', 'state': 'pooling_suspected', 'basis': ['UNTRUSTED_PRIVATE_TEXT'],
                    'conflict': False, 'correction': None}],
        'weather': {'state': 'rain', 'label': '降雨参考', 'region': '杭州市', 'hazards': ['rain'],
                    'providers': [{'status': 'fresh', 'observed_at': rp.iso(NOW - 1)}]}}


def payload(**changes):
    value = {'point_ids': ['ZJG-P01'], 'details': {'title': '测试楼宇预案'}}
    value.update(changes)
    return value


class DocumentTests(unittest.TestCase):
    def build(self, data=None, snapshot=None, request=None, now=NOW):
        return rp.build_document(data or dataset(), snapshot, request or payload(), 'operator-test', now, 3)

    def test_without_monitor_keeps_unknown_and_does_not_use_simulation_weather(self):
        doc = self.build()
        self.assertEqual(doc['observations']['weather']['state'], 'unknown')
        self.assertEqual(doc['observations']['points'][0]['state'], 'unknown')
        self.assertEqual(doc['tasks'][0]['trigger_state'], 'standby')
        self.assertFalse(doc['publishable'])
        self.assertEqual(doc['tasks'][0]['status'], 'pending_review')
        self.assertTrue(any('责任岗位' in item for item in doc['missing_items']))
        self.assertTrue(any('疏散路线' in item for item in doc['missing_items']))

    def test_new_monitor_signal_is_structured_and_never_contains_raw_camera_data(self):
        doc = self.build(snapshot=monitor())
        self.assertEqual(doc['observations']['points'][0]['state'], 'pooling_suspected')
        self.assertEqual(doc['tasks'][0]['trigger_state'], 'signal_present')
        raw = json.dumps(doc)
        for forbidden in ('secret-camera', 'CAMERA_PASSWORD', 'PRIVATE_RAW_MODEL_TEXT', 'secret-hash', 'UNTRUSTED_PRIVATE_TEXT'):
            self.assertNotIn(forbidden, raw)

    def test_stale_future_or_failed_snapshot_cannot_claim_current_state(self):
        for served in (NOW - 61, NOW + 1):
            snapshot = monitor()
            snapshot['served_at'] = rp.iso(served)
            doc = self.build(snapshot=snapshot)
            self.assertEqual(doc['observations']['points'][0]['state'], 'unknown')
            self.assertEqual(doc['observations']['weather']['state'], 'unknown')
        for observed in (NOW - 60, NOW + 1):
            snapshot = monitor()
            snapshot['sources'][0]['observed_at'] = rp.iso(observed)
            self.assertEqual(self.build(snapshot=snapshot)['observations']['points'][0]['state'], 'unknown')

    def test_active_manual_verification_expires_and_is_labelled(self):
        snapshot = monitor()
        snapshot['sources'] = []
        snapshot['points'][0].update(state='clear', correction={
            'active': True, 'created_at': rp.iso(NOW - 20), 'expires_at': rp.iso(NOW + 20)})
        point = self.build(snapshot=snapshot)['observations']['points'][0]
        self.assertEqual(point['state'], 'clear')
        self.assertIn('人工校正', point['label'])
        snapshot['points'][0]['correction']['expires_at'] = rp.iso(NOW)
        self.assertEqual(self.build(snapshot=snapshot)['observations']['points'][0]['state'], 'unknown')

    def test_expired_correction_cannot_hide_automatic_water_signal(self):
        snapshot = monitor()
        snapshot['points'][0].update(state='clear', automatic_state='pooling_suspected', correction={
            'active': True, 'created_at': rp.iso(NOW - 30), 'expires_at': rp.iso(NOW)})
        point = self.build(snapshot=snapshot)['observations']['points'][0]
        self.assertEqual(point['state'], 'pooling_suspected')
        self.assertNotIn('人工校正', point['label'])

    def test_one_remaining_fresh_source_does_not_extend_expired_aggregate(self):
        snapshot = monitor()
        snapshot['served_at'] = rp.iso(NOW - 1)
        second = copy.deepcopy(snapshot['sources'][0])
        second['id'] = 'cam-2'
        snapshot['sources'][0]['observed_at'] = rp.iso(NOW - 60)
        snapshot['sources'].append(second)
        snapshot['points'][0].update(state='clear', automatic_state='clear')
        point = self.build(snapshot=snapshot)['observations']['points'][0]
        self.assertEqual(point['sources'][1]['status'], 'fresh')
        self.assertEqual(point['state'], 'unknown')
        self.assertTrue(any('原自动汇总已撤回' in line for line in point['basis']))
        snapshot['points'][0].update(state='pooling', correction={
            'active': True, 'created_at': rp.iso(NOW - 20), 'expires_at': rp.iso(NOW + 20)})
        self.assertEqual(self.build(snapshot=snapshot)['observations']['points'][0]['state'], 'pooling')

    def test_point_scope_and_input_validation(self):
        for request in (payload(point_ids=[]), payload(point_ids=['unknown']), payload(point_ids=['ZJG-P01'] * 2),
                        payload(details={'resources': 'x' * 4001}), payload(details={'title': 'x' * 161}),
                        payload(data={'weather': {'state': 'clear'}}), payload(details={'unexpected': 'test'})):
            with self.assertRaises(ValidationError):
                self.build(request=request)
        data = dataset()
        data['points'][0]['campus'] = '其他校区'
        with self.assertRaises(ValidationError):
            self.build(data=data)

    def test_matching_obeys_type_region_campus_expiry_and_point_ids(self):
        for changes in ({'point_types': ['outdoor']}, {'region': '其他城市'}, {'campus': '其他校区'},
                        {'point_ids': ['ZJG-P02']}, {'valid_until': rp.iso(NOW)}, {'valid_until': 'invalid'}):
            data = dataset()
            data['plans'][0].update(changes)
            doc = self.build(data=data, snapshot=monitor())
            self.assertFalse(doc['tasks'])
            self.assertTrue(any('没有适用' in item for item in doc['missing_items']))

    def test_missing_review_evidence_is_draft_and_cannot_auto_execute(self):
        data = dataset()
        data['plans'][0]['verified_by'] = ''
        data['points'][0]['verified_at'] = rp.iso(NOW + 1)
        doc = self.build(data=data, snapshot=monitor())
        self.assertEqual(doc['sources'][0]['status'], 'draft')
        self.assertEqual(doc['sources'][0]['recorded_status'], 'verified')
        self.assertFalse(doc['observations']['points'][0]['verified'])
        self.assertEqual(doc['tasks'][0]['status'], 'pending_review')

    def test_html_escapes_all_user_fields_and_has_no_script_or_remote_resources(self):
        data = dataset()
        injection = '<script src="https://evil.example/x">alert(1)</script>'
        data['plans'][0].update(action=injection, source=injection)
        doc = self.build(data=data, request=payload(details={'title': injection, 'contact_roster': injection}))
        rendered = rp.export_html(doc).decode()
        self.assertNotIn('<script', rendered)
        self.assertIn('&lt;script', rendered)
        self.assertIn("default-src 'none'", rendered)
        self.assertIn('不是正式发布文件', rendered)

    def test_read_checks_data_and_source_validity_without_rewriting_snapshot(self):
        original = self.build(snapshot=monitor())
        saved = copy.deepcopy(original)
        data = dataset()
        data['points'][0]['name'] = '变更后点位名称'
        updated = rp.with_update_status(original, data, None, NOW + 1001)
        self.assertEqual(original, saved)
        self.assertEqual(updated['observations'], saved['observations'])
        self.assertTrue(updated['update_status']['needs_refresh'])
        messages = ' '.join(updated['update_status']['messages'])
        for expected in ('历史快照', '资料已变更', '有效性已变化', '现已过期'):
            self.assertIn(expected, messages)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        rp.initialize(self.db)
        self.document = rp.build_document(dataset(), monitor(), payload(), 'operator', NOW)

    def test_append_only_versions_and_conflict(self):
        first = rp.append_version(self.db, self.document, 0)
        with self.assertRaises(rp.PlanConflict):
            rp.append_version(self.db, self.document, 0)
        second = rp.append_version(self.db, self.document, 1)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(rp.load_version(self.db, first['id']), first)
        self.assertEqual(rp.list_versions(self.db)['revision'], 2)
        with self.assertRaises(rp.PlanConflict):
            rp.append_version(self.db, self.document, True)

    def test_review_creates_new_draft_and_preserves_original(self):
        first = rp.append_version(self.db, self.document, 0)
        result = rp.review_version(self.db, {'id': first['id'], 'expected_revision': 1, 'review_note': '需补充值班联系人'},
                                   'admin', dataset(), None, NOW + 30)
        self.assertEqual(result['status'], 'reviewed_draft')
        self.assertEqual(result['parent_id'], first['id'])
        self.assertEqual(result['review']['by'], 'admin')
        self.assertFalse(result['publishable'])
        self.assertEqual(rp.load_version(self.db, first['id']), first)
        self.assertEqual(rp.list_versions(self.db)['revision'], 2)


class HTTPResponsePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth, self.admin = admin_service(Path(self.temp.name))
        self.auth.manage_user('test-admin', {'username': 'test-operator', 'password': TEST_PASSWORD, 'role': 'operator'})
        self.operator = self.auth.login({'username': 'test-operator', 'password': TEST_PASSWORD})
        self.auth.manage_user('test-admin', {'username': 'test-viewer', 'password': TEST_PASSWORD, 'role': 'viewer'})
        self.viewer = self.auth.login({'username': 'test-viewer', 'password': TEST_PASSWORD})
        self.patches = [patch.object(server, 'AUTH', self.auth), patch.object(server, 'MONITOR', None),
                        patch.object(server, 'DB_PATH', Path(self.temp.name) / 'test.sqlite3')]
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

    def request(self, path, data=None, session=None, headers=None, raw=False):
        values = {'Origin': self.url, 'Content-Type': 'application/json'}
        if session:
            values.update(session_headers(self.http, session))
        values.update(headers or {})
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=values)
        try:
            response = urlopen(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            return response.status, body if raw else json.loads(body), response.headers

    def test_all_sensitive_reads_require_cookie_and_second_proof_and_operator_role(self):
        for path in ('/api/response-plans', '/api/response-plans/' + 'a' * 32,
                     '/api/response-plans/' + 'a' * 32 + '/export?format=json'):
            self.assertEqual(self.request(path)[0], 401)
            self.assertEqual(self.request(path, session=self.admin, headers={'X-App-Token': ''})[0], 403)
            self.assertEqual(self.request(path, session=self.viewer)[0], 403)
        self.assertEqual(self.request('/api/response-plans', session=self.operator)[0], 200)

    def test_preview_and_save_permissions_identity_and_version_conflict(self):
        self.assertEqual(self.request('/api/response-plans/preview', payload(), self.viewer)[0], 403)
        self.assertEqual(self.request('/api/response-plans/save', payload(expected_revision=0), self.operator,
                                      {'Origin': 'https://evil.example'})[0], 403)
        status, packet, _ = self.request('/api/response-plans/preview', payload(), self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(packet['document']['created_by'], 'test-operator')
        status, packet, _ = self.request('/api/response-plans/save', payload(expected_revision=0), self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(packet['revision'], 1)
        self.assertEqual(self.request('/api/response-plans/save', payload(expected_revision=0), self.operator)[0], 409)

    def test_repeated_rejected_post_bodies_return_the_denial_response(self):
        for _ in range(20):
            status, packet, _ = self.request('/api/response-plans/save', payload(expected_revision=0), self.operator,
                                             {'Origin': 'https://evil.example'})
            self.assertEqual(status, 403)
            self.assertIn('同源', packet['error'])
        self.assertEqual(self.request('/api/response-plans', session=self.operator)[1]['revision'], 0)

    def test_review_only_admin_new_version_and_export_attachment(self):
        _, first, _ = self.request('/api/response-plans/save', payload(expected_revision=0), self.operator)
        record = first['document']['id']
        review = {'id': record, 'expected_revision': 1, 'review_note': '仅内部核对，现场待核验'}
        self.assertEqual(self.request('/api/response-plans/review', review, self.operator)[0], 403)
        status, reviewed, _ = self.request('/api/response-plans/review', review, self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(reviewed['document']['status'], 'reviewed_draft')
        self.assertEqual(self.request('/api/response-plans/' + record, session=self.operator)[1]['document']['status'], 'draft')
        for fmt in ('html', 'json'):
            status, body, headers = self.request('/api/response-plans/' + record + '/export?format=' + fmt,
                                                 session=self.operator, raw=True)
            self.assertEqual(status, 200)
            self.assertIn('attachment;', headers['Content-Disposition'])
            self.assertIn('filename*=UTF-8', headers['Content-Disposition'])
            self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
            self.assertIn('历史快照', body.decode())
        self.assertEqual(self.request('/api/response-plans/' + record + '/export?format=exe', session=self.admin)[0], 400)

    def test_monitor_failure_becomes_unknown_and_no_exception_details_leak(self):
        class FailedMonitor:
            def snapshot(self):
                raise RuntimeError('SECRET_CAMERA_CREDENTIALS')
        with patch.object(server, 'MONITOR', FailedMonitor()):
            status, result, _ = self.request('/api/response-plans/preview', payload(), self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(result['document']['observations']['points'][0]['state'], 'unknown')
        self.assertNotIn('SECRET_CAMERA_CREDENTIALS', json.dumps(result))

    def test_history_query_filters_and_invalid_parameters(self):
        self.request('/api/response-plans/save', payload(expected_revision=0, details={'title': 'West A'}), self.operator)
        self.request('/api/response-plans/save', payload(expected_revision=1, details={'title': 'East B'}), self.operator)
        status, result, _ = self.request('/api/response-plans?limit=1', session=self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(result['total'], 2)
        self.assertTrue(result['has_more'])
        status, result, _ = self.request('/api/response-plans?before=2&q=west&point_id=ZJG-P01&status=draft', session=self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(result['records'][0]['title'], 'West A')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['total'], 1)
        for suffix in ('?limit=101', '?before=0', '?limit=1&limit=2', '?q=x&q=y', '?status=published', '?extra=x'):
            self.assertEqual(self.request('/api/response-plans' + suffix, session=self.operator)[0], 400)
        self.assertEqual(self.request('/api/response-plans?q=west', session=self.viewer)[0], 403)


if __name__ == '__main__':
    unittest.main()
