"""署名対象bundleの全packを、Gitへ渡す前に有界に検査する。"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unicodedata
import zlib

CHUNK = 65536
MAX_PACK_OBJECTS = 200000
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_METADATA_TOTAL = 64 * 1024 * 1024
MAX_DELTA_DEPTH = 64
MAX_HEADER_BYTES = 1024 * 1024
MAX_PATH_BYTES = 16 * 1024 * 1024
PROTECTED = ('data', 'config.json', '.env', 'chrome-profile', 'venv', 'models')
KINDS = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}


class ArtifactError(ValueError):
    def __init__(self, code='INVALID_PACK'):
        self.code = 'RELEASE_ARTIFACT_' + code
        super().__init__(self.code)


def _check(value, code='INVALID_PACK'):
    if not value:
        raise ArtifactError(code)


def _time(deadline):
    _check(type(deadline) in (int, float) and math.isfinite(deadline)
           and time.monotonic() < deadline, 'DEADLINE')


def _exact(stream, size):
    data = stream.read(size)
    _check(len(data) == size, 'TRUNCATED_PACK')
    return data


def _new_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, 'wb')


def _number(stream):
    value = 0
    for shift in range(0, 64, 7):
        byte = _exact(stream, 1)[0]
        value |= (byte & 127) << shift
        if not byte & 128:
            _check(value <= 2**53 - 1)
            return value
    raise ArtifactError()


def _inflate(stream, destination, declared, digest, deadline):
    """Hash each consumed compressed byte once; never use zlib's unlimited max_length=0."""
    decoder, emitted, pending = zlib.decompressobj(), 0, b''
    try:
        while not decoder.eof:
            _time(deadline)
            if not pending:
                pending = stream.read(CHUNK)
                _check(pending, 'TRUNCATED_PACK')
            before = pending
            out = decoder.decompress(before, min(CHUNK, declared - emitted + 1))
            emitted += len(out)
            _check(emitted <= declared, 'OBJECT_SIZE_MISMATCH')
            destination.write(out)
            pending = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
            consumed = len(before) - len(pending)
            _check(consumed or out or decoder.eof, 'INVALID_ZLIB')
            digest.update(before[:consumed])
        if pending:
            stream.seek(-len(pending), os.SEEK_CUR)
        _check(emitted == declared, 'OBJECT_SIZE_MISMATCH')
    except zlib.error:
        raise ArtifactError('INVALID_ZLIB') from None


def _object_digest(row, deadline):
    identity = hashlib.sha1(f"{row['kind']} {row['size']}\0".encode('ascii'))
    content = hashlib.sha256()
    with row['path'].open('rb') as stream:
        while chunk := stream.read(CHUNK):
            _time(deadline)
            identity.update(chunk)
            content.update(chunk)
    row.update(oid=identity.hexdigest(), content_sha256=content.hexdigest())


def _apply_delta(row, base, destination, maximum, deadline):
    """Resolve one bounded delta using files, counting each reconstructed byte as work."""
    with row['path'].open('rb') as instructions, base['path'].open('rb') as source:
        _check(_number(instructions) == base['size'], 'DELTA_BASE_SIZE')
        target = _number(instructions)
        _check(target <= maximum, 'PACK_INFLATED_LIMIT')
        if base['kind'] != 'blob':
            _check(target <= MAX_METADATA_BYTES, 'METADATA_LIMIT')
        emitted = 0
        with _new_file(destination) as output:
            while opcode := instructions.read(1):
                _time(deadline)
                command = opcode[0]
                _check(command != 0, 'INVALID_DELTA')
                if command & 128:
                    offset = size = 0
                    for index in range(4):
                        if command & (1 << index):
                            offset |= _exact(instructions, 1)[0] << (8 * index)
                    for index in range(3):
                        if command & (1 << (index + 4)):
                            size |= _exact(instructions, 1)[0] << (8 * index)
                    size = size or 65536
                    _check(offset + size <= base['size'] and emitted + size <= target, 'INVALID_DELTA')
                    source.seek(offset)
                    remaining = size
                    while remaining:
                        _time(deadline)
                        part = _exact(source, min(CHUNK, remaining))
                        output.write(part)
                        remaining -= len(part)
                else:
                    size = command
                    _check(emitted + size <= target, 'INVALID_DELTA')
                    output.write(_exact(instructions, size))
                emitted += size
            _check(emitted == target, 'OBJECT_SIZE_MISMATCH')
        return target


def _scan_pack(stream, directory, *, max_inflated_bytes, deadline):
    """Private regular-file input only; no Git, candidate code, network or installation.

    The working budget includes every zlib byte and every delta result, including
    unreachable objects. It bounds input work/storage, not process peak RSS.
    The caller owns an empty private staging directory and removes it on failure.
    """
    _time(deadline)
    _check(type(max_inflated_bytes) is int and 0 < max_inflated_bytes <= 1073741824)
    directory = Path(directory)
    start, digest = stream.tell(), hashlib.sha1()

    def read(size):
        data = _exact(stream, size)
        digest.update(data)
        return data

    header = read(12)
    _check(header[:4] == b'PACK' and int.from_bytes(header[4:8], 'big') in (2, 3))
    count = int.from_bytes(header[8:12], 'big')
    _check(count <= MAX_PACK_OBJECTS, 'PACK_OBJECT_LIMIT')
    rows, offsets, by_oid = [], {}, {}
    offset_children, ref_children = defaultdict(list), defaultdict(list)
    budget, metadata = max_inflated_bytes, 0
    for index in range(count):
        _time(deadline)
        offset = stream.tell() - start
        first = read(1)[0]
        kind, size, byte = (first >> 4) & 7, first & 15, first
        for shift in range(4, 64, 7):
            if not byte & 128:
                break
            byte = read(1)[0]
            size |= (byte & 127) << shift
        _check(not byte & 128 and kind in (*KINDS, 6, 7))
        _check(size <= budget, 'PACK_INFLATED_LIMIT')
        if kind in (1, 2, 4):
            _check(size <= MAX_METADATA_BYTES, 'METADATA_LIMIT')
        row = {'offset': offset, 'path': directory / f'object-{index:06d}', 'size': size,
               'kind': KINDS.get(kind), 'depth': 0, 'oid': None}
        if kind == 6:
            byte = read(1)[0]
            distance = byte & 127
            for _ in range(9):
                if not byte & 128:
                    break
                byte = read(1)[0]
                distance = ((distance + 1) << 7) | (byte & 127)
            _check(not byte & 128 and distance > 0 and offset - distance in offsets, 'INVALID_DELTA_BASE')
            row['base_offset'] = offset - distance
            offset_children[row['base_offset']].append(row)
        elif kind == 7:
            row['base_oid'] = read(20).hex()
            ref_children[row['base_oid']].append(row)
        with _new_file(row['path']) as output:
            _inflate(stream, output, size, digest, deadline)
        budget -= size
        if kind in (1, 2, 4):
            metadata += size
            _check(metadata <= MAX_METADATA_TOTAL, 'METADATA_LIMIT')
        rows.append(row)
        offsets[offset] = row
    checksum = _exact(stream, 20)
    _check(checksum == digest.digest() and stream.read(1) == b'', 'PACK_CHECKSUM_OR_TRAILING_BYTES')
    pack_size = stream.tell() - start

    ready = deque(row for row in rows if row['kind'] is not None)
    scheduled = {row['offset'] for row in ready}
    resolved = 0
    while ready:
        _time(deadline)
        row = ready.popleft()
        if row['kind'] is None:
            base = offsets.get(row.get('base_offset')) if 'base_offset' in row else by_oid.get(row['base_oid'])
            _check(base is not None and base['oid'] is not None, 'DELTA_BASE_MISSING')
            row['depth'] = base['depth'] + 1
            _check(row['depth'] <= MAX_DELTA_DEPTH, 'DELTA_DEPTH_LIMIT')
            destination = row['path'].with_suffix('.resolved')
            size = _apply_delta(row, base, destination, budget, deadline)
            budget -= size
            row.update(kind=base['kind'], path=destination, size=size)
            if row['kind'] != 'blob':
                metadata += size
                _check(metadata <= MAX_METADATA_TOTAL, 'METADATA_LIMIT')
        _object_digest(row, deadline)
        previous = by_oid.get(row['oid'])
        _check(previous is None or (previous['kind'], previous['size'], previous['content_sha256']) == (
            row['kind'], row['size'], row['content_sha256']), 'OBJECT_COLLISION')
        by_oid[row['oid']] = row
        resolved += 1
        for child in (*offset_children[row['offset']], *ref_children[row['oid']]):
            if child['offset'] not in scheduled:
                scheduled.add(child['offset'])
                ready.append(child)
    _check(resolved == count, 'DELTA_BASE_MISSING')
    return {'objects': by_oid, 'pack_sha1': checksum.hex(), 'pack_size': pack_size,
            'pack_start': start, 'object_count': count, 'inflated_bytes': max_inflated_bytes - budget}


def _bundle_header(stream):
    _check(stream.readline(64) == b'# v2 git bundle\n', 'UNSUPPORTED_BUNDLE')
    count, total = 0, 16
    while True:
        line = stream.readline(4097)
        total += len(line)
        _check(line.endswith(b'\n') and len(line) <= 4096 and total <= MAX_HEADER_BYTES, 'BUNDLE_HEADER_LIMIT')
        if line == b'\n':
            break
        if line.startswith(b'-'):
            # This empty isolated ODB has no externally verified prerequisite closure.
            raise ArtifactError('PREREQUISITE_UNAVAILABLE')
        _check(re.fullmatch(rb'[0-9a-f]{40} [!-~]+\n', line) is not None, 'INVALID_BUNDLE_HEADER')
        name = line[41:-1].decode('ascii')
        _check(name == 'HEAD' or name.startswith('refs/'), 'INVALID_BUNDLE_HEADER')
        _check(not any(token in name for token in ('..', '@{', '//', '\\', '~', '^', ':', '?', '*', '['))
               and not name.endswith(('/', '.'))
               and all(part and not part.startswith('.') and not part.endswith('.lock') for part in name.split('/')),
               'INVALID_BUNDLE_HEADER')
        count += 1
        _check(count <= 4096, 'BUNDLE_HEADER_LIMIT')
    _check(count > 0, 'INVALID_BUNDLE_HEADER')
    return stream.tell()


def _copy_authenticated(source, destination, artifact, deadline):
    _check(artifact['format'] == 'git-bundle-v2' and artifact['object_format'] == 'sha1'
           and type(artifact['size']) is int and 0 < artifact['size'] <= 268435456
           and re.fullmatch('[0-9a-f]{64}', artifact['sha256']) is not None, 'INVALID_CORE')
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        _check(stat.S_ISREG(before.st_mode) and before.st_size == artifact['size'], 'ARTIFACT_SIZE')
        total, digest = 0, hashlib.sha256()
        with _new_file(destination) as output:
            while chunk := stream.read(CHUNK):
                _time(deadline)
                total += len(chunk)
                _check(total <= artifact['size'], 'ARTIFACT_SIZE')
                digest.update(chunk)
                output.write(chunk)
        after = os.fstat(stream.fileno())
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        _check(identity(before) == identity(after) and total == artifact['size']
               and digest.hexdigest() == artifact['sha256'], 'ARTIFACT_HASH_OR_CHANGED')


def _target_tree(objects, commit, *, max_entries, max_bytes, deadline):
    def get(identity, kind):
        row = objects.get(identity)
        _check(row is not None and row['kind'] == kind, 'OBJECT_GRAPH_INCOMPLETE')
        return row

    row = get(commit, 'commit')
    with row['path'].open('rb') as stream:
        first = stream.readline(128)
    _check(re.fullmatch(rb'tree [0-9a-f]{40}\n', first) is not None, 'INVALID_COMMIT')
    stack = [(first[5:-1].decode('ascii'), '', 0)]
    seen, files, entries, expanded, path_bytes = set(), {}, 0, 0, 0
    while stack:
        _time(deadline)
        identity, parent, depth = stack.pop()
        _check(depth <= 64, 'TREE_DEPTH_LIMIT')
        with get(identity, 'tree')['path'].open('rb') as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
        _check(len(raw) <= MAX_METADATA_BYTES, 'METADATA_LIMIT')
        position = 0
        while position < len(raw):
            _time(deadline)
            separator = raw.find(b' ', position, position + 8)
            end = raw.find(b'\0', separator + 1)
            _check(separator > position and end > separator and end + 21 <= len(raw), 'INVALID_TREE')
            mode, name = raw[position:separator], raw[separator + 1:end].decode('utf-8', errors='strict')
            identity = raw[end + 1:end + 21].hex()
            position = end + 21
            _check(mode in (b'40000', b'100644', b'100755'), 'UNSAFE_ENTRY_TYPE')
            _check(name and name not in ('.', '..') and len(name.encode('utf-8')) <= 255
                   and not any(ord(char) < 32 or ord(char) == 127 for char in name)
                   and not any(char in name for char in ('/', '\\', ':'))
                   and name == unicodedata.normalize('NFC', name), 'UNSAFE_PATH')
            path = parent + name
            normalized = path.casefold()
            _check(len(path.encode('utf-8')) <= 4096 and normalized not in seen
                   and name.casefold() not in ('.git', '.gitmodules', '.gitattributes'), 'UNSAFE_PATH')
            _check(not any(normalized == item or normalized.startswith(item + '/') for item in PROTECTED),
                   'PROTECTED_PATH')
            seen.add(normalized)
            entries += 1
            path_bytes += len(path.encode('utf-8'))
            _check(entries <= max_entries and path_bytes <= MAX_PATH_BYTES, 'TREE_ENTRY_LIMIT')
            if mode == b'40000':
                get(identity, 'tree')
                stack.append((identity, path + '/', depth + 1))
            else:
                blob = get(identity, 'blob')
                expanded += blob['size']
                _check(expanded <= max_bytes, 'TREE_EXPANDED_LIMIT')
                files[path] = blob
    return {'files': files, 'entries': entries, 'expanded_bytes': expanded}



@contextmanager
def _inspect_verified(source, *, core, policy, profile_id, profile_sha256, deadline):
    """Already authenticated Core only. This yields inspected data, never execution authority.

    The owner must persist H first, recheck current bindings after inspection,
    and consume a live grant + publish STARTED before importing into its repo.
    No prerequisite is fetched or borrowed from an unverified object database.
    The trusted process owner must perform Git strict graph validation before
    treating these parsed bytes as an inspected artifact or requesting a grant.
    """
    prepared = False
    try:
        _time(deadline)
        _check(type(policy['max_archive_entries']) is int and 0 < policy['max_archive_entries'] <= 50000
               and core['artifact']['size'] <= policy['max_artifact_bytes'], 'INVALID_POLICY')
        _check(type(profile_id) is str and re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', profile_id) is not None
               and type(profile_sha256) is str and re.fullmatch('[0-9a-f]{64}', profile_sha256) is not None,
               'PROFILE_UNKNOWN')
        with tempfile.TemporaryDirectory(prefix='mcs-release-inspect-') as temporary:
            directory = Path(temporary)
            copy = directory / 'artifact.bundle'
            _copy_authenticated(source, copy, core['artifact'], deadline)
            objects = directory / 'objects'
            objects.mkdir()
            with copy.open('rb') as stream:
                start = _bundle_header(stream)
                scanned = _scan_pack(stream, objects, max_inflated_bytes=policy['max_unpacked_bytes'], deadline=deadline)
            tree = _target_tree(scanned['objects'], core['commit'], max_entries=policy['max_archive_entries'],
                                max_bytes=policy['max_unpacked_bytes'], deadline=deadline)
            lock = tree['files'].get('deployment/sdc-release-lock.json')
            _check(lock is not None and lock['size'] <= 1024 * 1024, 'LOCK_MISSING_OR_TOO_LARGE')
            lock_raw = lock['path'].read_bytes()
            _check(hashlib.sha256(lock_raw).hexdigest() == core['lock_sha256'], 'LOCK_HASH')
            pack_path = directory / 'verified.pack'
            with copy.open('rb') as stream, _new_file(pack_path) as output:
                stream.seek(start)
                while chunk := stream.read(CHUNK):
                    _time(deadline)
                    output.write(chunk)
            _time(deadline)
            prepared = True
            yield {'core': core, 'bundle': copy, 'pack': pack_path, 'pack_sha1': scanned['pack_sha1'],
                   'lock_raw': lock_raw,
                   'profile_sources': {name: tree['files'].get(name) for name in (
                       'deployment/requirements-standalone.txt', 'install.sh')},
                   'entries': tree['entries'], 'expanded_bytes': tree['expanded_bytes'],
                   'pack_objects': scanned['object_count'], 'pack_inflated_bytes': scanned['inflated_bytes'],
                   'pack_size': scanned['pack_size']}
    except ArtifactError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        if prepared:
            raise
        raise ArtifactError('INSPECTION_FAILED') from None
