"""Synthetic runtime gates accept verified deployed helpers during pointer promotion."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import run_log_install as deploy
import runtime_inspection
import service_management


FACTS = {'python': '3.14.8', 'sqlite': '3.51.3',
         'packages': {'discord.py': None, 'slack-bolt': None, 'slack-sdk': None}}


def put(path, raw, mode=0o600):
    path.write_bytes(raw)
    path.chmod(mode)


@contextmanager
def promoted_helper():
    with tempfile.TemporaryDirectory(prefix='mcs-recovery-generation-') as temporary:
        base = Path(temporary).resolve()
        base.chmod(0o700)
        root, checkout, recovery, agents = (base / name for name in
                                            ('runtime', 'checkout', 'recovery', 'agents'))
        (root / 'data').mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        source = checkout / 'mcs/core/run_log.py'
        source.parent.mkdir(parents=True)
        agents.mkdir(mode=0o700)
        # Any execution of either deployed generation fails this test.
        put(source, b'raise AssertionError("synthetic old helper must not execute")\n', 0o644)
        with patch.object(deploy, '_migrate', return_value={}):
            deploy.prepare(root, checkout, recovery)
            old = Path(deploy.helper_path(root, recovery, require_installed=True))
            put(source, b'raise AssertionError("synthetic new helper must not execute")\n', 0o644)
            deploy.prepare(root, checkout, recovery, promote=True)
        executable = str(base / 'independent-python')
        put(recovery / 'mcs_recover.py', b'# synthetic watchdog\n', 0o700)
        put(recovery / 'repo_path', os.fsencode(str(checkout)))
        plist = agents / 'org.mcs.recovery.plist'
        command = [executable, '-I', str(old), 'exec', '--data-dir', str(root / 'data'),
                   '--', executable, str(recovery / 'mcs_recover.py'), '--if-stale']
        put(plist, plistlib.dumps({'Label': 'org.mcs.recovery', 'ProgramArguments': command}))
        paths = service_management.default_paths(root=str(root), checkout=str(checkout),
                    recovery_dir=str(recovery), agents_dir=str(agents), hermes_python=executable,
                    hermes_runtime_home=str(base / 'hermes'), platform='darwin')
        yield root, checkout, recovery, old, plist, paths


class RecoveryHelperGenerationTests(unittest.TestCase):
    def test_old_deployed_helper_passes_after_current_pointer_promotion(self):
        with promoted_helper() as (root, checkout, recovery, old, plist, paths):
            current = deploy.helper_path(root, recovery, require_installed=True)
            self.assertNotEqual(str(old), current)
            before = {path: path.read_bytes() for path in recovery.iterdir()}
            self.assertEqual(deploy.verify_installed_helper(root, recovery, old), str(old))
            self.assertEqual(deploy.verify_installed_helper(root, recovery, current), current)
            with patch.object(runtime_inspection, 'probe_python', return_value=FACTS):
                self.assertEqual(runtime_inspection.recovery_runtime(plist, checkout=checkout), FACTS)
                self.assertTrue(service_management.recovery_owned(paths=paths))
                self.assertEqual(service_management.runtime_gate_errors(
                    {'recovery_python': paths.hermes_python}, paths=paths), [])
            self.assertEqual({path: path.read_bytes() for path in recovery.iterdir()}, before)

    def test_untrusted_helpers_or_current_receipts_remain_rejected(self):
        for defect in ('unknown', 'other_root', 'outside', 'symlink', 'hardlink', 'changed',
                       'writable', 'directory_mode', 'filename_hash', 'receipt_invalid',
                       'current_missing', 'current_changed'):
            with self.subTest(defect=defect), promoted_helper() as fixture:
                root, checkout, recovery, old, plist, paths = fixture
                helper = old
                if defect in ('unknown', 'other_root', 'outside', 'filename_hash'):
                    digest = hashlib.sha256(old.read_bytes()).hexdigest()
                    name = old.name
                    if defect == 'unknown':
                        name = 'unknown.py'
                    elif defect == 'other_root':
                        name = deploy._pointer(str(root.parent / 'other-root'))[:-5] + '-' + digest + '.py'
                    elif defect == 'filename_hash':
                        name = deploy._pointer(str(root))[:-5] + '-' + '0' * 64 + '.py'
                    helper = (root.parent if defect == 'outside' else recovery) / name
                    put(helper, old.read_bytes())
                elif defect in ('symlink', 'hardlink'):
                    saved = old.with_suffix('.saved')
                    old.rename(saved)
                    old.symlink_to(saved) if defect == 'symlink' else os.link(saved, old)
                elif defect == 'changed':
                    put(old, old.read_bytes() + b'# modified\n')
                elif defect == 'writable':
                    old.chmod(0o660)
                elif defect == 'directory_mode':
                    recovery.chmod(0o755)
                elif defect == 'receipt_invalid':
                    put(recovery / deploy._pointer(str(root)), b'{"version":1}')
                else:
                    current = Path(deploy.helper_path(root, recovery, require_installed=True))
                    if defect == 'current_missing':
                        current.unlink()
                    else:
                        put(current, current.read_bytes() + b'# modified\n')
                document = plistlib.loads(plist.read_bytes())
                document['ProgramArguments'][2] = str(helper)
                put(plist, plistlib.dumps(document))
                with self.assertRaises(deploy.LogInstallError):
                    deploy.verify_installed_helper(root, recovery, helper)
                with patch.object(runtime_inspection, 'probe_python', return_value=FACTS):
                    self.assertIsNone(runtime_inspection.recovery_runtime(plist, checkout=checkout))
                    self.assertFalse(service_management.recovery_owned(paths=paths))

    def test_helper_change_during_read_is_rejected(self):
        for defect in ('path_swap', 'content_change', 'pointer_change'):
            with self.subTest(defect=defect), promoted_helper() as fixture:
                root, _checkout, recovery, old, _plist, _paths = fixture
                read = deploy._read
                changed = []

                def racing_read(fd, limit, **kwargs):
                    raw = read(fd, limit, **kwargs)
                    if not changed and os.fstat(fd).st_ino == old.stat().st_ino:
                        changed.append(True)
                        if defect == 'path_swap':
                            replacement = old.with_suffix('.replacement')
                            put(replacement, old.read_bytes())
                            os.replace(replacement, old)
                        elif defect == 'content_change':
                            put(old, old.read_bytes() + b'# modified\n')
                        else:
                            digest = hashlib.sha256(old.read_bytes()).hexdigest()
                            put(recovery / deploy._pointer(str(root)), json.dumps(
                                {'version': 1, 'root': str(root), 'sha256': digest,
                                 'helper': old.name}).encode())
                    return raw

                with patch.object(deploy, '_read', racing_read):
                    with self.assertRaises(deploy.LogInstallError):
                        deploy.verify_installed_helper(root, recovery, old)
                self.assertTrue(changed)


if __name__ == '__main__':
    unittest.main()
