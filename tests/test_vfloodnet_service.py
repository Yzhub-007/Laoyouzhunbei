"""V-FloodNet sidecar request/response contract tests without model weights."""
import base64
import io
import json
import unittest

from PIL import Image

from integrations.v_floodnet import service


class VFloodNetServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        buffer = io.BytesIO()
        Image.new('RGB', (8, 6), 'blue').save(buffer, format='PNG')
        cls.png = buffer.getvalue()

    def payload(self, data=None, media_type='image/png'):
        encoded = base64.b64encode(self.png if data is None else data).decode('ascii')
        return {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'analyze'},
            {'type': 'image_url', 'image_url': {'url': f'data:{media_type};base64,{encoded}'}}
        ]}]}

    def test_extracts_single_local_image(self):
        data, media_type = service.extract_image_data(self.payload())
        self.assertEqual(data, self.png)
        self.assertEqual(media_type, 'image/png')

    def test_rejects_remote_multiple_or_invalid_images(self):
        cases = [
            {'messages': []},
            {'messages': [{'content': [{'type': 'image_url', 'image_url': {'url': 'https://example.test/a.png'}}]}]},
            self.payload(media_type='image/gif'),
        ]
        multiple = self.payload()
        multiple['messages'][0]['content'].append(multiple['messages'][0]['content'][-1])
        cases.append(multiple)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(service.RequestError):
                service.extract_image_data(payload)

    def test_openai_compatible_response_contains_strict_verdict(self):
        verdict = {'water_detected': True, 'confidence': 0.81, 'quality': 'good',
                   'summary': 'local segmentation result'}
        response = service.chat_response(verdict)
        content = json.loads(response['choices'][0]['message']['content'])
        self.assertEqual(content, verdict)
        self.assertEqual(response['model'], service.DEFAULT_MODEL_NAME)

    def test_threshold_validation_does_not_import_torch(self):
        for value in (0, 1, -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.VFloodNetEngine('missing.pth', mask_threshold=value)
            with self.subTest(fraction=value), self.assertRaises(ValueError):
                service.VFloodNetEngine('missing.pth', min_water_fraction=value)


if __name__ == '__main__':
    unittest.main()
