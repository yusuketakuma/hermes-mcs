"""Only bounded reason codes from the verified helper survive deployment errors."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import pytest

import mcs_update
import run_log_install as deploy
from test_update_log_migration import layout


@pytest.mark.parametrize('operation', ['prepare', 'tail'])
@pytest.mark.parametrize('phase,error_type,message,expected', [
    ('initialize', 'RunLogError', 'run_log_state_invalid', 'run_log_state_invalid'),
    ('migrate', 'RunLogError', 'run_log_untrusted', 'run_log_untrusted'),
    ('migrate', 'RuntimeError', 'run_log_untrusted', None),
    ('migrate', 'ChildError', 'run_log_untrusted', None),
    ('migrate', 'RunLogError', 'run_log_untrusted: SYNTHETIC_PRIVATE_PATH',
     'run_log_migration_failed'),
    ('migrate', 'RunLogError', 'run_log_' + 'a' * 81, 'run_log_migration_failed'),
])
def test_verified_helper_failure_codes_only(
        tmp_path, monkeypatch, operation, phase, error_type, message, expected):
    root, checkout, recovery, source = layout(tmp_path)
    source.write_text(
        'class RunLogError(RuntimeError): pass\n'
        'class ChildError(RunLogError): pass\n'
        'def initialize(data):\n'
        + (f'    raise {error_type}({message!r})\n' if phase == 'initialize' else '    pass\n')
        + 'def migrate(data):\n'
        + (f'    raise {error_type}({message!r})\n' if phase == 'migrate' else '    return {}\n'))
    if operation == 'tail':
        # Failed import publishes the verified helper; the next tail import uses it.
        with pytest.raises(deploy.LogInstallError):
            deploy.prepare(root, checkout, recovery)
    monkeypatch.setattr(mcs_update, 'HOME', str(root))
    monkeypatch.setattr(update_apply, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_cli, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_execution, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(mcs_update, 'REPO', str(checkout))
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(mcs_update, '_RUN_LOG_SOURCE', source.read_bytes())
    monkeypatch.setattr(update_lifecycle_context, '_RUN_LOG_SOURCE', mcs_update._RUN_LOG_SOURCE)
    monkeypatch.setattr(mcs_update, '_RUN_LOG_SOURCE_ERROR', False)
    monkeypatch.setattr(update_lifecycle_context, '_RUN_LOG_SOURCE_ERROR', mcs_update._RUN_LOG_SOURCE_ERROR)
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_update, '_service_paths',
                        lambda: SimpleNamespace(recovery_dir=str(recovery)))
    monkeypatch.setattr(update_execution, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_lifecycle_context, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_planning, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_preflight, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_recovery, '_service_paths', mcs_update._service_paths)
    monkeypatch.setattr(update_restore, '_service_paths', mcs_update._service_paths)
    invoke = mcs_update._prepare_run_log if operation == 'prepare' else mcs_update._migrate_log_tail
    prefix = 'run_log_prepare_failed' if operation == 'prepare' else 'run_log_tail_unverified'
    with pytest.raises(mcs_update.UpdateError) as error:
        invoke()
    assert str(error.value) == prefix + (': ' + expected if expected else '')


@pytest.mark.parametrize('stream', ['stderr', 'stdout'])
def test_post_merge_keeps_traceback_terminal_cause(monkeypatch, stream):
    terminal = 'RunLogError: run_log_state_invalid'
    diagnostic = 'Traceback: synthetic stack frame\n' * 100 + terminal

    class Child:
        returncode = 1

        def communicate(self, timeout=None):
            return ('', diagnostic) if stream == 'stderr' else (diagnostic, '')

    monkeypatch.setattr(mcs_update.subprocess, 'Popen', lambda *_a, **_k: Child())
    monkeypatch.setattr(mcs_update, '_migrate_log_tail', lambda: None)
    monkeypatch.setattr(update_execution, '_migrate_log_tail', mcs_update._migrate_log_tail)
    monkeypatch.setattr(update_recovery, '_migrate_log_tail', mcs_update._migrate_log_tail)
    monkeypatch.setattr(mcs_update.journal_store, 'load_state',
                        lambda *_a: {'stages': [{'stage': 'post_merge'}], 'applying': {}})
    with pytest.raises(mcs_update.UpdateError) as error:
        mcs_update._run_post_merge('a' * 40, run_lock_fd=None)
    assert str(error.value) == 'post_merge_failed: ' + diagnostic[-300:]
    assert str(error.value).endswith(terminal)
