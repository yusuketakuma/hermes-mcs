"""Synthetic committed-log verification across a changed mount device number."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_log


@contextmanager
def imported_log():
    with tempfile.TemporaryDirectory(prefix='mcs-log-device-') as temporary:
        data = Path(temporary).resolve()
        data.chmod(0o700)
        source = data / 'update.log'
        source.write_bytes(b'synthetic legacy\n')
        source.chmod(0o600)
        run_log.migrate(data, ['update.log'])
        state = json.loads((data / run_log.STATE).read_bytes())
        state['sources']['update.log']['identity'][0] += 1
        save_state(data, state)
        yield data, source, state


def save_state(data, state):
    for path in (data / run_log.STATE, data / run_log.BACKUPS / run_log.STATE):
        path.write_text(json.dumps(state))


class RunLogDeviceChangeTests(unittest.TestCase):
    def test_device_only_change_is_durable_and_never_reimports(self):
        with imported_log() as (data, source, _state):
            before = (data / run_log.LOG).read_bytes()
            for _ in range(2):
                self.assertEqual(run_log.migrate(data, ['update.log'])['imported'], [])
                self.assertEqual((data / run_log.LOG).read_bytes(), before)
            canonical = json.loads((data / run_log.BACKUPS / run_log.STATE).read_bytes())
            self.assertEqual(canonical['sources']['update.log']['identity'],
                             [source.stat().st_dev, source.stat().st_ino])
            self.assertEqual(json.loads((data / run_log.STATE).read_bytes()), canonical)

    def test_device_change_imports_only_verified_tail(self):
        with imported_log() as (data, source, _state):
            with source.open('ab') as stream:
                stream.write(b'synthetic tail\n')
            self.assertEqual(run_log.migrate(data, ['update.log'])['imported'], ['update.log'])
            self.assertEqual((data / run_log.LOG).read_bytes(), source.read_bytes())
            self.assertEqual(run_log.migrate(data, ['update.log'])['imported'], [])

    def test_changed_or_missing_evidence_is_rejected_without_rebinding(self):
        for failure in ('inode', 'prefix', 'shorter', 'backup_missing', 'backup_content',
                        'backup_shorter', 'backup_name'):
            with self.subTest(failure=failure), imported_log() as (data, source, state):
                record = state['sources']['update.log']
                archived = data / run_log.BACKUPS / record['backup']
                if failure == 'inode':
                    record['identity'][1] += 1
                elif failure == 'prefix':
                    source.write_bytes(b'x' + source.read_bytes()[1:])
                elif failure == 'shorter':
                    source.write_bytes(b'short')
                elif failure == 'backup_missing':
                    archived.unlink()
                elif failure == 'backup_content':
                    archived.write_bytes(b'x' + archived.read_bytes()[1:])
                elif failure == 'backup_shorter':
                    archived.write_bytes(b'short')
                else:
                    record['backup'] = '../synthetic'
                save_state(data, state)
                before = (data / run_log.STATE).read_bytes()
                log_before = (data / run_log.LOG).read_bytes()
                with self.assertRaises((run_log.RunLogError, OSError)):
                    run_log.migrate(data, ['update.log'])
                self.assertEqual((data / run_log.STATE).read_bytes(), before)
                self.assertEqual((data / run_log.LOG).read_bytes(), log_before)

    def test_source_or_backup_change_during_verification_is_rejected(self):
        for target in ('source_content', 'source_path', 'backup_content', 'backup_path'):
            with self.subTest(target=target), imported_log() as (data, source, state):
                archived = data / run_log.BACKUPS / state['sources']['update.log']['backup']
                digest = run_log._digest
                changed = []

                def racing_digest(fd, end, start=0):
                    result = digest(fd, end, start)
                    expected = source if target.startswith('source') else archived
                    if not changed and os.fstat(fd).st_ino == expected.stat().st_ino:
                        changed.append(True)
                        if target.endswith('path'):
                            replacement = expected.with_name('synthetic-replacement')
                            replacement.write_bytes(expected.read_bytes())
                            replacement.chmod(0o600)
                            os.replace(replacement, expected)
                        else:
                            expected.write_bytes(b'x' + expected.read_bytes()[1:])
                    return result

                before = (data / run_log.STATE).read_bytes()
                with patch.object(run_log, '_digest', racing_digest):
                    with self.assertRaises(run_log.RunLogError):
                        run_log.migrate(data, ['update.log'])
                self.assertTrue(changed)
                self.assertEqual((data / run_log.STATE).read_bytes(), before)

    def test_unfinished_pending_is_not_rebound(self):
        with imported_log() as (data, _source, state):
            record = state['sources']['update.log']
            live = (data / run_log.LOG).stat()
            state['pending'] = {'kind': 'append', 'stage': record['backup'],
                                'start': 0, 'end': record['offset'], 'offset': 0,
                                'sha256': record['sha256'], 'source': None,
                                'identity': [live.st_dev + 1, live.st_ino]}
            save_state(data, state)
            before = (data / run_log.STATE).read_bytes()
            with self.assertRaises(run_log.RunLogError):
                run_log.migrate(data, ['update.log'])
            self.assertEqual((data / run_log.STATE).read_bytes(), before)


def test_rebind_crash_preserves_pinned_helper_tail_progress():
    from test_run_log import _old_migrate

    assertions = unittest.TestCase()
    for after_publish in (False, True):
        with imported_log() as (data, source, _state):
            publish = run_log._publish

            def crash_publish(directory, raw):
                if after_publish:
                    publish(directory, raw)
                raise OSError('synthetic rebind publish crash')

            with patch.object(run_log, '_publish', crash_publish):
                with assertions.assertRaises(OSError):
                    run_log.migrate(data, ['update.log'])
            canonical = json.loads((data / run_log.BACKUPS / run_log.STATE).read_bytes())
            assertions.assertEqual(canonical['sources']['update.log']['identity'],
                                   [source.stat().st_dev, source.stat().st_ino])
            with source.open('ab') as stream:
                stream.write(b'synthetic pinned helper tail\n')
            if not after_publish:
                with assertions.assertRaises(run_log.RunLogError):
                    _old_migrate(data, ['update.log'])
                # Repair only the committed mirror, without importing the tail.
                run_log.initialize(data)
            _old_migrate(data, ['update.log'])
            advanced = json.loads((data / run_log.STATE).read_bytes())
            assertions.assertEqual(advanced['sources']['update.log']['offset'], source.stat().st_size)
            assertions.assertEqual(run_log.migrate(data, ['update.log'])['imported'], [])
            assertions.assertEqual((data / run_log.LOG).read_bytes(), source.read_bytes())
            assertions.assertEqual(json.loads((data / run_log.BACKUPS / run_log.STATE).read_bytes()),
                                   advanced)


if __name__ == '__main__':
    unittest.main()
