"""Real Windows DPAPI tests use disposable fixtures, never production data."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import secure_storage as storage


@unittest.skipUnless(os.name == 'nt', 'Production storage requires Windows DPAPI')
class ProtectedStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'records.sqlite3'

    def create(self):
        with storage.database(self.path) as db:
            db.execute('CREATE TABLE records(value TEXT)')
            db.execute('INSERT INTO records VALUES (?)', ('synthetic-secret-123',))

    def test_database_is_ciphertext_and_row_factory_and_restart_work(self):
        self.create()
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(storage.MAGIC))
        self.assertNotIn(b'synthetic-secret-123', raw)
        self.assertNotIn(b'SQLite format 3', raw)
        with storage.database(self.path) as db:
            self.assertEqual(db.execute('SELECT * FROM records').fetchone()['value'], 'synthetic-secret-123')
        self.assertFalse(self.path.with_name(self.path.name + '-wal').exists())

    def test_read_only_transactions_keep_ciphertext_and_modification_time(self):
        self.create()
        before = self.path.read_bytes()
        fixed_time = 1_700_000_000_000_000_000
        os.utime(self.path, ns=(fixed_time, fixed_time))
        before_mtime = self.path.stat().st_mtime_ns
        with patch('secure_storage._atomic_write', wraps=storage._atomic_write) as write:
            for _ in range(3):
                with storage.database(self.path) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS records(value TEXT)')
                    self.assertEqual(db.execute('SELECT value FROM records').fetchone()[0], 'synthetic-secret-123')
            write.assert_not_called()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(before_mtime, self.path.stat().st_mtime_ns)

    def test_changed_transaction_is_persisted_and_protected(self):
        self.create()
        before = self.path.read_bytes()
        fixed_time = 1_700_000_000_000_000_000
        os.utime(self.path, ns=(fixed_time, fixed_time))
        with storage.database(self.path) as db:
            db.execute('UPDATE records SET value=?', ('synthetic-updated-456',))
        after = self.path.read_bytes()
        self.assertNotEqual(before, after)
        self.assertTrue(after.startswith(storage.MAGIC))
        self.assertNotIn(b'synthetic-updated-456', after)
        self.assertGreater(self.path.stat().st_mtime_ns, fixed_time)
        with storage.database(self.path) as db:
            self.assertEqual(db.execute('SELECT value FROM records').fetchone()[0], 'synthetic-updated-456')

    def test_new_empty_database_can_be_reopened_without_rewriting(self):
        with storage.database(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0], 0)
        before = self.path.read_bytes()
        self.assertTrue(before.startswith(storage.MAGIC))
        before_mtime = self.path.stat().st_mtime_ns
        with storage.database(self.path) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0], 0)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(before_mtime, self.path.stat().st_mtime_ns)

    def test_rollback_keeps_original_ciphertext(self):
        self.create()
        before = self.path.read_bytes()
        before_mtime = self.path.stat().st_mtime_ns
        with self.assertRaises(RuntimeError):
            with storage.database(self.path) as db:
                db.execute('DELETE FROM records')
                raise RuntimeError('cancel')
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(before_mtime, self.path.stat().st_mtime_ns)

    def test_wrong_purpose_and_ciphertext_tamper_fail_closed(self):
        self.create()
        with self.assertRaises(storage.StorageSecurityError):
            storage.read_json(self.path)
        raw = bytearray(self.path.read_bytes())
        raw[-5] ^= 0x40
        self.path.write_bytes(raw)
        with self.assertRaises(storage.StorageSecurityError):
            with storage.database(self.path):
                self.fail('Tampered encrypted data was opened')
        self.assertEqual(bytes(raw), self.path.read_bytes())

    def test_legacy_migration_has_encrypted_backup_and_no_plaintext_temporary(self):
        old = sqlite3.connect(self.path)
        old.execute('CREATE TABLE records(value TEXT)')
        old.execute('INSERT INTO records VALUES(?)', ('legacy-synthetic-private-text',))
        old.commit()
        old.close()
        with storage.database(self.path) as db:
            self.assertEqual(db.execute('SELECT value FROM records').fetchone()[0], 'legacy-synthetic-private-text')
        backup = self.path.with_name(self.path.name + '.migration.dpapi')
        self.assertTrue(backup.read_bytes().startswith(storage.MAGIC))
        for path in Path(self.temp.name).iterdir():
            self.assertNotIn(b'legacy-synthetic-private-text', path.read_bytes())

    def test_pending_journal_or_wal_refuses_migration(self):
        old = sqlite3.connect(self.path)
        old.execute('CREATE TABLE records(value TEXT)')
        old.close()
        original = self.path.read_bytes()
        for suffix in ('-wal', '-journal'):
            pending = self.path.with_name(self.path.name + suffix)
            pending.write_bytes(b'pending')
            with self.assertRaises(storage.StorageSecurityError):
                with storage.database(self.path):
                    self.fail('Uncheckpointed database was opened')
            self.assertEqual(original, self.path.read_bytes())
            pending.unlink()

    def test_migration_failure_keeps_legacy_database(self):
        old = sqlite3.connect(self.path)
        old.execute('CREATE TABLE records(value TEXT)')
        old.close()
        original = self.path.read_bytes()
        with patch('secure_storage._dpapi', side_effect=storage.StorageSecurityError('blocked')):
            with self.assertRaises(storage.StorageSecurityError):
                with storage.database(self.path):
                    pass
        self.assertEqual(original, self.path.read_bytes())

    def test_json_migration_default_and_nonfinite_rejection(self):
        path = Path(self.temp.name) / 'config.json'
        self.assertEqual(storage.read_json(path, {'default': 1}), {'default': 1})
        path.write_text(json.dumps({'secret': 'private-JSON-secret'}))
        self.assertEqual(storage.read_json(path)['secret'], 'private-JSON-secret')
        self.assertNotIn(b'private-JSON-secret', path.read_bytes())
        storage.write_json(path, {'secret': 'replacement'})
        self.assertEqual(storage.read_json(path), {'secret': 'replacement'})
        with self.assertRaises(ValueError):
            storage.write_json(path, {'bad': float('nan')})

    def test_thread_transactions_do_not_lose_updates(self):
        with storage.database(self.path) as db:
            db.execute('CREATE TABLE counter(value INTEGER)')
            db.execute('INSERT INTO counter VALUES(0)')
        def increment(_):
            for _ in range(6):
                with storage.database(self.path) as db:
                    value = db.execute('SELECT value FROM counter').fetchone()[0]
                    db.execute('UPDATE counter SET value=?', (value + 1,))
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(increment, range(5)))
        with storage.database(self.path) as db:
            self.assertEqual(db.execute('SELECT value FROM counter').fetchone()[0], 30)

    def test_process_transactions_do_not_lose_updates(self):
        with storage.database(self.path) as db:
            db.execute('CREATE TABLE counter(value INTEGER)')
            db.execute('INSERT INTO counter VALUES(0)')
        code = """import sys
from secure_storage import database
for _ in range(8):
    with database(sys.argv[1]) as db:
        value = db.execute('SELECT value FROM counter').fetchone()[0]
        db.execute('UPDATE counter SET value=?', (value + 1,))
"""
        processes = [subprocess.Popen([sys.executable, '-c', code, str(self.path)],
                                     cwd=Path(__file__).resolve().parents[1],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr.decode('utf-8', 'replace'))
        with storage.database(self.path) as db:
            self.assertEqual(db.execute('SELECT value FROM counter').fetchone()[0], 24)

    def test_acl_failure_is_fatal_and_does_not_write_plaintext(self):
        with patch('secure_storage.protect_file', side_effect=storage.StorageSecurityError('ACL denied')):
            with self.assertRaises(storage.StorageSecurityError):
                storage.write_json(self.path, {'secret': 'unwritten'})
        self.assertFalse(self.path.exists())

    def test_non_windows_explicitly_fails_closed(self):
        with patch('secure_storage.os.name', 'posix'):
            with self.assertRaises(storage.StorageSecurityError):
                storage.write_json(self.path, {'secret': 'unwritten'})


if __name__ == '__main__':
    unittest.main()
