"""Process ownership tests only use disposable runtime directories."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from instance_lock import InstanceRunningError, LOCK_NAME, running_instance
from secure_storage import StorageSecurityError


@unittest.skipUnless(os.name == 'nt', 'Service instance locks require Windows')
class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'runtime'

    def child_try(self):
        script = '''
import sys
from instance_lock import InstanceRunningError, running_instance
try:
    with running_instance(sys.argv[1]):
        print('acquired')
except InstanceRunningError:
    print('busy')
    sys.exit(23)
'''
        return subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(self.runtime)],
                              cwd=Path(__file__).resolve().parent.parent,
                              capture_output=True, text=True, timeout=10)

    def test_nested_same_runtime_is_rejected(self):
        with running_instance(self.runtime):
            with self.assertRaisesRegex(InstanceRunningError, '已有服务正在运行'):
                with running_instance(self.runtime):
                    self.fail('Nested service entered')

    def test_second_process_is_rejected_and_can_enter_after_exit(self):
        with running_instance(self.runtime):
            result = self.child_try()
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(result.stdout.strip(), 'busy')
        result = self.child_try()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'acquired')

    def test_normal_exit_keeps_only_empty_marker_and_releases_lock(self):
        with running_instance(self.runtime):
            pass
        lock = self.runtime / LOCK_NAME
        self.assertEqual(lock.read_bytes(), b'\0')
        with running_instance(self.runtime):
            self.assertTrue(lock.is_file())
        self.assertEqual(lock.read_bytes(), b'\0')

    def test_exception_releases_lock(self):
        with self.assertRaisesRegex(ValueError, 'fixture'):
            with running_instance(self.runtime):
                raise ValueError('fixture')
        with running_instance(self.runtime):
            pass

    def test_different_runtimes_can_run_at_once(self):
        with running_instance(self.runtime):
            with running_instance(Path(self.temp.name) / 'other-runtime'):
                pass

    def test_process_termination_releases_kernel_lock(self):
        script = '''
import sys
from instance_lock import running_instance
with running_instance(sys.argv[1]):
    print('ready', flush=True)
    sys.stdin.readline()
'''
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(self.runtime)],
                                   cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with self.assertRaises(InstanceRunningError):
                with running_instance(self.runtime):
                    self.fail('Parent entered child runtime')
            process.terminate()
            process.wait(timeout=10)
            with running_instance(self.runtime):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_runtime_junction_and_linked_ancestor_are_rejected(self):
        target = Path(self.temp.name) / 'target'
        target.mkdir()
        junction = Path(self.temp.name) / 'linked'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        try:
            for path in (junction, junction / 'child-runtime'):
                with self.subTest(path=path), self.assertRaisesRegex(StorageSecurityError, '链接或目录联接'):
                    with running_instance(path):
                        self.fail('Linked runtime entered')
        finally:
            junction.rmdir()

    def test_lock_file_symlink_is_rejected(self):
        self.runtime.mkdir()
        target = Path(self.temp.name) / 'target-lock'
        target.write_bytes(b'\0')
        link = self.runtime / LOCK_NAME
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest('This Windows account cannot create symlinks: ' + str(exc.winerror))
        with self.assertRaisesRegex(StorageSecurityError, '链接或目录联接'):
            with running_instance(self.runtime):
                self.fail('Linked lock entered')
        self.assertEqual(target.read_bytes(), b'\0')


if __name__ == '__main__':
    unittest.main()
