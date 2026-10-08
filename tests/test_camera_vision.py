"""Camera capture/vision contracts with synthetic frames; no external model calls."""
import base64
import json
import io
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import camera_vision as vision
from engine import ValidationError
from PIL import Image, PngImagePlugin

NOW = 1791000000
_fixture = io.BytesIO()
Image.new('RGB', (20, 20), 'white').save(_fixture, format='PNG')
PNG = _fixture.getvalue()
CONFIG = {'VISION_ENDPOINT': 'http://127.0.0.1:8000/v1/chat/completions',
          'VISION_API_KEY': 'test-key-not-a-credential', 'VISION_MODEL': 'configured-vision-model'}


def verdict(**changes):
    result = {'water_detected': True, 'confidence': 0.84, 'quality': 'good', 'summary': '入口地面可见连续积水。'}
    result.update(changes)
    return result


def envelope(result=None):
    content = json.dumps(result if result is not None else verdict(), ensure_ascii=False)
    return json.dumps({'choices': [{'message': {'content': content}}]}, ensure_ascii=False).encode()


class Response:
    def __init__(self, data, headers=None, status=200):
        self.data, self.headers, self.status = data, headers or {}, status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, length):
        return self.data[:length]


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'monitor-input'
        self.root.mkdir()
        self.picture = self.root / 'gate.png'
        self.picture.write_bytes(PNG)
        os.utime(self.picture, (NOW - 2, NOW - 2))
        self.source = {'kind': 'camera', 'transport': 'vision', 'input_type': 'image_file',
                       'path': 'gate.png', 'vision_enabled': True, 'max_age_seconds': 300}

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, source=None, config=None):
        return vision.capture_and_analyze(source or self.source, self.root,
                                          CONFIG if config is None else config, clock=lambda: NOW)

    def url_source(self, **changes):
        source = {'kind': 'camera', 'transport': 'vision', 'input_type': 'image_url',
                  'url': 'http://127.0.0.1:9000/camera/snapshot.jpg', 'vision_enabled': True,
                  'max_age_seconds': 300}
        source.update(changes)
        return source

    def test_file_uses_original_mtime_and_base64_not_path(self):
        with patch.object(vision, 'NetworkPolicy') as builder:
            builder.return_value.open.return_value = Response(envelope())
            result = self.capture()
        self.assertEqual(result['observed_at'], vision._iso(NOW - 2))
        self.assertTrue(result['water_detected'])
        self.assertEqual(len(result['frame_sha256']), 64)
        request = builder.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], CONFIG['VISION_MODEL'])
        content = payload['messages'][1]['content']
        self.assertEqual(content[1]['image_url']['url'], 'data:image/png;base64,' + base64.b64encode(vision._prepare_image(PNG, self.source)).decode())
        self.assertNotIn('gate.png', json.dumps(payload))
        self.assertNotIn('gate.png', json.dumps(result))
        self.assertEqual([p.name for p in self.root.iterdir()], ['gate.png'])

    def test_disabled_or_incomplete_vision_never_reads_or_uploads(self):
        cases = [(dict(self.source, vision_enabled=False), CONFIG),
                 (self.source, {}),
                 (self.source, {'LLM_ENDPOINT': CONFIG['VISION_ENDPOINT'], 'LLM_API_KEY': 'key', 'LLM_MODEL': 'model'}),
                 (self.source, dict(CONFIG, VISION_MODEL=''))]
        for source, config in cases:
            with self.subTest(config=list(config), enabled=source['vision_enabled']), patch.object(vision, '_read_image') as read, patch.object(vision, 'NetworkPolicy') as http:
                with self.assertRaises(ValidationError):
                    self.capture(source, config)
                read.assert_not_called()
                http.assert_not_called()

    def test_model_endpoint_constraints_and_local_http(self):
        for endpoint in ['http://remote.example.test/v1/chat/completions',
                         'https://user:password@remote.example.test/v1',
                         'https://remote.example.test/v1?api_key=test',
                         'https://remote.example.test/v1#fragment']:
            with self.subTest(endpoint=endpoint):
                self.assertFalse(vision.vision_configured(dict(CONFIG, VISION_ENDPOINT=endpoint)))
        for endpoint in ['http://localhost:8000/v1/chat/completions', 'http://127.0.0.1:8000/v1', 'http://[::1]:8000/v1']:
            self.assertTrue(vision.vision_configured(dict(CONFIG, VISION_ENDPOINT=endpoint)))

    def test_source_path_cannot_escape(self):
        for path in ['../private.png', str(self.picture.resolve()), 'C:\\private.png']:
            with self.subTest(path=path), self.assertRaises(ValidationError):
                vision.validate_camera_source(dict(self.source, path=path), self.root)

    def test_camera_urls_reject_inline_secrets_but_allow_lan(self):
        vision.validate_camera_source(self.url_source(), self.root)
        for url in ['http://user:secret@camera/image.jpg', 'http://camera/image.jpg?api_key=secret',
                    'file:///private.png', 'http://camera/image.jpg#fragment']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                vision.validate_camera_source(self.url_source(url=url), self.root)

    def test_old_and_future_file_never_upload(self):
        for mtime in (NOW - 301, NOW + 5):
            os.utime(self.picture, (mtime, mtime))
            with patch.object(vision, '_analyze') as analyze, self.assertRaises(ValidationError):
                self.capture()
            analyze.assert_not_called()

    def test_url_uses_captured_header(self):
        with patch.object(vision, 'NetworkPolicy') as builder:
            builder.return_value.open.side_effect = [
                Response(PNG, {'X-Captured-At': vision._iso(NOW - 10)}), Response(envelope())]
            result = self.capture(self.url_source())
        self.assertEqual(result['observed_at'], vision._iso(NOW - 10))
        self.assertIn('X-Captured-At', result['evidence'])
        self.assertNotIn('192.168', json.dumps(result))

    def test_last_modified_and_custom_header(self):
        observed, header = vision._header_time({'Last-Modified': 'Thu, 01 Oct 2026 12:00:00 GMT'})
        self.assertEqual(vision._iso(observed), '2026-10-01T12:00:00+00:00')
        self.assertEqual(header, 'Last-Modified')
        observed, header = vision._header_time({'X-Frame-Time': vision._iso(NOW)}, 'X-Frame-Time')
        self.assertEqual(observed, NOW)
        self.assertEqual(header, 'X-Frame-Time')

    def test_missing_invalid_or_naive_header_never_upload(self):
        for headers in [{}, {'X-Captured-At': 'not-a-time'}, {'X-Captured-At': '2026-10-03T12:00:00'},
                        {'X-Captured-At': vision._iso(NOW + 5)}, {'Last-Modified': vision._iso(NOW - 301)}]:
            with self.subTest(headers=headers), patch.object(vision, 'NetworkPolicy') as builder:
                builder.return_value.open.return_value = Response(PNG, headers)
                with self.assertRaises(ValidationError):
                    self.capture(self.url_source())
                self.assertEqual(builder.return_value.open.call_count, 1)

    def test_same_picture_returns_same_fingerprint(self):
        with patch.object(vision, '_analyze', return_value=verdict()):
            first = self.capture()
            os.utime(self.picture, (NOW, NOW))
            second = self.capture()
        self.assertEqual(first['frame_sha256'], second['frame_sha256'])

    def test_unchanged_picture_skips_paid_call_and_does_not_refresh_time(self):
        with patch.object(vision, '_analyze', return_value=verdict()) as analyze:
            first = self.capture()
            os.utime(self.picture, (NOW, NOW))
            result = vision.capture_and_analyze(self.source, self.root, CONFIG,
                                               clock=lambda: NOW, previous_sha256=first['frame_sha256'])
        self.assertIsNone(result)
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(first['observed_at'], vision._iso(NOW - 2))

    def test_redirects_and_network_errors_are_redacted(self):
        for failure in [HTTPError('http://user:secret@camera', 302, 'redirect', {}, None),
                        URLError('http://user:secret@camera failed')]:
            with patch.object(vision, 'NetworkPolicy') as builder:
                builder.return_value.open.side_effect = failure
                with self.assertRaises(ValidationError) as caught:
                    self.capture(self.url_source())
                self.assertNotIn('secret', str(caught.exception))

    def test_oversize_wrong_content_and_excessive_dimensions(self):
        for data in [b'<html>login</html>', PNG * (vision.MAX_IMAGE_BYTES // len(PNG) + 1),
                     PNG[:16] + struct.pack('>II', 20000, 20000) + PNG[24:]]:
            with self.assertRaises(ValidationError):
                vision._image_type(data)
        self.assertEqual(vision._image_type(PNG), 'image/png')

    def test_model_response_strictness(self):
        bad_results = [verdict(water_detected='true'), verdict(water_detected=1),
                       verdict(confidence=True), verdict(confidence=float('nan')),
                       verdict(confidence=1.1), verdict(quality='unknown'),
                       verdict(water_detected=None, quality='good'), verdict(extra='field'),
                       verdict(summary=''), verdict(summary='a' * 1001)]
        for result in bad_results:
            with self.subTest(result=result), self.assertRaises(ValidationError):
                vision._parse_result(envelope(result))
        for raw in [b'{}', b'[]', b'not-json', json.dumps({'choices': [{'message': {'content': '```json\n{}\n```'}}]}).encode()]:
            with self.assertRaises(ValidationError):
                vision._parse_result(raw)

    def test_poor_quality_forces_unknown_even_if_model_claims_water(self):
        result = vision._parse_result(envelope(verdict(quality='poor', water_detected=True)))
        self.assertIsNone(result['water_detected'])

    def test_large_model_response_rejected(self):
        with patch.object(vision, 'NetworkPolicy') as builder:
            builder.return_value.open.return_value = Response(b' ' * (vision.MAX_RESPONSE_BYTES + 1))
            with self.assertRaises(ValidationError):
                self.capture()

    def test_no_ffmpeg_is_clear_and_does_not_affect_images(self):
        source = dict(self.url_source(), input_type='rtsp', url='rtsp://camera/live')
        with patch.object(vision.shutil, 'which', return_value=None), patch.object(vision.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(ValidationError, 'HTTPS抓拍'):
                self.capture(source)
        with patch.object(vision, '_analyze', return_value=verdict()):
            self.assertTrue(self.capture()['water_detected'])

    def test_rtsp_is_blocked_before_credentials_reach_subprocess(self):
        source = {'kind': 'camera', 'transport': 'vision', 'input_type': 'rtsp',
                  'url_env': 'CAMERA_GATE_01_RTSP', 'vision_enabled': True}
        config = dict(CONFIG, CAMERA_GATE_01_RTSP='rtsp://camera-user:private-password@camera/live')
        with patch.object(vision, '_ffmpeg', return_value='ffmpeg'), patch.object(vision.subprocess, 'run') as run, patch.object(vision, '_analyze', return_value=verdict()):
            with self.assertRaises(ValidationError) as caught:
                self.capture(source, config)
            run.assert_not_called()
        self.assertNotIn('private-password', str(caught.exception))
        self.assertNotIn('camera-user', str(caught.exception))

    def test_public_model_blocked_before_camera_read(self):
        configs = [dict(CONFIG, VISION_ENDPOINT='https://cloud.example/v1'),
                   dict(CONFIG, VISION_ENDPOINT='https://8.8.8.8/v1', VISION_TRUSTED_ORIGINS='https://8.8.8.8:443')]
        for config in configs:
            with self.subTest(config=config), patch.object(vision, '_read_image') as read, patch.object(vision, '_analyze') as analyze:
                with self.assertRaises(ValidationError):
                    self.capture(config=config)
                read.assert_not_called()
                analyze.assert_not_called()

    def test_remote_private_model_requires_roi_before_read(self):
        config = dict(CONFIG, VISION_ENDPOINT='https://192.168.1.10/v1', VISION_TRUSTED_ORIGINS='https://192.168.1.10:443')
        for source in (self.source, dict(self.source, roi=[0, 0, 1, 1])):
            with patch.object(vision, '_read_image') as read, self.assertRaisesRegex(ValidationError, 'ROI'):
                self.capture(source, config)
            read.assert_not_called()
        with patch.object(vision, '_analyze', return_value=verdict()) as analyze:
            self.capture(dict(self.source, roi=[0.25, 0.25, 0.5, 0.5]), config)
        with Image.open(io.BytesIO(analyze.call_args.args[0])) as prepared:
            self.assertEqual(prepared.size, (10, 10))

    def test_metadata_removed_and_mask_survives_crop(self):
        original = Image.new('RGB', (100, 80), 'white')
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('Description', 'plate ABC123; latitude=32; owner=private')
        exif = original.getexif()
        exif[270] = 'private camera metadata'
        raw = io.BytesIO()
        original.save(raw, format='PNG', pnginfo=metadata, exif=exif)
        processed = vision._prepare_image(raw.getvalue(), dict(self.source, roi=[0.2, 0.25, 0.5, 0.5], privacy_masks=[[0.2, 0.25, 0.1, 0.125]]))
        self.assertNotIn(b'private', processed)
        self.assertNotIn(b'ABC123', processed)
        with Image.open(io.BytesIO(processed)) as result:
            self.assertEqual(result.size, (50, 40))
            self.assertFalse(result.getexif())
            self.assertEqual(result.info, {})
            self.assertEqual(result.getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(result.getpixel((9, 9)), (0, 0, 0))
            self.assertEqual(result.getpixel((20, 20)), (255, 255, 255))

    def test_free_form_model_summary_never_enters_observation(self):
        result = vision._parse_result(envelope(verdict(summary='person=PrivateName; plate=ABC123')))
        self.assertNotIn('PrivateName', result['summary'])
        self.assertNotIn('ABC123', result['summary'])
        self.assertIn('待人工核验', result['summary'])

    def test_bad_roi_and_masks_rejected(self):
        for changes in ({'roi': [0, 0, 0, 1]}, {'roi': [0, 0, 1.1, 1]}, {'roi': [True, 0, 1, 1]},
                        {'privacy_masks': [[0.5, 0, 0.8, 1]]}, {'privacy_masks': 'none'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                vision.validate_camera_source(dict(self.source, **changes), self.root)

    def test_missing_pillow_fails_closed(self):
        with patch.dict('sys.modules', {'PIL': None}), self.assertRaisesRegex(ValidationError, 'Pillow'):
            vision._prepare_image(PNG, self.source)

    def test_ffmpeg_timeout_does_not_leak_command(self):
        with patch.object(vision, '_ffmpeg', return_value='ffmpeg'), patch.object(vision.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['rtsp://secret@camera'], 15)):
            with self.assertRaises(ValidationError) as caught:
                vision._frame('rtsp://secret@camera', CONFIG, rtsp=True)
            self.assertNotIn('secret', str(caught.exception))

    def test_local_video_disallows_playlist_demuxers_and_network_protocols(self):
        with patch.object(vision, '_ffmpeg', return_value='ffmpeg'), patch.object(vision.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=PNG)
            vision._frame(self.root / 'clip.mp4', CONFIG, offset=4)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'file,pipe')
        demuxers = args[args.index('-format_whitelist') + 1].split(',')
        self.assertNotIn('concat', demuxers)
        self.assertNotIn('hls', demuxers)
        self.assertNotIn('image2', demuxers)

    def test_recording_never_becomes_live_and_replay_has_no_observed_time(self):
        video = self.root / 'recording.mp4'
        video.write_bytes(b'synthetic-video-fixture')
        source = dict(self.source, input_type='video_file', path='recording.mp4', offset_seconds=10)
        with patch.object(vision, '_frame') as frame, self.assertRaisesRegex(ValidationError, '回放'):
            self.capture(source)
        frame.assert_not_called()
        with patch.object(vision, '_frame', return_value=PNG) as frame, patch.object(vision, '_analyze', return_value=verdict()):
            result = vision.analyze_video_replay(source, self.root, CONFIG, clock=lambda: NOW)
        self.assertTrue(result['replay'])
        self.assertEqual(result['mode'], 'replay')
        self.assertNotIn('observed_at', result)
        self.assertEqual(result['frame_offset_seconds'], 10)
        self.assertEqual(frame.call_args.kwargs, {'offset': 10})

    def test_capture_can_expire_during_analysis(self):
        with patch.object(vision, '_analyze', return_value=verdict()), self.assertRaisesRegex(ValidationError, '过期'):
            vision.capture_and_analyze(self.source, self.root, CONFIG, clock=iter([NOW, NOW + 301]).__next__)


if __name__ == '__main__':
    unittest.main()

