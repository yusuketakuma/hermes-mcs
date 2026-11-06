"""Synthetic checks for optional OCR installation and update registration."""
import update_apply
import update_cli
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import release_apply_checks
import json
import sys
from types import SimpleNamespace
import ocr_model_contract
import importlib.util
from pathlib import Path
import subprocess

import install_options
import install_options_contract
import mcs_update
import pytest
import update_policy

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('setup_yomitoku', REPO / 'scripts/setup_yomitoku.py')
setup_yomitoku = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_yomitoku)


def test_yomitoku_option_is_recorded_without_enabling_ocr(tmp_path):
    config, legacy = tmp_path / 'config.json', tmp_path / 'data/install_options.json'
    config.write_text('{}')
    config.chmod(0o600)
    assert install_options_contract.valid(['--with-yomitoku'])
    base = install_options.inspect(config, legacy, ['--with-yomitoku'])
    install_options.save(config, legacy, ['--with-yomitoku'], base)
    assert install_options.recorded(config, legacy) == ['--with-yomitoku']
    assert 'attachment_ocr' not in config.read_text()


def fake_runtime(monkeypatch, root):
    calls = []
    monkeypatch.setattr(setup_yomitoku.shutil, 'which', lambda name: '/synthetic/uv')
    def run(args, *, env):
        assert 'PYTHONPATH' not in env
        assert 'HF_HUB_OFFLINE' not in env
        assert env['HF_HOME'] == str(Path.home() / '.cache/huggingface')
        assert 'HF_HUB_CACHE' not in env
        calls.append(args)
        if args[1] == 'venv':
            binary = root / 'ocr-venv/bin'
            binary.mkdir(parents=True)
            (binary / 'python').touch()
        if args[1] == 'pip':
            (root / 'ocr-venv/bin/yomitoku').touch()
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(setup_yomitoku, 'run', run)
    return calls


def test_setup_separate_python_pinned_source_and_repeatable_cache(monkeypatch, tmp_path):
    root = tmp_path / 'synthetic'
    calls = fake_runtime(monkeypatch, root)
    monkeypatch.setenv('PYTHONPATH', '/synthetic/unused')
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('HF_HOME', '/synthetic/unused-cache')
    monkeypatch.setenv('HF_HUB_CACHE', '/synthetic/unused-cache/hub')
    setup_yomitoku.setup(root, REPO)
    assert ['--python', '3.13'] == calls[0][2:4]
    assert any('version("yomitoku")' in str(call) and "0.16.1" in str(call) for call in calls)
    model_call = next(call for call in calls if 'snapshot_download' in str(call))
    assert json.loads(model_call[4]) == json.loads(ocr_model_contract.MODEL_JSON)
    assert len(ocr_model_contract.MODELS) == 4
    assert all('dewarp' not in repo and 'middle' not in repo
               for repo, _ in ocr_model_contract.MODELS.values())
    requirements = (REPO / 'deployment/requirements-ocr.txt').read_text()
    assert '@0279cf256e2588430d085e2dc958e8740be82921' in requirements
    marker = root / 'ocr-venv/.mcs-ocr-requirements'
    marker.write_bytes(marker.read_bytes() + b'synthetic-prior-model\n')
    calls.clear()
    setup_yomitoku.setup(root, REPO)
    assert len(calls) == 3  # model list changes never reinstall dependencies
    assert b'synthetic-prior-model' not in marker.read_bytes()
    assert not (root / 'config.json').exists()


def test_setup_failed_model_download_does_not_mark_complete(monkeypatch, tmp_path):
    calls = fake_runtime(monkeypatch, tmp_path)
    original = setup_yomitoku.run
    def fail(args, *, env):
        if 'snapshot_download' in str(args):
            raise subprocess.CalledProcessError(1, args)
        return original(args, env=env)
    monkeypatch.setattr(setup_yomitoku, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        setup_yomitoku.setup(tmp_path, REPO)
    assert calls
    assert not (tmp_path / 'ocr-venv/.mcs-ocr-requirements').exists()


@pytest.mark.parametrize('incomplete', [False, True])
def test_setup_preserves_foreign_or_incomplete_environment(monkeypatch, tmp_path, incomplete):
    venv = tmp_path / 'ocr-venv'
    if incomplete:
        venv.mkdir()
        (venv / 'keep').write_text('synthetic')
    else:
        venv.symlink_to(tmp_path / 'foreign')
    monkeypatch.setattr(setup_yomitoku, 'run', lambda *_a, **_k: pytest.fail('must not run'))
    with pytest.raises(ValueError):
        setup_yomitoku.setup(tmp_path, REPO)
    assert venv.is_symlink() if not incomplete else (venv / 'keep').read_text() == 'synthetic'


@pytest.mark.parametrize('changed', ['deployment/requirements-ocr.txt', 'mcs/extract/ocr_model_contract.py'])
@pytest.mark.parametrize('opted_in', [False, True])
def test_updater_reinstall_only_for_opted_in_ocr(monkeypatch, opted_in, changed):
    monkeypatch.setattr(mcs_update, 'recorded_install_args',
                        lambda: ['--with-yomitoku'] if opted_in else [])
    monkeypatch.setattr(update_planning, 'recorded_install_args', mcs_update.recorded_install_args)
    monkeypatch.setattr(update_preflight, 'recorded_install_args', mcs_update.recorded_install_args)
    monkeypatch.setattr(update_runtime_migration, 'recorded_install_args', mcs_update.recorded_install_args)
    calls = []
    def diff(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, int(changed in args), '', '')
    monkeypatch.setattr(mcs_update, '_git', diff)
    monkeypatch.setattr(update_apply, '_git', mcs_update._git)
    monkeypatch.setattr(update_cli, '_git', mcs_update._git)
    monkeypatch.setattr(update_planning, '_git', mcs_update._git)
    monkeypatch.setattr(update_preflight, '_git', mcs_update._git)
    monkeypatch.setattr(update_recovery, '_git', mcs_update._git)
    monkeypatch.setattr(update_repository, '_git', mcs_update._git)
    monkeypatch.setattr(update_restore, '_git', mcs_update._git)
    monkeypatch.setattr(update_runtime_migration, '_git', mcs_update._git)
    errors = mcs_update._precheck_target_runtime_files('synthetic', False)
    assert errors == (['ocr_requirements_changed'] if opted_in else [])
    assert 'ocr_requirements_changed' in update_policy.REINSTALL_ERRORS
    assert 'ocr-venv/' in mcs_update.PROTECTED
    assert any('scripts/setup_yomitoku.py' in args for args in calls) == opted_in


def test_installer_registers_optional_setup_without_default_install():
    shell = (REPO / 'install.sh').read_text()
    assert 'WITH_YOMITOKU=0' in shell
    assert '--with-yomitoku) WITH_YOMITOKU=1 ;;' in shell
    assert '[ "$WITH_YOMITOKU" -eq 0 ] || set -- "$@" --with-yomitoku' in shell
    assert '. "$INSTALL_RUNTIME_FIXED"' in shell
    runtime = (REPO / 'deployment/install/runtime.sh').read_text()
    assert '"$REPO/scripts/setup_yomitoku.py" --root "$MCS_ROOT"' in runtime


def test_reinstall_keeps_recorded_yomitoku_selection(monkeypatch):
    from contextlib import contextmanager
    import release_apply_owner

    @contextmanager
    def inherited(fds):
        yield {fd: fd for fd in fds}

    captured = []
    class Child:
        returncode = 0

        def communicate(self, **kwargs):
            return '', None

    def popen(argv, **kwargs):
        captured.append(argv)
        return Child()

    monkeypatch.setattr(mcs_update, '_verify_install_options_base', lambda _a: 'synthetic-base')
    monkeypatch.setattr(update_apply, '_verify_install_options_base', mcs_update._verify_install_options_base)
    monkeypatch.setattr(update_runtime_migration, '_verify_install_options_base', mcs_update._verify_install_options_base)
    monkeypatch.setattr(mcs_update, 'recorded_install_args', lambda: ['--with-yomitoku'])
    monkeypatch.setattr(update_planning, 'recorded_install_args', mcs_update.recorded_install_args)
    monkeypatch.setattr(update_preflight, 'recorded_install_args', mcs_update.recorded_install_args)
    monkeypatch.setattr(update_runtime_migration, 'recorded_install_args', mcs_update.recorded_install_args)
    monkeypatch.setattr(release_apply_owner, '_verify_inherited_lock', lambda *_a: None)
    monkeypatch.setattr(release_apply_checks, '_verify_inherited_lock', release_apply_owner._verify_inherited_lock)
    monkeypatch.setattr(mcs_update.owned_process, '_inherited_fds', inherited)
    monkeypatch.setattr(mcs_update.subprocess, 'Popen', popen)
    monkeypatch.setenv(mcs_update.UPDATE_LOCK_FD_ENV, '7')
    applying = {'reinstall': True, 'install_args': ['--no-llm']}
    mcs_update._reinstall(applying)
    assert captured[0][-3:] == ['--no-llm', '--with-yomitoku', '--no-services']
    assert applying['reinstall_done'] is True


def test_manual_installer_reads_saved_ocr_selection(tmp_path):
    import sys

    config, legacy = tmp_path / 'config.json', tmp_path / 'data/install_options.json'
    config.write_text('{"install_args":["--with-yomitoku"]}')
    config.chmod(0o600)
    shell = (REPO / 'install.sh').read_text()
    code = shell.split("<<'PYOCR'\n", 1)[1].split('\nPYOCR', 1)[0]
    result = subprocess.run([sys.executable, '-I', '-', str(REPO / 'mcs'), str(config), str(legacy)],
                            input=code, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == '1'
    assert 'if [ "$_ocr_previous" = 1 ] && [ "$WITH_YOMITOKU" -eq 0 ]; then' in shell


def test_model_fetch_uses_exact_revision_and_rechecks_weight(monkeypatch, tmp_path):
    calls = fake_runtime(monkeypatch, tmp_path)
    setup_yomitoku.setup(tmp_path, REPO)
    command = next(call for call in calls if 'snapshot_download' in str(call))
    class Missing(Exception):
        pass
    fetched = set()
    observed = []
    def weight(*, repo_id, revision, filename, local_files_only):
        assert filename == 'model.safetensors' and local_files_only
        assert (repo_id, revision) in ocr_model_contract.MODELS.values()
        observed.append((repo_id, revision))
        if (repo_id, revision) not in fetched:
            raise Missing()
    def snapshot(*, repo_id, revision):
        fetched.add((repo_id, revision))
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        hf_hub_download=weight, snapshot_download=snapshot))
    monkeypatch.setitem(sys.modules, 'huggingface_hub.errors', SimpleNamespace(LocalEntryNotFoundError=Missing))
    monkeypatch.setattr(sys, 'argv', ['-c', command[4]])
    exec(command[3], {})
    assert fetched == set(ocr_model_contract.MODELS.values())
    assert len(observed) == 8


def test_missing_executable_never_writes_ready_marker(monkeypatch, tmp_path):
    fake_runtime(monkeypatch, tmp_path)
    original = setup_yomitoku.run
    def remove_binary(args, *, env):
        result = original(args, env=env)
        (tmp_path / 'ocr-venv/bin/yomitoku').unlink(missing_ok=True)
        return result
    monkeypatch.setattr(setup_yomitoku, 'run', remove_binary)
    with pytest.raises(ValueError, match='executable'):
        setup_yomitoku.setup(tmp_path, REPO)
    assert not (tmp_path / 'ocr-venv/.mcs-ocr-requirements').exists()
