"""release_auto_evidence のcontrol root evidence/耐久保存を検証。

実MCS・実データには触れない。control rootはテストHOME配下の合成runtime_home。
"""
import hashlib
import os
import shutil
import stat

import pytest

import release_auto_evidence as ev


@pytest.fixture
def runtime_home(tmp_path):
    return str(tmp_path / 'runtime')


def _evidence_dir(runtime_home):
    from runtime_entry import control_root
    return control_root(runtime_home) / 'evidence'


def test_store_load_roundtrip(runtime_home):
    raw = b'{"prestart-abort": "synthetic"}'
    key = ev.store_evidence(runtime_home, raw)
    assert key == hashlib.sha256(raw).hexdigest()
    assert ev.load_evidence(runtime_home, key) == raw


def test_store_is_idempotent_for_same_bytes(runtime_home):
    raw = b'same-bytes'
    assert ev.store_evidence(runtime_home, raw) == ev.store_evidence(runtime_home, raw)


def test_store_writes_private_regular_file(runtime_home):
    key = ev.store_evidence(runtime_home, b'data')
    path = _evidence_dir(runtime_home) / key
    info = os.lstat(path)
    assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_nlink == 1
    dir_info = os.lstat(_evidence_dir(runtime_home))
    assert stat.S_IMODE(dir_info.st_mode) == 0o700


def test_load_missing_key(runtime_home):
    ev.store_evidence(runtime_home, b'x')
    missing = '0' * 64
    with pytest.raises(ev.EvidenceError) as error:
        ev.load_evidence(runtime_home, missing)
    assert error.value.code == 'EVIDENCE_MISSING'


def test_load_rejects_bad_key(runtime_home):
    for bad in ('', 'z' * 64, 'a' * 63, '../etc/passwd', None):
        with pytest.raises(ev.EvidenceError) as error:
            ev.load_evidence(runtime_home, bad)
        assert error.value.code == 'EVIDENCE_KEY'


def test_store_rejects_empty_and_oversize(runtime_home):
    with pytest.raises(ev.EvidenceError) as error:
        ev.store_evidence(runtime_home, b'')
    assert error.value.code == 'EVIDENCE_SIZE'
    with pytest.raises(ev.EvidenceError):
        ev.store_evidence(runtime_home, b'x' * (ev.MAX_EVIDENCE_BYTES + 1))


def test_load_detects_tamper(runtime_home):
    raw = b'original-evidence'
    key = ev.store_evidence(runtime_home, raw)
    path = _evidence_dir(runtime_home) / key
    os.chmod(path, 0o600)
    with open(path, 'r+b') as stream:
        stream.write(b'tampered-evidence!')
    with pytest.raises(ev.EvidenceError) as error:
        ev.load_evidence(runtime_home, key)
    assert error.value.code == 'EVIDENCE_TAMPER'


def test_store_conflict_on_differing_existing(runtime_home):
    raw = b'legit-bytes'
    key = ev.store_evidence(runtime_home, raw)
    path = _evidence_dir(runtime_home) / key
    # 同key名で異bytesを置く(sha256衝突/改竄を模倣)。loadがTAMPERで拒否する。
    os.chmod(path, 0o600)
    with open(path, 'wb') as stream:
        stream.write(b'forged-bytes')
    with pytest.raises(ev.EvidenceError):
        ev.store_evidence(runtime_home, raw)


def test_symlinked_evidence_dir_rejected(runtime_home):
    ev.store_evidence(runtime_home, b'seed')
    target = _evidence_dir(runtime_home)
    shutil.rmtree(target)
    os.symlink(str(target.parent / 'elsewhere'), str(target))
    with pytest.raises(ev.EvidenceError) as error:
        ev.load_evidence(runtime_home, 'a' * 64)
    assert error.value.code == 'EVIDENCE_DIR_HOLD'


def test_evidence_dir_wrong_mode_rejected(runtime_home):
    ev.store_evidence(runtime_home, b'x')
    os.chmod(_evidence_dir(runtime_home), 0o755)
    with pytest.raises(ev.EvidenceError) as error:
        ev.load_evidence(runtime_home, 'a' * 64)
    assert error.value.code == 'EVIDENCE_DIR_HOLD'


def test_store_write_failure_removes_partial(runtime_home, monkeypatch):
    """書込み途中失敗の残骸を除去し、同bytesの再storeを可能にする。"""
    raw = b'evidence-that-fails-once'
    key = hashlib.sha256(raw).hexdigest()
    real_write = os.write
    state = {'failed': False}

    def flaky_write(fd, data):
        if not state['failed']:
            state['failed'] = True
            raise OSError('synthetic write failure')
        return real_write(fd, data)

    monkeypatch.setattr(os, 'write', flaky_write)
    with pytest.raises(OSError):
        ev.store_evidence(runtime_home, raw)
    assert not (_evidence_dir(runtime_home) / key).exists()
    monkeypatch.setattr(os, 'write', real_write)
    assert ev.store_evidence(runtime_home, raw) == key
    assert ev.load_evidence(runtime_home, key) == raw
