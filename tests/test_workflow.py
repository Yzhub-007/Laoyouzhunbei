import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from engine import ValidationError, generate
from workflow import initialize, mutate
from test_engine import DEMO, CONFIG, Opener
from auth_support import admin_service, session_headers


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.result = initialize(generate(copy.deepcopy(DEMO), scope_confirmed=True))

    def review(self, result=None, **changes):
        result = result or self.result
        card = result['cards'][0]
        payload = dict(revision=result['revision'], card_id=card['card_id'], actor='演练审核员',
                       state='confirmed', action=card['action'], role=card['role'],
                       field_status='closed', note='桌面演练情景：管理人员确认关闭')
        payload.update(changes)
        return mutate(result, 'review', payload)

    def notice(self, result):
        return mutate(result, 'notice', dict(revision=result['revision'], actor='整理员', card_id='task-1'))

    def test_pending_cannot_generate_notice_or_complete(self):
        with self.assertRaises(ValidationError): self.notice(self.result)
        with self.assertRaises(ValidationError): self.review(state='completed')

    def test_full_review_feedback_and_processing(self):
        reviewed = self.review(action='人工修改的核查任务')
        self.assertEqual(self.result['revision'], 0)  # Immutable input.
        self.assertEqual(reviewed['cards'][0]['original_action'], self.result['cards'][0]['action'])
        notice = self.notice(reviewed)
        result = mutate(notice, 'feedback', dict(revision=2, actor='学生', notice_id='notice-2',
                                                status='received', note='已理解演练入口状态'))
        self.assertEqual(len(result['audit']), 3)
        self.assertFalse(result['publishable'])
        self.assertFalse(result['notices'][0]['published'])
        processing = self.review(result, state='processing')
        completed = self.review(processing, state='completed')
        self.assertEqual(completed['cards'][0]['status'], '已完成')

    def test_unknown_field_status_cannot_become_closed_notice(self):
        with self.assertRaisesRegex(ValidationError, '尚未确认'):
            self.notice(self.review(field_status='unknown'))

    def test_reopen_revokes_notice_and_feedback_is_blocked(self):
        result = self.notice(self.review())
        result = self.review(result, state='pending', action='修订后的任务')
        self.assertTrue(result['notices'][0]['revoked'])
        with self.assertRaises(ValidationError):
            mutate(result, 'feedback', dict(revision=3, actor='测试', notice_id='notice-2',status='received',note='测试'))

    def test_editing_confirmed_content_requires_reopen(self):
        with self.assertRaisesRegex(ValidationError, '退回'):
            self.review(self.review(), action='新的行动')

    def test_stale_revision_and_missing_actor_rejected(self):
        with self.assertRaisesRegex(ValidationError, '记录已更新'): self.review(revision=99)
        with self.assertRaises(ValidationError): self.review(actor='')

    def test_duplicate_notice_rejected(self):
        with self.assertRaisesRegex(ValidationError, '已有有效草稿'): self.notice(self.notice(self.review()))

    def test_ai_omissions_and_raw_response_are_recorded(self):
        raw = json.dumps({'matches': []})
        with patch('network_policy.NetworkPolicy.open', return_value=Opener(raw).open(None, 0)):
            result = generate(DEMO, 'ai', CONFIG, True)
        self.assertEqual(len(result['agent']['omitted_candidates']), 5)
        self.assertEqual(result['agent']['model_audit']['raw_output'], raw)
        self.assertNotIn('LLM_API_KEY', json.dumps(result))


class PersistenceTests(unittest.TestCase):
    def test_second_server_cannot_share_same_port(self):
        first = server.LocalServer(('127.0.0.1', 0), server.Handler)
        try:
            with self.assertRaises(OSError):
                second = server.LocalServer(first.server_address, server.Handler)
                second.server_close()
        finally:
            first.server_close()

    def test_http_workflow_persists_and_prevents_stale_write(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'DB_PATH', Path(folder) / 'test.sqlite3'):
            auth, issued = admin_service(Path(folder))
            auth_patch = patch.object(server, 'AUTH', auth)
            auth_patch.start()
            http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            def post(path, payload, token=None):
                headers = {'Content-Type': 'application/json', **session_headers(http, issued)}
                if token is not None:
                    headers['X-App-Token'] = token
                request = Request(f'http://127.0.0.1:{http.server_port}{path}', data=json.dumps(payload).encode(),
                                  headers=headers, method='POST')
                with urlopen(request) as response: return json.load(response)
            try:
                run = post('/api/generate', dict(data=DEMO, scope_confirmed=True))
                card = run['cards'][0]
                payload = dict(operation='review', run_id=run['run_id'], revision=0, card_id=card['card_id'],
                               actor='管理人员', state='confirmed', action=card['action'], role=card['role'],
                               field_status='unknown', note='依据台账核验任务')
                post('/api/workflow', payload)
                with self.assertRaises(HTTPError) as stale: post('/api/workflow', payload)
                self.assertEqual(stale.exception.code, 400)
                loaded = post('/api/run', dict(run_id=run['run_id']))
                self.assertEqual(loaded['revision'], 1)
                self.assertEqual(loaded['cards'][0]['workflow_state'], 'confirmed')
                self.assertEqual(len(post('/api/runs', {})['runs']), 1)
                with self.assertRaises(HTTPError) as auth: post('/api/run', dict(run_id=run['run_id']), token='invalid')
                self.assertEqual(auth.exception.code, 403)
            finally:
                http.shutdown(); http.server_close(); thread.join()
                auth_patch.stop()


if __name__ == '__main__': unittest.main()

