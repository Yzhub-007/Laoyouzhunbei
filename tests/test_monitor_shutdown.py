"""Shutdown races use blocked synthetic fetches and no network calls."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from live_data import LiveService
from monitoring import MonitorService, iso

NOW = 1791000000
PILOT = {
    'site': {'region': '测试地区', 'campus': '测试园区', 'status': 'pending'},
    'points': [{'id': 'P1', 'name': '测试入口', 'type': 'entrance',
                'region': '测试地区', 'campus': '测试园区', 'status': 'pending'}],
    'plans': []}


class Weather:
    def snapshot(self):
        return {'providers': {}}


class MonitorShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'monitor.sqlite3'
        self.service = MonitorService(self.path, Weather(), lambda: copy.deepcopy(PILOT), clock=lambda: NOW)
        self.addCleanup(self.service.stop)
        self.entered, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)

    def wait_fetch(self):
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError('Test did not release the synthetic fetch')

    def finish_stopped(self):
        started = time.monotonic()
        self.service.stop()
        self.assertLess(time.monotonic() - started, 1.7)
        self.assertTrue(self.service.thread.is_alive())
        self.assertEqual(self.service.request_poll(), {'queued': False})
        self.release.set()
        self.service.thread.join(timeout=3)
        self.assertFalse(self.service.thread.is_alive())

    def configure(self, camera=False):
        source = {'id': 'sensor', 'name': '测试来源', 'point_id': 'P1',
                  'kind': 'camera' if camera else 'water_depth',
                  'transport': 'vision' if camera else 'file', 'enabled': True,
                  'max_age_seconds': 300}
        if camera:
            source.update(url='http://localhost/frame.jpg', input_type='image_url', vision_enabled=True)
        else:
            source['path'] = 'sensor.json'
        payload = self.service.get_config()
        payload['config']['sources'] = [source]
        self.service.save_config(payload)

    def test_late_weather_does_not_update_state_or_snapshot_events(self):
        def fetch():
            self.wait_fetch()
            return {'providers': {'model': {'weather_code': 61, 'data_epoch': NOW,
                                          'max_age_seconds': 600}}}
        self.service.live_service.snapshot = fetch
        self.service.snapshot()
        before = self.path.read_bytes()
        with patch.object(self.service, 'snapshot', wraps=self.service.snapshot) as snapshot:
            self.service.start()
            self.assertTrue(self.entered.wait(2))
            self.finish_stopped()
            snapshot.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.service.live_snapshot)
        self.assertIsNone(self.service.last_poll_at)

    def test_late_observation_does_not_ingest_or_create_events(self):
        self.configure()
        def fetch(*args):
            self.wait_fetch()
            return [{'observed_at': iso(NOW), 'quality': 'good', 'depth_cm': 30,
                     'depth_reference': 'ground'}]
        self.service.snapshot()
        before = self.path.read_bytes()
        with patch('monitoring.fetch_source', side_effect=fetch):
            self.service.start()
            self.assertTrue(self.entered.wait(2))
            self.finish_stopped()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.service.source_health, {})
        self.assertIsNone(self.service.last_poll_at)

    def test_late_source_failure_does_not_change_health_or_events(self):
        self.configure()
        def fetch(*args):
            self.wait_fetch()
            raise OSError('synthetic upstream failure')
        self.service.snapshot()
        before = self.path.read_bytes()
        with patch('monitoring.fetch_source', side_effect=fetch):
            self.service.start()
            self.assertTrue(self.entered.wait(2))
            self.finish_stopped()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.service.source_health, {})

    def test_late_unchanged_camera_does_not_refresh_health(self):
        self.configure(camera=True)
        def fetch(*args, **kwargs):
            self.wait_fetch()
            return None
        self.service.snapshot()
        before = self.path.read_bytes()
        with patch('camera_vision.capture_and_analyze', side_effect=fetch):
            self.service.start()
            self.assertTrue(self.entered.wait(2))
            self.finish_stopped()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.service.source_health, {})

    def test_stopped_manual_poll_does_not_fetch_or_write(self):
        self.service.stop()
        before = self.path.read_bytes()
        with patch.object(self.service.live_service, 'snapshot') as fetch:
            self.assertIsNone(self.service.poll_once())
            fetch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_during_fetch_triggers_another_poll_without_new_worker(self):
        second = threading.Event()
        calls = []
        def fetch():
            calls.append(1)
            if len(calls) == 1:
                self.wait_fetch()
            else:
                second.set()
            return {'providers': {}}
        self.service.live_service.snapshot = fetch
        self.service.start()
        worker = self.service.thread
        self.assertTrue(self.entered.wait(2))
        for _ in range(5):
            self.assertTrue(self.service.request_poll()['queued'])
        self.release.set()
        self.assertTrue(second.wait(3), 'Refresh request was lost during the active fetch')
        self.service.stop()
        self.assertEqual(len(calls), 2)
        self.assertIs(self.service.thread, worker)

    def test_late_worker_exception_does_not_replace_error_after_stop(self):
        def failed_poll():
            self.wait_fetch()
            raise RuntimeError('synthetic')
        self.service.last_error = '保留原状态'
        with patch.object(self.service, 'poll_once', side_effect=failed_poll):
            self.service.start()
            self.assertTrue(self.entered.wait(2))
            self.finish_stopped()
        self.assertEqual(self.service.last_error, '保留原状态')

    def test_late_real_weather_service_does_not_commit_its_cache(self):
        cache_path = Path(self.temp.name) / 'weather-cache.json'
        initial = {'model': {'checked_epoch': NOW - 3600, 'data': None, 'error': '旧状态'}}
        cache_path.write_text(json.dumps(initial), encoding='utf-8')
        def fetch(name):
            self.wait_fetch()
            return None, '测试结果'
        live = LiveService(cache_path, fetcher=fetch, clock=lambda: NOW)
        self.service.live_service = live
        before = cache_path.read_bytes()
        self.service.start()
        self.assertTrue(self.entered.wait(2))
        self.finish_stopped()
        self.assertEqual(cache_path.read_bytes(), before)
        self.assertEqual(live.cache, initial)
        self.assertFalse(cache_path.with_suffix('.tmp').exists())


if __name__ == '__main__':
    unittest.main()
