import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from build_release import REQUIRED, TOP_LEVEL, build_release, collect_files, reject_link


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        for name in REQUIRED:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('public fixture\n', encoding='utf-8')
        self.output = Path(self.temp.name) / 'release.zip'

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_excludes_private_trees_and_preserves_all_hashes(self):
        private_files = ('runtime/secrets.dpapi', 'runtime/accounts.dpapi', 'runtime/setup-code.txt',
                         'evidence/private-monitor.png', '.env', '.venv/private.py',
                         '__pycache__/server.pyc', 'tools/transfer_protected_owner.py',
                         'transfer_protected_owner.py', 'old.zip', 'data/private.json')
        for name in private_files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SECRET-DO-NOT-PACK', encoding='utf-8')
        (self.root / '.env.example').write_text('VISION_API_KEY=\nSECURITY_EVENT_DAYS=30\n', encoding='utf-8')
        original_read = Path.read_bytes

        def safe_read(path):
            if self.root in path.parents and path.relative_to(self.root).as_posix() in private_files:
                self.fail('Builder attempted to read a private file')
            return original_read(path)

        with patch.object(Path, 'read_bytes', safe_read):
            result = build_release(self.root, self.output)
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertTrue(all(name.startswith(TOP_LEVEL + '/') for name in names))
            self.assertTrue(all('..' not in Path(name).parts and '\\' not in name for name in names))
            self.assertFalse(any('SECRET-DO-NOT-PACK' in archive.read(name).decode('utf-8') for name in names))
            manifest = json.loads(archive.read(TOP_LEVEL + '/RELEASE_MANIFEST.json'))
            self.assertEqual(len(manifest['files']), len(names) - 1)
            for item in manifest['files']:
                body = archive.read(TOP_LEVEL + '/' + item['path'])
                self.assertEqual(item['size'], len(body))
                self.assertEqual(item['sha256'], hashlib.sha256(body).hexdigest())
            self.assertTrue(REQUIRED <= {item['path'] for item in manifest['files']})
            extraction = Path(self.temp.name) / 'extracted'
            archive.extractall(extraction)
            self.assertTrue((extraction / TOP_LEVEL / 'server.py').is_file())
        self.assertEqual(result['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertTrue(self.output.with_suffix('.zip.sha256').read_text().startswith(result['sha256']))

    def test_includes_new_reviewable_modules_and_only_named_data_templates(self):
        (self.root / 'future_module.py').write_text('module = True')
        (self.root / 'data/evaluation').mkdir()
        (self.root / 'data/evaluation/vision_acceptance_example.json').write_text('{}')
        (self.root / 'data/evaluation/customer_samples.json').write_text('private')
        names = dict(collect_files(self.root))
        self.assertIn('future_module.py', names)
        self.assertIn('data/evaluation/vision_acceptance_example.json', names)
        self.assertNotIn('data/evaluation/customer_samples.json', names)

    def test_missing_runtime_module_fails_closed(self):
        (self.root / 'secure_storage.py').unlink()
        with self.assertRaisesRegex(ValueError, 'secure_storage.py'):
            build_release(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_secret_in_example_is_not_released(self):
        (self.root / '.env.example').write_text('VISION_API_KEY=real-secret\n')
        with self.assertRaisesRegex(ValueError, '.env.example'):
            build_release(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_reparse_point_rejected(self):
        path = Mock()
        path.name = 'junction'
        path.lstat.return_value = Mock(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        with self.assertRaisesRegex(ValueError, 'junction'):
            reject_link(path)

    def test_symlink_cannot_escape_public_tree(self):
        outside_dir = Path(self.temp.name) / 'outside'
        outside_dir.mkdir()
        outside = outside_dir / 'private.py'
        outside.write_text('do not read')
        try:
            os.symlink(outside, self.root / 'linked.py')
        except OSError:
            if os.name != 'nt':
                self.skipTest('This account does not permit symbolic links')
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                     str(self.root / 'web' / 'junction'), str(outside_dir)],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.skipTest('This account does not permit symlinks or junctions; detection tested separately')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            collect_files(self.root)

    def test_same_sources_produce_identical_archive(self):
        first = build_release(self.root, self.output)
        second = build_release(self.root, self.output)
        self.assertEqual(first['sha256'], second['sha256'])


if __name__ == '__main__':
    unittest.main()
