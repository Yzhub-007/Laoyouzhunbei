"""Accepted local requests finish before runtime ownership is released."""
from pathlib import Path
import socket
from socketserver import BaseRequestHandler
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from instance_lock import InstanceRunningError, running_instance
import server


class ServerShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'runtime'

    def test_accepted_socket_has_bounded_read_timeout(self):
        service = server.LocalServer(('127.0.0.1', 0), BaseRequestHandler)
        self.addCleanup(service.server_close)
        with socket.create_connection(service.server_address, timeout=2):
            connection, _ = service.get_request()
            try:
                self.assertEqual(connection.gettimeout(), 15)
                self.assertFalse(service.daemon_threads)
                self.assertTrue(service.block_on_close)
            finally:
                connection.close()

    def test_close_waits_for_handler_and_keeps_runtime_owned(self):
        entered, release, ready, closing, finished = (threading.Event() for _ in range(5))
        errors = []

        class BlockedHandler(BaseRequestHandler):
            def handle(self):
                entered.set()
                if not release.wait(5):
                    errors.append('Test did not release handler')

        class TrackedServer(server.LocalServer):
            def server_close(self):
                closing.set()
                super().server_close()

        service = TrackedServer(('127.0.0.1', 0), BlockedHandler)

        def own_service():
            try:
                with running_instance(self.runtime):
                    ready.set()
                    try:
                        service.serve_forever(poll_interval=0.01)
                    finally:
                        service.server_close()
            except Exception as exc:
                errors.append(str(exc))
            finally:
                finished.set()

        owner = threading.Thread(target=own_service, daemon=True)
        owner.start()
        try:
            self.assertTrue(ready.wait(2), errors)
            with socket.create_connection(service.server_address, timeout=2):
                self.assertTrue(entered.wait(2))
                service.shutdown()
                self.assertTrue(closing.wait(2))
                self.assertFalse(finished.wait(0.1), 'server_close returned while a request was active')
                with self.assertRaises(InstanceRunningError):
                    with running_instance(self.runtime):
                        self.fail('Runtime was released before the active request completed')
                release.set()
                self.assertTrue(finished.wait(2))
            self.assertEqual(errors, [])
            with running_instance(self.runtime):
                pass
        finally:
            release.set()
            if not closing.is_set():
                service.shutdown()
            owner.join(timeout=3)
            service.server_close()

    def ownership_at_close(self, states):
        try:
            with running_instance(self.runtime):
                states.append(False)
        except InstanceRunningError:
            states.append(True)

    def test_initialization_failure_closes_inside_lock_and_again_safely(self):
        states = []
        service = Mock()
        service.server_close.side_effect = lambda: self.ownership_at_close(states)
        old_monitor = Mock()
        with (patch.object(server, 'LocalServer', return_value=service),
              patch.object(server, 'DB_PATH', self.runtime / 'records.sqlite3'),
              patch.object(server, 'MONITOR', old_monitor),
              patch.object(server, 'read_config', side_effect=ValueError('synthetic initialization failure')),
              patch('sys.argv', ['server.py', '--port', '0'])):
            with self.assertRaisesRegex(ValueError, 'synthetic initialization failure'):
                server.main()
        self.assertEqual(states, [True, False])
        service.serve_forever.assert_not_called()
        old_monitor.stop.assert_not_called()

    def test_monitor_start_failure_stops_it_before_releasing_runtime(self):
        states, calls = [], []
        service, monitor, auth = Mock(), Mock(), Mock()
        auth.status.return_value = {'setup_required': False}
        monitor.start.side_effect = ValueError('synthetic worker failure')
        monitor.stop.side_effect = lambda: calls.append('stop')

        def close():
            calls.append('close')
            self.ownership_at_close(states)

        service.server_close.side_effect = close
        with (patch.object(server, 'LocalServer', return_value=service),
              patch.object(server, 'DB_PATH', self.runtime / 'records.sqlite3'),
              patch.object(server, 'MONITOR', None), patch.object(server, 'AUTH', None),
              patch.object(server, 'LIVE', None), patch.object(server, 'CONFIG', {}),
              patch.object(server, 'read_config', return_value={}),
              patch.object(server, 'AuthService', return_value=auth),
              patch.object(server, 'LiveService', return_value=Mock()),
              patch.object(server, 'MonitorService', return_value=monitor),
              patch('sys.argv', ['server.py', '--port', '0'])):
            with self.assertRaisesRegex(ValueError, 'synthetic worker failure'):
                server.main()
        self.assertEqual(calls, ['stop', 'close', 'close'])
        self.assertEqual(states, [True, False])
        service.serve_forever.assert_not_called()


if __name__ == '__main__':
    unittest.main()
