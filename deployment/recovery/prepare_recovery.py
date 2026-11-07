#!/usr/bin/env python3
"""Stage the entrypoint's exact companion generation before replacing a recovery tool."""
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def _regular(path, maximum=262144):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("recovery_support_invalid")
        raw = b""
        while len(raw) <= maximum:
            chunk = os.read(fd, maximum + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > maximum:
            raise ValueError("recovery_support_invalid")
        return raw
    finally:
        os.close(fd)


def _manifest(raw, entrypoint):
    """Parse literal companion digests from the exact entrypoint bytes to be published."""
    tree = ast.parse(raw, filename=str(entrypoint))
    declarations = [node for node in tree.body if isinstance(node, ast.Assign)
                    and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "SUPPORT_FILES"]
    if len(declarations) != 1:
        raise ValueError("recovery_support_manifest_missing")
    pins = ast.literal_eval(declarations[0].value)
    if (not isinstance(pins, dict) or set(pins) != {
            "mcs_recovery_gateway.py", "mcs_recovery_core.py",
            "mcs_recovery_services.py", "mcs_recovery_database.py"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in pins.values())):
        raise ValueError("recovery_support_manifest_invalid")
    return pins


def _sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_bundle(entrypoint, destination, entry_raw):
    """Publish a complete immutable generation; keep every previous entrypoint's companions."""
    entrypoint, destination = Path(entrypoint), Path(destination)
    pins = _manifest(entry_raw, entrypoint)
    verified = {}
    for filename, digest in pins.items():
        raw = _regular(entrypoint.parent / filename)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("recovery_support_generation_mismatch")
        verified[filename] = raw
    metadata = destination.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022):
        raise ValueError("recovery_support_directory_unsafe")
    generation = hashlib.sha256(json.dumps(pins, sort_keys=True,
        separators=(",", ":")).encode("ascii")).hexdigest()
    final = destination / ("runtime-" + generation)
    if os.path.lexists(final):
        if not stat.S_ISDIR(final.lstat().st_mode):
            raise ValueError("recovery_support_directory_unsafe")
        for filename, raw in verified.items():
            if _regular(final / filename) != raw:
                raise ValueError("recovery_support_generation_mismatch")
        _sync_directory(final)
        _sync_directory(destination)
        return generation
    staging = Path(tempfile.mkdtemp(prefix=".runtime-stage-", dir=str(destination)))
    try:
        for filename, raw in verified.items():
            fd = os.open(str(staging / filename), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        _sync_directory(staging)
        os.rename(str(staging), str(final))
        _sync_directory(destination)
    finally:
        if staging.exists():
            shutil.rmtree(str(staging))
    return generation


def prepare(entrypoint, destination):
    """Stage companions for one captured entrypoint without replacing any public tool."""
    return _prepare_bundle(entrypoint, destination, _regular(entrypoint))


def _temporary_file(directory, raw, mode):
    fd, name = tempfile.mkstemp(prefix=".entry-stage-", dir=str(directory))
    try:
        try:
            os.fchmod(fd, mode)
            remaining = memoryview(raw)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("recovery_entry_write_incomplete")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


def install(entrypoint, destination):
    """Publish only the same captured bytes whose exact companion bundle was prepared."""
    entrypoint, destination = Path(entrypoint), Path(destination)
    entry_raw = _regular(entrypoint)
    _prepare_bundle(entrypoint, destination, entry_raw)
    target = destination / "mcs_recover.py"
    prior = _regular(target) if os.path.lexists(target) else None
    if prior == entry_raw:
        return False
    pending = []
    try:
        entry_tmp = _temporary_file(destination, entry_raw, 0o700)
        pending.append(entry_tmp)
        previous_tmp = None
        if prior is not None:
            metadata = target.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("recovery_entry_invalid")
            previous_tmp = _temporary_file(destination, prior, stat.S_IMODE(metadata.st_mode))
            pending.append(previous_tmp)
            os.utime(str(previous_tmp), ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        _sync_directory(destination)
        if previous_tmp is not None:
            os.replace(str(previous_tmp), str(target) + ".prev")
            _sync_directory(destination)
        os.replace(str(entry_tmp), str(target))
        _sync_directory(destination)
        return True
    finally:
        for path in pending:
            if os.path.lexists(path):
                path.unlink()


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: prepare_recovery.py SOURCE_ENTRYPOINT DESTINATION_DIRECTORY")
    changed = install(sys.argv[1], sys.argv[2])
    print("1" if changed else "0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
