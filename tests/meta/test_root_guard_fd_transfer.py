"""Load only guard function ASTs; no conftest import, hooks, timer or live stderr."""
import ast
from contextlib import contextmanager
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@contextmanager
def guard_world():
    read_fd = write_fd = None
    owned = []
    globals_ = None
    try:
        read_fd, write_fd = os.pipe()
        source = Path(__file__).resolve().parents[2] / "conftest.py"
        parsed = ast.parse(source.read_text())
        nodes = [node for node in parsed.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"pytest_configure", "pytest_unconfigure"}]
        assert {node.name for node in nodes} == {"pytest_configure", "pytest_unconfigure"}
        calls = {"dup": [], "fdopen": [], "close": [], "cancel": []}

        def duplicate(fd):
            calls["dup"].append(fd)
            assert fd == 2  # Borrow this fictional stderr, never the process stderr.
            value = os.dup(write_fd)
            owned.append(value)
            return value

        def stream(fd, mode):
            calls["fdopen"].append((fd, mode))
            return os.fdopen(fd, mode)

        def close(fd):
            calls["close"].append(fd)
            return os.close(fd)

        fake = SimpleNamespace(dup=duplicate, fdopen=stream, close=close)
        globals_ = {"os": fake, "errno": errno, "_hang_stderr": None,
                    "_test_timeout": lambda: 1,
                    "_cancel_dump": lambda: calls["cancel"].append(True)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), globals_)
        yield SimpleNamespace(namespace=globals_, fake=fake, calls=calls,
                              read_fd=read_fd, write_fd=write_fd, owned=owned)
    finally:
        try:
            if globals_ is not None and globals_.get("_hang_stderr") is not None:
                globals_["_hang_stderr"].close()
        finally:
            try:
                for fd in owned:
                    try:
                        os.close(fd)
                    except OSError as error:
                        if error.errno != errno.EBADF:
                            raise
            finally:
                try:
                    if write_fd is not None:
                        os.close(write_fd)
                finally:
                    if read_fd is not None:
                        os.close(read_fd)


def assert_closed(fd):
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


def test_dup_failure_is_original_and_never_closes_borrowed_stderr():
    with guard_world() as world:
        error = OSError("synthetic dup refusal")

        def refuse(fd):
            assert fd == 2
            raise error

        world.fake.dup = refuse
        with pytest.raises(OSError) as caught:
            world.namespace["pytest_configure"](None)
        assert caught.value is error
        assert world.owned == [] and world.calls["fdopen"] == [] and world.calls["close"] == []
        assert world.namespace["_hang_stderr"] is None
        assert os.fstat(world.write_fd)


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("library_closed_first", [False, True])
def test_stream_transfer_failure_closes_only_duplicate_and_keeps_original(failure, library_closed_first):
    with guard_world() as world:
        error = failure("synthetic pre-transfer refusal")

        def refuse(fd, mode):
            assert mode == "w" and fd == world.owned[0]
            if library_closed_first:
                os.close(fd)
            raise error

        world.fake.fdopen = refuse
        with pytest.raises(failure) as caught:
            world.namespace["pytest_configure"](None)
        assert caught.value is error
        assert len(world.owned) == 1
        assert all(fd == world.owned[0] for fd in world.calls["close"])
        assert_closed(world.owned[0])
        assert world.namespace["_hang_stderr"] is None
        assert os.fstat(world.write_fd)


def test_real_stdlib_constructor_refusal_closes_duplicate():
    with guard_world() as world:
        def invalid_encoding(fd, mode):
            return os.fdopen(fd, mode, encoding="synthetic-invalid-encoding")

        world.fake.fdopen = invalid_encoding
        with pytest.raises(LookupError):
            world.namespace["pytest_configure"](None)
        assert len(world.owned) == 1
        assert_closed(world.owned[0])
        assert world.namespace["_hang_stderr"] is None
        assert os.fstat(world.write_fd)


def test_success_retains_stream_until_unconfigure_and_never_closes_original():
    with guard_world() as world:
        world.namespace["pytest_configure"](None)
        stream = world.namespace["_hang_stderr"]
        assert stream.fileno() == world.owned[0] and not stream.closed
        world.namespace["pytest_configure"](None)
        assert world.calls["dup"] == [2] and len(world.owned) == 1
        stream.write("synthetic")
        world.namespace["pytest_unconfigure"](None)
        assert stream.closed and world.namespace["_hang_stderr"] is None
        assert_closed(world.owned[0])
        assert os.fstat(world.write_fd)
        assert os.read(world.read_fd, len("synthetic")) == b"synthetic"
        assert world.calls["cancel"] == [True]


def test_disabled_timeout_does_not_duplicate_or_close_anything():
    with guard_world() as world:
        world.namespace["_test_timeout"] = lambda: 0
        world.namespace["pytest_configure"](None)
        assert world.owned == [] and world.calls["dup"] == []
        assert world.namespace["_hang_stderr"] is None
        assert os.fstat(world.write_fd)


def test_non_ebadf_cleanup_failure_remains_visible_with_original_context():
    with guard_world() as world:
        original = OSError("synthetic constructor refusal")
        cleanup = OSError(errno.EIO, "synthetic close refusal")

        def refuse(_fd, _mode):
            raise original

        def close_then_refuse(fd):
            os.close(fd)
            raise cleanup

        world.fake.fdopen = refuse
        world.fake.close = close_then_refuse
        with pytest.raises(OSError) as caught:
            world.namespace["pytest_configure"](None)
        assert caught.value is cleanup and caught.value.__context__ is original
        assert_closed(world.owned[0])
        assert os.fstat(world.write_fd)
