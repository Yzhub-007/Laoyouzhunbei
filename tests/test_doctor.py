import importlib
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import doctor


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('server.py', 'secure_storage.py', 'auth.py', 'web/index.html', 'data/demo.json'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')

    def tearDown(self):
        self.temp.cleanup()

    def check_by_name(self, **kwargs):
        result = doctor.run_checks(self.root, **kwargs)
        return result, {check['name']: check for check in result['checks']}

    def test_probe_never_reads_runtime_or_imports_application(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read files')), \
             patch.object(Path, 'read_text', side_effect=AssertionError('must not read files')), \
             patch('doctor.current_username', return_value='Operator'):
            result, checks = self.check_by_name(skip_port=True)
        self.assertTrue(result['offline'])
        self.assertFalse(result['runtime_read'])
        self.assertEqual(checks['writable']['status'], 'pass')
        self.assertFalse((self.root / 'runtime').exists())
        self.assertFalse(list(self.root.glob('.doctor-*')))

    def test_missing_pillow_has_actionable_failure(self):
        original_import = importlib.import_module

        def import_module(name):
            if name.startswith('PIL'):
                raise ImportError('absent')
            return original_import(name)

        with patch('doctor.importlib.import_module', side_effect=import_module):
            result, checks = self.check_by_name(skip_port=True)
        self.assertFalse(result['ok'])
        self.assertEqual(checks['pillow']['status'], 'error')
        self.assertIn('setup.ps1', checks['pillow']['message'])

    def test_formal_startup_rejects_sandbox_identity(self):
        with patch('doctor.current_username', return_value='CodexSandboxOffline'):
            result, checks = self.check_by_name(check_startup=True, skip_port=True)
            _, diagnostic = self.check_by_name(skip_port=True)
        self.assertFalse(result['ok'])
        self.assertEqual(checks['account']['status'], 'error')
        self.assertEqual(diagnostic['account']['status'], 'warning')

    def test_port_conflict_is_reported_without_connecting(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            result, checks = self.check_by_name(port=listener.getsockname()[1])
        self.assertFalse(result['ok'])
        self.assertEqual(checks['port']['status'], 'error')

    def test_missing_files_are_named(self):
        (self.root / 'server.py').unlink()
        result, checks = self.check_by_name(skip_port=True)
        self.assertFalse(result['ok'])
        self.assertIn('server.py', checks['files']['message'])


if __name__ == '__main__':
    unittest.main()
