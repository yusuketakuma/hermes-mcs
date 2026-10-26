"""Runtime owned-FD handoffs close on failure before any external operation."""
import errno
import os

import pytest

import safe_io


SITES = ["health", "validator", "receiver", "alias", "registry",
         "standalone_init", "lineworks_init", "evaluation_output"]


def _operation(site, tmp_path, monkeypatch):
    if site == "health":
        import health_watch
        monkeypatch.setattr(health_watch, "load_config", lambda *a: {})
        monkeypatch.setattr(health_watch, "_watch", lambda *a: pytest.fail("handoff reached watcher"))
        return lambda: health_watch.main(["--home", str(tmp_path)]), 1
    if site == "validator":
        import snapshots
        monkeypatch.setattr(snapshots.subprocess, "Popen", lambda *a, **k: pytest.fail("handoff spawned validator"))
        return lambda: snapshots._valid_snapshot_candidate(
            str(tmp_path / "candidate.db"), deadline=float("inf")), False
    if site == "receiver":
        import c1_receiver
        sink = c1_receiver.ReferenceReceiver(tmp_path / "receiver", source_label="synthetic")
        path = sink.root / "state.json"
        path.write_text("{}")
        path.chmod(0o600)
        return sink._load, None
    if site == "alias":
        import drug_alias
        path = tmp_path / "alias.json"
        path.write_text("{}")
        path.chmod(0o600)
        return lambda: drug_alias.private_json(path), None
    if site == "registry":
        from adapters.common import registry
        reg = registry.Registry(str(tmp_path))
        # Enter the actual append path without batch-exit save masking the fault.
        reg._batch_depth = 1
        return lambda: reg.put_tokens({"synthetic": {"action": "ack"}}, durable=True), None
    if site == "standalone_init":
        from mcs_standalone import __main__ as cli
        monkeypatch.setattr(cli.config, "connector_settings", lambda *a, **k: None)
        monkeypatch.setattr(cli.config, "credentials_path", lambda *a: tmp_path / "credentials.json")
        def missing(*args):
            raise ValueError("synthetic missing credentials")
        monkeypatch.setattr(cli.config, "load_credentials", missing)
        monkeypatch.setattr(cli.config, "validate_credentials", lambda *a: None)
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.getpass, "getpass", lambda *a: "SYNTHETIC_TOKEN")
        return lambda: cli.initialize(tmp_path, "discord"), None
    if site == "lineworks_init":
        from adapters.lineworks import __main__ as cli
        key = tmp_path / "synthetic.key"
        key.write_bytes(b"synthetic, never signed")
        key.chmod(0o600)
        def settings(root, *, require_interactive=True):
            assert root == tmp_path and require_interactive is False
            return {"application_id": "synthetic"}
        monkeypatch.setattr(cli, "settings", settings)
        monkeypatch.setattr(cli, "credentials_path", lambda *a: tmp_path / "credentials.json")
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.getpass, "getpass", lambda *a: "SYNTHETIC_TOKEN")
        monkeypatch.setattr("builtins.input", lambda *a: str(key))
        monkeypatch.setattr(cli, "_sign", lambda *a: b"synthetic")
        monkeypatch.setattr(cli, "Credentials", lambda **k: None)
        return lambda: cli.initialize(tmp_path), None
    import semantic_snapshot
    import semantic_snapshot_blind
    source, config, output = [tmp_path / name for name in ("source.json", "config.json", "output.json")]
    for path in (source, config):
        path.write_text("{}")
        path.chmod(0o600)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(semantic_snapshot_blind, "prepare", lambda *a: {"synthetic": True})
    return lambda: semantic_snapshot.main([
        "blind-prepare", "--input", str(source), "--config", str(config),
        "--output", str(output)]), 2


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("fault", [OSError, KeyboardInterrupt, SystemExit])
def test_runtime_handoff_releases_exact_owned_fd(tmp_path, monkeypatch, capsys, site, fault):
    operation, io_result = _operation(site, tmp_path, monkeypatch)
    descriptors = []
    failure = fault("synthetic stream adoption fault")
    original_fdopen, original_open = safe_io.os.fdopen, os.open
    evaluation_opens, evaluation_reads = [], []

    def capture_evaluation_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        # Capture ownership at acquisition, independently of the adoption hook.
        if os.fspath(path) in {str(tmp_path / name) for name in (
                "source.json", "config.json", "output.json")}:
            evaluation_opens.append((os.fspath(path), flags, fd))
        return fd

    if site == "evaluation_output":
        monkeypatch.setattr(os, "open", capture_evaluation_open)
    before = {p.name: p.read_bytes() for p in tmp_path.rglob("*.json")}

    def refuse(fd, *args, **kwargs):
        if site == "evaluation_output":
            mode = args[0] if args else kwargs.get("mode")
            if mode == "rb":
                evaluation_reads.append(fd)
                return original_fdopen(fd, *args, **kwargs)
            assert mode == "w"
        descriptors.append(fd)
        raise failure

    monkeypatch.setattr(safe_io.os, "fdopen", refuse)
    try:
        if fault is OSError and site in ("health", "validator", "evaluation_output"):
            assert operation() == io_result
        else:
            with pytest.raises(fault) as caught:
                operation()
            assert caught.value is failure
        if site == "evaluation_output":
            assert [path for path, _, _ in evaluation_opens] == [
                str(tmp_path / name) for name in ("source.json", "config.json", "output.json")]
            assert evaluation_reads == [fd for _, _, fd in evaluation_opens[:2]]
            assert all(flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                       for _, flags, _ in evaluation_opens[:2])
            assert evaluation_opens[2][1] == os.O_WRONLY | os.O_CREAT | os.O_EXCL
            assert descriptors == [evaluation_opens[2][2]]
        assert len(descriptors) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(descriptors[0])
        assert closed.value.errno == errno.EBADF
        for name, raw in before.items():
            assert next(tmp_path.rglob(name)).read_bytes() == raw
        assert not (tmp_path / "credentials.json").exists()
        assert not list(tmp_path.glob(".*-init-*"))
        if site == "evaluation_output" and fault is OSError:
            assert capsys.readouterr().err == "sealed snapshot evaluation failed\n"
    finally:
        # Even a red characterization cannot leak the descriptor opened by this test.
        owned_output = {fd for path, _, fd in evaluation_opens
                        if path == str(tmp_path / "output.json")}
        for fd in set(descriptors) | owned_output:
            try:
                os.close(fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
