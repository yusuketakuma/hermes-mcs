"""v3 recovery controller (M7) を合成tmpdir+fake depsで検証する。

実process/network/本体importは使わない。closure世代はtmpdirへ人工的に構築。
"""
import hashlib
import json
import os
from contextlib import contextmanager

import pytest

import test_release_auto_apply as at
import test_release_auto_owner as ot
import test_release_runtime_proof as rt_t
from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as state
import release_auto_recovery as rec
import release_runtime_proof as rt_mod

H = 'a' * 64


def _manifest(files):
    value = {'schema_version': 'mcs.release-control-manifest.v3',
             'owner_id': 'mcs.updater', 'owner_revision': 2,
             'entrypoints': ['mcs_update.py'], 'files': files,
             'dependency_policy_sha256': H,
             'recovery_generation_sha256': H}
    canonical = contract.canonical_typed(value, 'ControlManifest')
    return value, canonical


def _deploy(tmp_path, files_spec):
    """files_spec: {relpath: bytes} をmanifest+generation dirへ展開する。"""
    entries = [{'path': p, 'mode': '100644',
                'sha256': hashlib.sha256(raw).hexdigest()}
               for p, raw in sorted(files_spec.items())]
    manifest, canonical = _manifest(entries)
    gen = os.path.join(str(tmp_path),
                       'runtime-' + hashlib.sha256(canonical).hexdigest())
    os.makedirs(gen)
    for p, raw in files_spec.items():
        full = os.path.join(gen, *p.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(raw)
    with open(os.path.join(gen, 'sdc-update-control.json'), 'wb') as f:
        f.write(canonical)
    return gen, manifest, canonical


def _full_files():
    return {p: b'x' for p in rec._REQUIRED_CLOSURE}


# --- CLI verify ---------------------------------------------------------------

def test_cli_verify_accepts_intact_generation(tmp_path, capsys):
    gen, _, _ = _deploy(tmp_path, _full_files())
    assert rec.main(['verify', '--generation-dir', gen]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is True
    assert out['generation'] == os.path.basename(gen)


def test_cli_verify_rejects_tampered_generation(tmp_path, capsys):
    gen, _, _ = _deploy(tmp_path, _full_files())
    target = os.path.join(gen, 'mcs/ops/release_auto_recovery.py')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'wb') as f:
        f.write(b'tampered')
    assert rec.main(['verify', '--generation-dir', gen]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is False
    assert out['code'] == 'GENERATION_TAMPER'


def test_cli_verify_rejects_missing_descriptor(tmp_path, capsys):
    assert rec.main(['verify', '--generation-dir',
                     str(tmp_path / 'nonexistent')]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is False
    assert out['code'] == 'GENERATION_DESCRIPTOR'


def test_cli_module_and_script_invocation(tmp_path):
    """python -m/script両経路でverify CLIが完走する(entrypoint契約)。"""
    import subprocess
    import sys
    from pathlib import Path
    gen, _, _ = _deploy(tmp_path, _full_files())
    repo = Path(__file__).resolve().parents[2]
    for argv in ([sys.executable, '-m', 'mcs.ops.release_auto_recovery',
                  'verify', '--generation-dir', gen],
                 [sys.executable, 'mcs/ops/release_auto_recovery.py',
                  'verify', '--generation-dir', gen]):
        proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)['ok'] is True


# --- verify_generation ---------------------------------------------------------

def test_verify_generation_happy(tmp_path):
    gen, manifest, canonical = _deploy(tmp_path, _full_files())
    assert rec.verify_generation(gen, manifest, canonical) is True


def test_verify_generation_rejects_tampered_file(tmp_path):
    gen, manifest, canonical = _deploy(tmp_path, _full_files())
    target = os.path.join(gen, 'mcs', 'ops', 'release_auto_state.py')
    with open(target, 'wb') as f:
        f.write(b'tampered')
    with pytest.raises(rec.RecoveryError) as e:
        rec.verify_generation(gen, manifest, canonical)
    assert e.value.code == 'GENERATION_TAMPER'


def test_verify_generation_rejects_extra_file(tmp_path):
    gen, manifest, canonical = _deploy(tmp_path, _full_files())
    with open(os.path.join(gen, 'evil.py'), 'wb') as f:
        f.write(b'x')
    with pytest.raises(rec.RecoveryError) as e:
        rec.verify_generation(gen, manifest, canonical)
    assert e.value.code == 'GENERATION_TAMPER'


def test_verify_generation_rejects_missing_required(tmp_path):
    files = _full_files()
    del files['mcs/ops/release_auto_recovery.py']
    gen, manifest, canonical = _deploy(tmp_path, files)
    with pytest.raises(rec.RecoveryError) as e:
        rec.verify_generation(gen, manifest, canonical)
    assert e.value.code == 'GENERATION_INCOMPLETE'


def test_verify_generation_rejects_wrong_dirname(tmp_path):
    gen, manifest, canonical = _deploy(tmp_path, _full_files())
    other = os.path.join(str(tmp_path), 'runtime-' + '0' * 64)
    os.rename(gen, other)
    with pytest.raises(rec.RecoveryError) as e:
        rec.verify_generation(other, manifest, canonical)
    assert e.value.code == 'GENERATION_NAME'


def test_verify_generation_rejects_descriptor_mismatch(tmp_path):
    gen, manifest, canonical = _deploy(tmp_path, _full_files())
    with open(os.path.join(gen, 'sdc-update-control.json'), 'wb') as f:
        f.write(b'{"tampered":true}')
    with pytest.raises(rec.RecoveryError) as e:
        rec.verify_generation(gen, manifest, canonical)
    assert e.value.code == 'GENERATION_DESCRIPTOR'


def test_verify_generation_rejects_symlink(tmp_path):
    gen, manifest, canonical = _deploy(tmp_path, _full_files())
    link = os.path.join(gen, 'mcs', 'ops', 'link.py')
    os.symlink('/etc/passwd', link)
    with pytest.raises(rec.RecoveryError):
        rec.verify_generation(gen, manifest, canonical)


def test_generation_for_locates_only_matching(tmp_path):
    gen, manifest, canonical = _deploy(tmp_path, _full_files())
    assert rec.generation_for(canonical, str(tmp_path)) == gen
    assert rec.generation_for(b'other', str(tmp_path)) is None


# --- observe_status ------------------------------------------------------------

def test_observe_status_idle_and_active(tmp_path):
    with ot.Store(tmp_path / 's', create=True) as store:
        ot.seed(store)
        info = rec.observe_status(store)
        assert info['action'] == 'idle' and info['phase'] is None


# --- recover_tick ---------------------------------------------------------------

class _AdvanceSpy:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result if result is not None else {'phase': 'X'}
        self.error = error

    def __call__(self, store):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@contextmanager
def _store_with_phase(tmp_path, phase):
    """指定phaseのactiveを持つowner stateを作る(STARTEDまでは実経路)。"""
    with ot.Store(tmp_path / 's', create=True) as store:
        ot.seed(store)
        deps = rt_t.RuntimeDeps()
        if phase == 'APPLIED':
            at.seed(store)
            op_id, deps = at.started(store, deps)
            import release_auto_apply as apply_mod
            apply_mod.apply_code_only_v3(store, at._capability(store), deps)
        elif phase == 'PREPARED':
            ot._prepared_owner(store, deps)
        yield store, deps


def test_recover_tick_idle_without_active(tmp_path):
    with ot.Store(tmp_path / 's', create=True) as store:
        ot.seed(store)
        assert rec.recover_tick(store, object())['action'] == 'idle'


def test_recover_tick_holds_before_runtime_phases(tmp_path):
    with _store_with_phase(tmp_path, 'PREPARED') as (store, deps):
        spy = _AdvanceSpy()
        deps.runtime_advance = spy
        info = rec.recover_tick(store, deps)
        assert info['action'] == 'hold'
        assert info['reason'] == 'BEFORE_RUNTIME_PROOF'
        assert spy.calls == 0  # 新grant/effectを出さない


def test_recover_tick_holds_without_advance_deps(tmp_path):
    with _store_with_phase(tmp_path, 'APPLIED') as (store, deps):
        del deps.runtime_advance  # RuntimeDeps既定hookを外して未注入を模倣
        info = rec.recover_tick(store, deps)
        assert info['action'] == 'hold'
        assert info['reason'] == 'NO_ADVANCE_DEPS'


def test_recover_tick_drives_applied_and_verifies(tmp_path):
    """APPLIED→VERIFIEDをrecovery tickで完走。start/result送信は増えない。"""
    with _store_with_phase(tmp_path, 'APPLIED') as (store, deps):
        deps.runtime_advance = lambda s: rt_mod.advance_runtime(s, deps)
        sent_before = list(deps.sent)
        for _ in range(3):
            rec.recover_tick(store, deps)
        owner = state.load(store)
        assert owner['active'] is None
        assert owner['outcomes'][-1]['state'] == 'VERIFIED'
        # recoveryは観測のみ: start/grant/resultの新規送信なし
        assert deps.sent == sent_before


def test_recover_tick_hold_on_advance_error(tmp_path):
    with _store_with_phase(tmp_path, 'APPLIED') as (store, deps):
        deps.desires = {}  # SERVICE_SET_UNKNOWNを起こす
        deps.runtime_advance = lambda s: rt_mod.advance_runtime(s, deps)
        info = rec.recover_tick(store, deps)
        assert info['action'] == 'hold'
        assert info['code'] == 'SERVICE_SET_UNKNOWN'
        assert state.load(store)['active']['phase'] == 'APPLIED'
