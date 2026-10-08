"""Synthetic monitoring fixtures test behaviour, not real campus conditions."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import ValidationError
from live_data import parse_model
from monitoring import DEFAULT_CONFIG, MonitorService, iso, MAX_ROWS_PER_SOURCE
import secure_storage

NOW = 1791000000
PILOT = {
    'site': {'region': '杭州市', 'campus': '紫金港校区', 'status': 'pending'},
    'points': [{'id': 'P1', 'name': '测试入口', 'type': 'entrance', 'region': '杭州市',
                'campus': '紫金港校区', 'status': 'pending'}],
    'plans': [{'id': 'R1', 'title': '核对信息', 'action': '由有权限人员核对记录', 'role': '管理人员',
               'hazards': ['rain', 'wind'], 'point_types': ['entrance'], 'status': 'draft'}]}


class FakeWeather:
    def __init__(self):
        self.calls = 0
        self.value = {'location': {'region': '杭州市'}, 'providers': {}}

    def snapshot(self):
        self.calls += 1
        return copy.deepcopy(self.value)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.dataset = copy.deepcopy(PILOT)
        self.live = FakeWeather()
        self.path = Path(self.temp.name) / 'monitor.sqlite3'
        self.service = MonitorService(self.path, self.live, lambda: self.dataset, clock=lambda: self.now)
        self.addCleanup(self.service.stop)

    def configure(self, sources=None, **kwargs):
        payload = self.service.get_config()
        payload['config'].update(kwargs)
        if sources is not None:
            payload['config']['sources'] = sources
        return self.service.save_config(payload)

    def source(self, kind='water_depth', sid='depth', **kwargs):
        return dict(id=sid, name=sid, point_id='P1', kind=kind, transport='push',
                    enabled=True, max_age_seconds=300, **kwargs)

    def depth(self, depth, ago=0, **kwargs):
        return dict(observed_at=iso(self.now - ago), depth_cm=depth, depth_reference='ground', **kwargs)

    def send(self, rows, sid='depth'):
        return self.service.ingest({'source_id': sid, 'observations': rows})

    def point(self):
        return self.service.snapshot()['points'][0]

    def model(self, **kwargs):
        return dict(data_epoch=self.now, data_time=iso(self.now), max_age_seconds=3600,
                    usable=True, **kwargs)

    def test_empty_has_no_fabricated_sources_or_weather(self):
        first = self.service.snapshot()
        self.assertEqual(first['points'][0]['state'], 'unknown')
        self.assertEqual(first['weather']['state'], 'unknown')
        self.assertEqual(first['sources'], [])
        self.assertEqual(first['tasks'], [])
        self.assertFalse(first['points'][0]['verified'])
        self.assertEqual(self.live.calls, 0)
        self.assertEqual(len(first['events']), len(self.service.snapshot()['events']))

    def test_snapshot_does_not_fetch_and_rechecks_expiry(self):
        self.live.value['providers']['model'] = self.model(weather_code=61)
        self.service.poll_once()
        self.assertEqual(self.service.snapshot()['weather']['state'], 'rain')
        self.now += 3601
        self.assertEqual(self.service.snapshot()['weather']['state'], 'unknown')
        self.assertEqual(self.live.calls, 1)

    def test_missing_rain_and_code_is_not_sunny(self):
        self.live.value['providers']['model'] = self.model(precipitation_mm=None, wind_speed_ms=0)
        self.service.poll_once()
        self.assertEqual(self.service.snapshot()['weather']['state'], 'unknown')

    def test_weather_rain_does_not_invent_flooding(self):
        self.live.value['providers']['model'] = self.model(weather_code=61, precipitation_mm=1, wind_speed_ms=12)
        self.live.value['providers']['station'] = self.model(weather_code='-SHRA', wind_speed_ms=2)
        snap = self.service.poll_once()
        self.assertEqual(snap['weather']['hazards'], ['rain', 'wind'])
        self.assertEqual(snap['weather']['providers'][0]['kind'], 'model_estimate')
        self.assertEqual(snap['weather']['providers'][1]['kind'], 'station_observation')
        self.assertEqual(snap['points'][0]['state'], 'unknown')
        self.assertEqual(len(snap['tasks']), 1)
        self.assertEqual(snap['tasks'][0]['status'], 'pending_review')
        self.assertEqual(snap['tasks'][0]['action'], self.dataset['plans'][0]['action'])
        self.assertEqual(len(self.service.snapshot()['tasks']), 1)

    def test_station_empty_present_weather_is_unknown(self):
        self.live.value['providers']['station'] = self.model(weather_code='未提供天气现象代码', wind_speed_ms=2)
        self.assertEqual(self.service.poll_once()['weather']['providers'][1]['state'], 'unknown')

    def test_independent_consecutive_samples_and_recovery(self):
        self.configure([self.source()])
        first = self.depth(20, ago=20)
        self.send([first])
        self.assertEqual(self.point()['state'], 'suspected')
        duplicate = self.send([first])
        self.assertEqual(duplicate['duplicates'], 1)
        self.assertEqual(self.point()['state'], 'suspected')
        self.send([self.depth(20, ago=10)])
        self.assertEqual(self.point()['state'], 'high_water')
        self.send([self.depth(0)])
        self.assertEqual(self.point()['state'], 'high_water')
        self.now += 10
        self.send([self.depth(0)])
        self.assertEqual(self.point()['state'], 'clear')
        self.now += 301
        self.assertEqual(self.point()['state'], 'unknown')

    def test_thresholds_and_out_of_order_time(self):
        self.configure([self.source()])
        self.send([self.depth(2, ago=10), self.depth(2)])
        self.assertEqual(self.point()['state'], 'pooling')
        self.send([self.depth(0, ago=30), self.depth(0, ago=20)])
        self.assertEqual(self.point()['state'], 'pooling')
        self.assertEqual(self.point()['observed_at'], iso(self.now))

    def test_continuous_depths_above_watch_confirm_even_across_alert_boundary(self):
        self.configure([self.source()])
        self.send([self.depth(20, ago=10), self.depth(5)])
        self.assertEqual(self.point()['state'], 'pooling')

    def test_atomic_batch_invalid_or_conflicting_rejected(self):
        self.configure([self.source()])
        for invalid in (float('nan'), float('inf'), True, -1, '2'):
            with self.assertRaises(ValidationError):
                self.send([self.depth(20, ago=10), self.depth(invalid)])
            self.assertEqual(self.point()['state'], 'unknown')
        with self.assertRaises(ValidationError):
            self.send([self.depth(20), self.depth(0)])
        self.assertEqual(self.point()['state'], 'unknown')
        self.send([self.depth(20)])
        with self.assertRaises(ValidationError):
            self.send([self.depth(20, ago=10), self.depth(0)])
        self.assertEqual(self.point()['state'], 'suspected')

    def test_calibration_and_timezone_required(self):
        self.configure([self.source()])
        row = self.depth(1)
        row.pop('depth_reference')
        with self.assertRaises(ValidationError):
            self.send([row])
        row = self.depth(1)
        row['observed_at'] = '2026-10-03T12:00:00'
        with self.assertRaises(ValidationError):
            self.send([row])

    def test_future_and_poor_never_used(self):
        self.configure([self.source()])
        self.send([self.depth(20, ago=-10)])
        self.assertEqual(self.point()['state'], 'unknown')
        self.assertEqual(self.service.snapshot()['sources'][0]['status'], 'future')
        self.now += 20
        self.send([self.depth(20, quality='poor')])
        self.assertEqual(self.point()['state'], 'unknown')

    def test_camera_cannot_measure_depth_and_conflicts_preserved(self):
        self.configure([self.source(), self.source('camera', 'cam')])
        camera = dict(observed_at=iso(self.now), water_detected=False, confidence=0.99, quality='good')
        self.send([dict(camera, observed_at=iso(self.now - 10)), camera], 'cam')
        self.send([self.depth(15, ago=10), self.depth(15)])
        point = self.point()
        self.assertEqual(point['state'], 'high_water')
        self.assertTrue(point['conflict'])
        self.assertEqual(set(point['sources']), {'cam', 'depth'})
        camera['depth_cm'] = 5
        with self.assertRaises(ValidationError):
            self.send([camera], 'cam')

    def test_camera_water_is_suspected_and_low_quality_unknown(self):
        self.configure([self.source('camera', 'cam')])
        row = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9)
        self.send([row], 'cam')
        self.assertEqual(self.point()['state'], 'pooling_suspected')
        self.now += 1
        self.send([dict(observed_at=iso(self.now), water_detected=None, confidence=0, quality='poor')], 'cam')
        self.assertEqual(self.point()['state'], 'unknown')

    def test_repeated_camera_image_cannot_refresh_observation_age(self):
        self.configure([self.source('camera', 'cam')])
        row = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9, frame_sha256='a' * 64)
        self.send([row], 'cam')
        self.now += 301
        row['observed_at'] = iso(self.now)
        self.assertEqual(self.send([row], 'cam')['duplicates'], 1)
        self.assertEqual(self.point()['state'], 'unknown')

    def test_camera_clear_requires_independent_continuous_valid_frames(self):
        self.configure([self.source('camera', 'cam')])
        row = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9, frame_sha256='a' * 64)
        self.send([row], 'cam')
        self.now += 1
        row.update(observed_at=iso(self.now), water_detected=False, frame_sha256='b' * 64)
        self.send([row], 'cam')
        self.assertEqual(self.point()['state'], 'pooling_suspected')
        self.now += 1
        row.update(observed_at=iso(self.now), confidence=0.1, frame_sha256='c' * 64)
        self.send([row], 'cam')
        self.assertEqual(self.point()['state'], 'unknown')
        self.now += 1
        row.update(observed_at=iso(self.now), confidence=0.9, frame_sha256='d' * 64)
        self.send([row], 'cam')
        self.assertEqual(self.point()['state'], 'unknown')
        self.now += 1
        row.update(observed_at=iso(self.now), frame_sha256='e' * 64)
        self.send([row], 'cam')
        self.assertEqual(self.point()['state'], 'clear')

    def test_correction_preserves_automatic_state_expires_and_new_alert_wins(self):
        self.configure([self.source()])
        correction = {'point_id': 'P1', 'state': 'clear', 'actor': '测试人员', 'note': '测试核对记录', 'ttl_minutes': 1}
        self.service.correct(correction)
        point = self.point()
        self.assertEqual(point['state'], 'clear')
        self.assertEqual(point['automatic_state'], 'unknown')
        self.now += 10
        self.send([self.depth(20, ago=1), self.depth(20)])
        point = self.point()
        self.assertEqual(point['state'], 'high_water')
        self.assertFalse(point['correction']['active'])
        self.now += 61
        self.assertFalse(self.point()['correction']['active'])

    def test_saved_config_and_observations_survive_restart(self):
        configured = self.configure([self.source()])
        self.send([self.depth(20, ago=10), self.depth(20)])
        restored = MonitorService(self.path, self.live, lambda: self.dataset, clock=lambda: self.now)
        self.assertEqual(restored.get_config(), configured)
        self.assertEqual(restored.snapshot()['points'][0]['state'], 'high_water')
        with self.assertRaises(ValidationError):
            self.service.save_config({'revision': 0, 'config': copy.deepcopy(DEFAULT_CONFIG)})

    def test_threshold_revision_reevaluates_existing_evidence_without_new_poll(self):
        first = self.configure([self.source()])
        self.send([self.depth(20, ago=10), self.depth(20)])
        self.assertEqual(self.point()['state'], 'high_water')
        second = self.configure(depth_watch_cm=25, depth_alert_cm=40)
        self.assertEqual(second['revision'], first['revision'] + 1)
        self.assertEqual(self.point()['state'], 'clear')
        self.assertEqual(self.live.calls, 0)

    def test_vision_unchanged_frame_updates_health_without_refreshing_evidence(self):
        source = self.source('camera', 'cam')
        source.update(transport='vision', input_type='image_url', url='http://localhost/camera.jpg', vision_enabled=True)
        self.configure([source])
        observation = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9, quality='good', frame_sha256='a' * 64)
        with patch('camera_vision.capture_and_analyze', side_effect=[observation, None]) as capture:
            self.service.poll_once()
            self.assertIsNone(capture.call_args.kwargs['previous_sha256'])
            self.now += 10
            self.service.source_health['cam']['error'] = '模拟暂时断线'
            self.service.poll_once()
            self.assertEqual(capture.call_args.kwargs['previous_sha256'], 'a' * 64)
        result = self.service.snapshot()['sources'][0]
        self.assertIsNone(result['error'])
        self.assertEqual(result['last_success_at'], iso(self.now))
        self.assertEqual(result['observed_at'], observation['observed_at'])
        self.now += 301
        self.assertEqual(self.point()['state'], 'unknown')

    def test_vision_timestamp_header_change_discards_old_evidence(self):
        source = self.source('camera', 'cam')
        source.update(transport='vision', input_type='image_url', url='http://localhost/camera.jpg', vision_enabled=True)
        self.configure([source])
        observation = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9, quality='good', frame_sha256='a' * 64)
        with patch('camera_vision.capture_and_analyze', return_value=observation):
            self.service.poll_once()
        self.assertEqual(self.point()['state'], 'pooling_suspected')
        source['timestamp_header'] = 'X-Camera-Capture-Time'
        self.configure([source])
        self.assertEqual(self.point()['state'], 'unknown')

    def test_privacy_area_and_consent_changes_invalidate_frame_cache(self):
        source = self.source('camera', 'cam')
        source.update(transport='vision', input_type='image_url', url='http://localhost/camera.jpg', vision_enabled=True)
        self.configure([source])
        observation = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9,
                           quality='good', frame_sha256='a' * 64)
        for changes in ({'roi': [0, 0, 0.5, 0.5]}, {'privacy_masks': [[0, 0, 0.2, 0.2]]},
                        {'url': 'http://localhost/other.jpg'}, {'vision_enabled': False}):
            with patch('camera_vision.capture_and_analyze', return_value=observation):
                self.service.poll_once()
            self.assertEqual(self.point()['state'], 'pooling_suspected')
            source.update(changes)
            self.configure([source])
            self.assertEqual(self.point()['state'], 'unknown')
            with self.service._db() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_observations').fetchone()[0], 0)

    def test_model_configuration_change_on_restart_invalidates_same_image(self):
        source = self.source('camera', 'cam')
        source.update(transport='vision', input_type='image_url', url='http://localhost/camera.jpg', vision_enabled=True)
        self.service.vision_config['VISION_MODEL'] = 'model-before'
        self.configure([source])
        observation = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9,
                           quality='good', frame_sha256='a' * 64)
        with patch('camera_vision.capture_and_analyze', return_value=observation):
            self.service.poll_once()
        same = MonitorService(self.path, self.live, lambda: self.dataset, clock=lambda: self.now,
                              vision_config={'VISION_MODEL': 'model-before'})
        self.assertEqual(same.snapshot()['points'][0]['state'], 'pooling_suspected')
        changed = MonitorService(self.path, self.live, lambda: self.dataset, clock=lambda: self.now,
                                 vision_config={'VISION_MODEL': 'model-after'})
        self.assertEqual(changed.snapshot()['points'][0]['state'], 'unknown')
        with patch('camera_vision.capture_and_analyze', return_value=observation) as capture:
            changed.poll_once()
            self.assertIsNone(capture.call_args.kwargs['previous_sha256'])

    def test_saved_privacy_change_recovers_after_interrupted_config_update(self):
        source = self.source('camera', 'cam')
        source.update(transport='vision', input_type='image_url', url='http://localhost/camera.jpg', vision_enabled=True)
        self.configure([source])
        observation = dict(observed_at=iso(self.now), water_detected=True, confidence=0.9,
                           quality='good', frame_sha256='a' * 64)
        with patch('camera_vision.capture_and_analyze', return_value=observation):
            self.service.poll_once()
        saved = self.service.get_config()
        saved['config']['sources'][0]['roi'] = [0.5, 0.5, 0.5, 0.5]
        saved['revision'] += 1
        secure_storage.write_json(self.service.config_path, saved)
        restored = MonitorService(self.path, self.live, lambda: self.dataset, clock=lambda: self.now)
        self.assertEqual(restored.snapshot()['points'][0]['state'], 'unknown')

    def test_inflight_result_from_previous_model_is_discarded(self):
        source = self.source('camera', 'cam')
        source.update(transport='vision', input_type='image_url', url='http://localhost/camera.jpg', vision_enabled=True)
        self.configure([source])
        def capture(*args, **kwargs):
            self.service.vision_config['VISION_MODEL'] = 'new-model'
            return dict(observed_at=iso(self.now), water_detected=True, confidence=0.9,
                        quality='good', frame_sha256='a' * 64)
        with patch('camera_vision.capture_and_analyze', side_effect=capture):
            self.service.poll_once()
        self.assertEqual(self.point()['state'], 'unknown')
        with self.service._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_observations').fetchone()[0], 0)

    def test_config_rejects_invalid_point_ranges_and_duplicate_ids(self):
        config = self.service.get_config()
        invalid_configs = []
        for key, value in [('poll_seconds', True), ('confirm_samples', 1), ('camera_confidence', float('nan')),
                           ('depth_alert_cm', 1), ('sources', [self.source(), self.source()])]:
            candidate = copy.deepcopy(config)
            candidate['config'][key] = value
            invalid_configs.append(candidate)
        for point in ('missing', []):
            candidate = copy.deepcopy(config)
            source = self.source()
            source['point_id'] = point
            candidate['config']['sources'] = [source]
            invalid_configs.append(candidate)
        for payload in invalid_configs:
            with self.assertRaises(ValidationError):
                self.service.save_config(payload)

    def test_file_transport_containment_and_failure(self):
        source = self.source()
        source.update(transport='file', path='sensor.json')
        self.configure([source])
        path = self.service.input_dir / 'sensor.json'
        path.write_text(json.dumps({'observations': [self.depth(5, ago=10), self.depth(5)]}), encoding='utf-8')
        self.assertEqual(self.service.poll_once()['points'][0]['state'], 'pooling')
        path.write_text('invalid json', encoding='utf-8')
        self.assertEqual(self.service.poll_once()['points'][0]['state'], 'unknown')
        self.assertEqual(self.service.snapshot()['sources'][0]['status'], 'error')
        for unsafe in ('../escape.json', str(self.path), 'nested/../../escape.json', 'sensor.txt'):
            source['path'] = unsafe
            with self.assertRaises(ValidationError):
                self.configure([source])

    def test_remapping_source_cannot_inherit_previous_point_evidence(self):
        self.configure([self.source()])
        self.send([self.depth(20, ago=10), self.depth(20)])
        self.dataset['points'].append(dict(self.dataset['points'][0], id='P2'))
        source = self.source()
        source['point_id'] = 'P2'
        self.configure([source])
        self.assertTrue(all(p['state'] == 'unknown' for p in self.service.snapshot()['points']))

    def test_tasks_respect_region_scope_and_plan_expiry(self):
        self.live.value['providers']['model'] = self.model(weather_code=61)
        self.live.value['location']['region'] = '福州市'
        self.assertEqual(self.service.poll_once()['tasks'], [])
        self.live.value['location']['region'] = '杭州市'
        for field, value in (('campus', '其他校区'), ('point_ids', ['P2']), ('valid_until', iso(self.now - 1)),
                             ('point_types', ['underground']), ('region', '福州市')):
            self.dataset['plans'][0] = dict(PILOT['plans'][0], **{field: value})
            self.assertEqual(self.service.poll_once()['tasks'], [])

    def test_retention_is_bounded(self):
        self.configure([self.source()])
        for offset in range(0, 300, 100):
            self.send([self.depth(1, ago=offset + i) for i in range(100)])
        with secure_storage.database(self.path) as db:
            count = db.execute('SELECT COUNT(*) FROM monitor_observations').fetchone()[0]
        self.assertEqual(count, MAX_ROWS_PER_SOURCE)

    def test_camera_free_text_and_ids_are_not_retained(self):
        self.configure([self.source('camera', 'cam')])
        private = 'synthetic-person-license-123456'
        row = dict(observed_at=iso(self.now), quality='good', water_detected=True,
                   confidence=0.9, summary=private, evidence=private, event_id=private, frame_sha256='a' * 64)
        self.send([row], 'cam')
        snapshot = self.service.snapshot()
        self.assertNotIn(private, json.dumps(snapshot))
        with self.service._db() as db:
            self.assertNotIn(private, db.execute('SELECT payload FROM monitor_observations').fetchone()[0])
        self.assertNotIn(private.encode(), self.path.read_bytes())
        self.assertTrue(self.service.config_path.read_bytes().startswith(secure_storage.MAGIC))

    def test_camera_file_http_and_vision_free_text_never_reaches_events(self):
        private = 'synthetic-private-face-and-license-9876'
        observation = dict(observed_at=iso(self.now), quality='good', water_detected=True,
                           confidence=0.9, summary=private, evidence=private, event_id=private)
        source = self.source('camera', 'cam')
        for transport in ('file', 'http', 'vision'):
            source['transport'] = transport
            if transport == 'file':
                source['path'] = 'camera.json'
                (self.service.input_dir / 'camera.json').write_text(json.dumps(observation), encoding='utf-8')
            else:
                source.pop('path', None)
                source['url'] = 'http://localhost/camera.json'
            if transport == 'vision':
                source.update(input_type='image_url', vision_enabled=True)
            self.configure([source])
            if transport == 'file':
                self.service.poll_once()
            elif transport == 'http':
                with patch('monitoring.fetch_source', return_value=[observation]):
                    self.service.poll_once()
            else:
                with patch('camera_vision.capture_and_analyze', return_value=observation):
                    self.service.poll_once()
            self.assertNotIn(private, json.dumps(self.service.snapshot()))
            with self.service._db() as db:
                self.assertNotIn(private.encode(), db.serialize())

    def test_retention_cleans_elapsed_records_but_keeps_recent_manual_note(self):
        self.configure([self.source()])
        self.send([self.depth(20)])
        self.service.correct({'point_id': 'P1', 'state': 'clear', 'actor': '测试人员',
                              'note': '应保留的人工核验依据', 'ttl_minutes': 1})
        self.service.snapshot()
        self.now += 8 * 86400
        self.service.snapshot()
        with self.service._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_observations').fetchone()[0], 0)
            self.assertIn('应保留的人工核验依据', db.execute('SELECT payload FROM monitor_corrections').fetchone()[0])
        self.now += 23 * 86400
        self.service.poll_once()
        with self.service._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_corrections').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_events WHERE created_epoch < ?',
                                        (self.now - 30 * 86400,)).fetchone()[0], 0)

    def test_retention_configuration_has_strict_bounds(self):
        for invalid in ('0', '31', True, '1.5', -1):
            with self.assertRaises(ValidationError):
                MonitorService(self.path, self.live, lambda: self.dataset,
                               vision_config={'SECURITY_OBSERVATION_DAYS': invalid})
        restored = MonitorService(self.path, self.live, lambda: self.dataset, clock=lambda: self.now,
                                  vision_config={'SECURITY_OBSERVATION_DAYS': '2'})
        self.assertEqual(restored.snapshot()['service']['retention_days']['observation'], 2)

    def test_legacy_camera_descriptions_and_event_basis_are_scrubbed_on_start(self):
        private = 'synthetic-legacy-person-id'
        with self.service._db() as db:
            db.execute("DELETE FROM monitor_security_meta WHERE key='privacy_templates_v1'")
            payload = dict(observed_at=iso(self.now), quality='good', water_detected=True,
                           confidence=0.9, summary=private, evidence=private)
            db.execute('INSERT INTO monitor_observations(source_id,observed_epoch,received_epoch,payload) VALUES(?,?,?,?)',
                       ('cam', self.now, self.now, json.dumps(payload)))
            db.execute('INSERT INTO monitor_events(entity,created_epoch,payload) VALUES(?,?,?)',
                       ('point:P1', self.now, json.dumps({'kind': 'point_state', 'basis': [private]})))
        restored = MonitorService(self.path, self.live, lambda: self.dataset, clock=lambda: self.now)
        with restored._db() as db:
            self.assertNotIn(private, db.execute('SELECT payload FROM monitor_observations').fetchone()[0])
            self.assertNotIn(private, db.execute('SELECT payload FROM monitor_events').fetchone()[0])

    def test_background_refresh_coalesces_and_snapshots_do_not_wait_for_io(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(2)
            return {'providers': {}}
        self.live.snapshot = blocked
        self.service.start()
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        self.service.snapshot()
        self.assertLess(time.monotonic() - before, 0.5)
        original = self.service.thread
        for _ in range(5):
            self.assertTrue(self.service.request_poll()['queued'])
            self.service.start()
            self.assertIs(self.service.thread, original)
        release.set()
        self.service.stop()
        self.assertFalse(self.service.thread.is_alive())


class WeatherCodeTests(unittest.TestCase):
    def fixture(self):
        return {'latitude': 26.07, 'longitude': 119.30,
                'current_units': {'time': 'unixtime', 'temperature_2m': '°C', 'relative_humidity_2m': '%',
                                  'precipitation': 'mm', 'wind_speed_10m': 'm/s', 'weather_code': 'wmo code'},
                'hourly_units': {'time': 'unixtime', 'precipitation': 'mm', 'wind_speed_10m': 'm/s'},
                'current': {'time': NOW, 'interval': 900, 'weather_code': 61},
                'hourly': {'time': [], 'precipitation': [], 'wind_speed_10m': []}}

    def test_weather_code_optional_and_strict(self):
        fixture = self.fixture()
        self.assertEqual(parse_model(fixture)['weather_code'], 61)
        for value in (True, '61', 61.5, 100, float('nan')):
            fixture['current']['weather_code'] = value
            with self.assertRaises(ValueError):
                parse_model(fixture)
        fixture['current'].pop('weather_code')
        self.assertIsNone(parse_model(fixture)['weather_code'])
        fixture['current']['weather_code'] = 61
        fixture['current_units']['weather_code'] = 'wrong'
        with self.assertRaises(ValueError):
            parse_model(fixture)


if __name__ == '__main__':
    unittest.main()
