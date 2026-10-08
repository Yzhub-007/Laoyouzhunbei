"""HTTP integration with synthetic local observations, never live field evidence."""
import copy
import base64
from io import BytesIO
from PIL import Image
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server
from monitoring import MonitorService
from auth_support import admin_service, session_headers


class OfflineWeather:
    def snapshot(self):
        return {'location': {'region': '杭州市'}, 'providers': {
            'model': {'usable': False, 'status': 'unavailable'},
            'station': {'usable': False, 'status': 'unavailable'}}}


class MonitorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / 'test.sqlite3'
        self.base = json.loads((server.ROOT / 'data/zju_pilot.json').read_text(encoding='utf-8'))
        self.now = time.time()
        self.monitor = MonitorService(self.db_path, OfflineWeather(), lambda: copy.deepcopy(self.base), clock=lambda: self.now)
        self.auth, self.issued = admin_service(Path(self.temp.name))
        self.patches = [patch.object(server, 'DB_PATH', self.db_path),
                        patch.object(server, 'AUTH', self.auth),
                        patch.object(server, 'MONITOR', self.monitor)]
        for item in self.patches:
            item.start()
        self.http = server.LocalServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)

    def tearDown(self):
        self.monitor.stop()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def request(self, path, payload=None, token=True, origin=None):
        headers = {'Content-Type': 'application/json', **session_headers(self.http, self.issued)}
        if token:
            headers['X-App-Token'] = self.issued['csrf_token']
        else:
            headers.pop('X-App-Token', None)
        if origin:
            headers['Origin'] = origin
        req = Request(self.url + path, headers=headers,
                      data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def configure(self):
        status, config = self.request('/api/monitor/config')
        self.assertEqual(status, 200)
        config['config']['sources'] = [{
            'id': 'camera-test', 'name': '合成监测源', 'point_id': 'ZJG-P01',
            'kind': 'camera', 'transport': 'push', 'enabled': True,
            'max_age_seconds': 300}]
        status, saved = self.request('/api/monitor/config', config)
        self.assertEqual(status, 200, saved)
        return config

    def test_no_camera_is_unknown_not_clear(self):
        status, snapshot = self.request('/api/monitor')
        self.assertEqual(status, 200)
        self.assertTrue(snapshot['points'])
        self.assertTrue(all(p['state'] == 'unknown' for p in snapshot['points']))
        self.assertEqual(snapshot['tasks'], [])

    def test_iso_roundtrip_does_not_mislabel_current_observation_as_future(self):
        self.configure()
        self.now = 1791000000.1234567
        observation = {'observed_at': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                       'water_detected': True, 'confidence': 0.95, 'quality': 'good'}
        self.assertEqual(self.request('/api/monitor/ingest', {'source_id': 'camera-test', 'observations': [observation]})[0], 200)
        _, snapshot = self.request('/api/monitor')
        self.assertEqual(snapshot['sources'][0]['status'], 'fresh')
        self.assertEqual(snapshot['points'][0]['state'], 'pooling_suspected')
        observation['observed_at'] = datetime.fromtimestamp(self.now + 2, timezone.utc).isoformat()
        self.assertEqual(self.request('/api/monitor/ingest', {'source_id': 'camera-test', 'observations': [observation]})[0], 200)
        _, snapshot = self.request('/api/monitor')
        self.assertEqual(snapshot['sources'][0]['status'], 'future')
        self.assertEqual(snapshot['points'][0]['state'], 'unknown')

    def test_mutation_requires_token_and_local_origin(self):
        for path in ('config', 'ingest', 'correction', 'refresh'):
            self.assertEqual(self.request('/api/monitor/' + path, {}, token=False)[0], 403)
            self.assertEqual(self.request('/api/monitor/' + path, {}, origin='https://untrusted.example')[0], 403)

    def test_config_revision_push_and_expiry(self):
        old_config = self.configure()
        self.assertEqual(self.request('/api/monitor/config', old_config)[0], 400)
        observation = {'observed_at': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                       'water_detected': True, 'confidence': 0.95, 'quality': 'good'}
        status, result = self.request('/api/monitor/ingest', {'source_id': 'camera-test', 'observations': [observation]})
        self.assertEqual(status, 200, result)
        _, snapshot = self.request('/api/monitor')
        self.assertIn(snapshot['points'][0]['state'], ('suspected', 'pooling_suspected'))
        self.now += 301
        _, snapshot = self.request('/api/monitor')
        self.assertEqual(snapshot['points'][0]['state'], 'unknown')

    def test_foreign_point_source_is_rejected(self):
        _, config = self.request('/api/monitor/config')
        config['config']['sources'] = [{
            'id': 'bad', 'name': '不存在点位', 'point_id': 'foreign', 'kind': 'camera',
            'transport': 'push', 'enabled': True, 'max_age_seconds': 300}]
        self.assertEqual(self.request('/api/monitor/config', config)[0], 400)

    def test_http_snapshot_to_vision_to_point_end_to_end(self):
        # Real local HTTP exchanges, synthetic image and model response.
        image_buffer = BytesIO()
        Image.new('RGB', (4, 4), (20, 40, 60)).save(image_buffer, format='PNG')
        png = image_buffer.getvalue()
        calls = []
        captured = datetime.fromtimestamp(self.now - 1, timezone.utc).isoformat()
        class CameraAndVision(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('X-Captured-At', captured)
                self.send_header('Content-Length', str(len(png)))
                self.end_headers()
                self.wfile.write(png)

            def do_POST(self):
                packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(packet)
                output = {'water_detected': True, 'confidence': 0.93, 'quality': 'good',
                          'summary': '合成模型响应：用于协议联调，不是真实现场。'}
                raw = json.dumps({'choices': [{'message': {'content': json.dumps(output)}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), CameraAndVision)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(upstream.server_port)
        try:
            self.monitor.vision_config = {'VISION_ENDPOINT': url + '/vision',
                                         'VISION_API_KEY': 'synthetic-test', 'VISION_MODEL': 'synthetic'}
            _, config = self.request('/api/monitor/config')
            config['config']['sources'] = [{'id': 'snapshot', 'name': '合成HTTP抓拍',
                'point_id': 'ZJG-P01', 'kind': 'camera', 'transport': 'vision', 'input_type': 'image_url',
                'url': url + '/snapshot.png', 'vision_enabled': True, 'enabled': True, 'max_age_seconds': 300}]
            status, saved = self.request('/api/monitor/config', config)
            self.assertEqual(status, 200, saved)
            self.monitor.poll_once()
            status, snapshot = self.request('/api/monitor')
            self.assertEqual(status, 200)
            self.assertEqual(snapshot['points'][0]['state'], 'pooling_suspected', snapshot['sources'])
            self.assertEqual(len(calls), 1)
            supplied = calls[0]['messages'][1]['content'][1]['image_url']['url']
            transmitted = Image.open(BytesIO(base64.b64decode(supplied.split(',', 1)[1])))
            self.assertEqual(transmitted.size, (4, 4))
            self.assertEqual(transmitted.convert('RGB').getpixel((0, 0)), (20, 40, 60))
            self.assertFalse(transmitted.getexif())
            self.monitor.poll_once()
            self.assertEqual(len(calls), 1, '重复帧应在视觉调用前去重')
            self.assertTrue(any(task['point_id'] == 'ZJG-P01' for task in snapshot['tasks']))
            self.assertFalse(snapshot['service'].get('external_ai_verified', False))
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
