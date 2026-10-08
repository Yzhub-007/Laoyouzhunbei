"""A duplicate runtime cannot mutate account/config stores during startup."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from instance_lock import running_instance
import server


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.old_db = server.DB_PATH
        self.addCleanup(setattr, server, 'DB_PATH', self.old_db)

    def test_duplicate_runtime_is_rejected_before_configuration_or_accounts(self):
        argv = ['server.py', '--port', '0', '--db', str(self.runtime / 'runs.sqlite3')]
        with running_instance(self.runtime), patch('sys.argv', argv), \
             patch.object(server, 'read_config') as read_config, patch.object(server, 'AuthService') as auth:
            with self.assertRaises(SystemExit) as stopped:
                server.main()
            self.assertEqual(stopped.exception.code, 1)
            read_config.assert_not_called()
            auth.assert_not_called()

    def test_failed_initialization_releases_instance_lock(self):
        argv = ['server.py', '--port', '0', '--db', str(self.runtime / 'runs.sqlite3')]
        with patch('sys.argv', argv), patch.object(server, 'read_config', side_effect=ValueError('synthetic failure')):
            with self.assertRaisesRegex(ValueError, 'synthetic failure'):
                server.main()
        with running_instance(self.runtime):
            pass


if __name__ == '__main__':
    unittest.main()
