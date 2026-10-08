import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from secure_storage import read_json, MAGIC, StorageSecurityError


class SecretConfigurationTests(unittest.TestCase):
    def test_legacy_config_migrates_before_plaintext_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / '.env'
            legacy.write_text('VISION_API_KEY=private-test-marker\nVISION_MODEL=local-model\n', encoding='utf-8')
            with patch.object(server, 'ROOT', root), patch.dict('os.environ', {}, clear=True):
                config = server.read_config(root / 'runtime')
            self.assertEqual(config['VISION_API_KEY'], 'private-test-marker')
            self.assertNotIn('private-test-marker', legacy.read_text())
            encrypted = root / 'runtime' / 'secrets.dpapi'
            self.assertTrue(encrypted.read_bytes().startswith(MAGIC))
            self.assertNotIn(b'private-test-marker', encrypted.read_bytes())
            self.assertEqual(read_json(encrypted)['VISION_MODEL'], 'local-model')

    def test_migration_failure_keeps_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / '.env'
            original = 'VISION_API_KEY=private-test-marker\n'
            legacy.write_text(original, encoding='utf-8')
            with patch.object(server, 'ROOT', root), patch.object(server, 'write_json', side_effect=StorageSecurityError('test failure')):
                with self.assertRaises(StorageSecurityError):
                    server.read_config(root / 'runtime')
            self.assertEqual(legacy.read_text(), original)


if __name__ == '__main__':
    unittest.main()
