"""Read and atomically publish the updater journal without loading its lifecycle CLI."""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import stat

from safe_io import fdopen_owned


def valid_release_ref(value):
    """Causal journal data only; a saved reference never creates execution authority."""
    fields = {'schema_version', 'root_fingerprint', 'repo_fingerprint', 'operation_id', 'grant_id',
              'plan_sha256', 'release_id', 'target_code_seq', 'target_commit', 'artifact_sha256',
              'lock_sha256', 'target_schema', 'start_receipt_id', 'started_at'}
    if type(value) is not dict or set(value) != fields or value['schema_version'] != 'mcs.release-reference.v1':
        return False
    for key in ('root_fingerprint', 'repo_fingerprint', 'plan_sha256', 'release_id', 'artifact_sha256', 'lock_sha256'):
        if type(value[key]) is not str or re.fullmatch('[0-9a-f]{64}', value[key]) is None:
            return False
    if type(value['target_commit']) is not str or re.fullmatch('[0-9a-f]{40}', value['target_commit']) is None:
        return False
    for key in ('operation_id', 'grant_id', 'start_receipt_id'):
        if type(value[key]) is not str or re.fullmatch(
                '[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', value[key]) is None:
            return False
    return all(type(value[key]) is int and lower <= value[key] <= 2**53 - 1
               for key, lower in (('target_code_seq', 1), ('target_schema', 1), ('started_at', 0)))

def default_state() -> dict:
    return {"v": 1, "stages": [], "applied": [], "attempts": {},
            "executed": {}, "applying": None}


def release_ref_digest(reference):
    if not valid_release_ref(reference):
        raise ValueError('invalid_release_reference')
    return hashlib.sha256(json.dumps(reference, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def valid_release_child(value, reference):
    if (type(value) is not dict or set(value) != {
            'schema_version', 'purpose', 'release_ref_sha256', 'ticket_sha256', 'phase'}
            or value['schema_version'] != 'mcs.release-child.v1'
            or value['purpose'] != 'post-merge.v1'
            or value['phase'] not in ('issued', 'consumed')
            or not valid_release_ref(reference)):
        return False
    return (value['release_ref_sha256'] == release_ref_digest(reference)
            and type(value['ticket_sha256']) is str
            and re.fullmatch('[0-9a-f]{64}', value['ticket_sha256']) is not None)


def valid_state(state):
    """Check journal shapes before any recovery decision or mutation."""
    if not isinstance(state, dict) or type(state.get("v")) is not int \
            or state["v"] != 1:
        return False
    if 'release_child' in state:
        return False  # A child receipt belongs to its reference-bearing stage only.
    for key in ("stages", "applied"):
        if not isinstance(state.get(key, []), list) \
                or any(not isinstance(row, dict) for row in state.get(key, [])):
            return False
    if state.get("applying") is not None and not isinstance(state["applying"], dict):
        return False
    for key in ("attempts", "executed", "restore_consent"):
        if key in state and not isinstance(state[key], dict):
            return False
    records = [*state.get("stages", []), *state.get("applied", []),
               state.get("applying") or {}]
    references = [row['release_ref'] for row in [state, *records] if 'release_ref' in row]
    if not all(valid_release_ref(reference) for reference in references):
        return False
    operations = {}
    for reference in references:
        prior = operations.setdefault(reference['operation_id'], reference)
        if prior != reference:
            return False
    if 'release_ref' in state and 'release_ref' in (state.get('applying') or {}):
        if state['release_ref'] != state['applying']['release_ref']:
            return False
    for row in records:
        if 'release_child' in row and not valid_release_child(
                row['release_child'], row.get('release_ref')):
            return False
        stamp = row.get("at", 0)
        if type(stamp) not in (int, float) or not 0 <= stamp < 1e12:
            return False
    return all(isinstance(row.get("stage"), str)
               for row in state.get("stages", []))


def read_control_json(path):
    """Read a regular local control file without following links or waiting on FIFOs."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with fdopen_owned(fd, "r", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("control_file_not_regular")
        return json.load(handle)


def load_state(path) -> dict:
    """Lock-free read — writers publish atomically so a torn read never
    happens. Unparsable state is 'corrupt': callers must escalate and
    touch nothing (S17/S24)."""
    try:
        state = read_control_json(path)
    except FileNotFoundError:
        return {"_corrupt": True} if os.path.lexists(path) else default_state()
    except (OSError, ValueError, RecursionError):
        return {"_corrupt": True}
    if not valid_state(state):
        return {"_corrupt": True}
    return {**default_state(), **state}


def save_state(path, state: dict) -> None:
    """tmp -> fsync -> os.replace -> dir fsync (R13). Caller holds
    update.lock (or is the --locks-held post-merge child)."""
    path = os.fspath(path)
    parent, name = os.path.dirname(path) or '.', os.path.basename(path)
    os.makedirs(parent, exist_ok=True)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    tmp, identity = None, None
    try:
        held = os.fstat(directory)
        def verify_parent():
            current = os.stat(parent)
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise OSError('journal_directory_changed')
        verify_parent()
        for _ in range(32):
            candidate = '.update_state.' + secrets.token_hex(16) + '.tmp'
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                try:
                    identity = os.fstat(fd)
                except BaseException:
                    os.close(fd)
                    raise
                tmp = candidate
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError('journal_temporary_name_exhausted')
        with fdopen_owned(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        verify_parent()
        current = os.stat(tmp, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise OSError('journal_temporary_changed')
        os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
        tmp = None
        os.fsync(directory)
        verify_parent()
    finally:
        if tmp is not None:
            try:
                current = os.stat(tmp, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(tmp, dir_fd=directory)
            except OSError:
                pass
        os.close(directory)
