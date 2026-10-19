"""Only owned export descriptors are reclaimed when stream transfer fails."""
import errno
import os
from types import SimpleNamespace

import pytest

import c1_receiver
import export_delivery
import ext_contract
import safe_io


@pytest.mark.parametrize("operation", ["audit", "receipts", "receive"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_export_owned_fd_transfer_failure_is_closed(tmp_path, monkeypatch, operation, failure):
    destination = tmp_path / "receipts.ndjson"
    if operation == "audit":
        exporter = export_delivery.GovernedExporter(tmp_path / "sender")
        def invoke():
            exporter._audit("deliver", "refused", auth_id="synthetic")
    elif operation == "receipts":
        def invoke():
            ext_contract._write_new_receipts(str(destination), "synthetic\n")
    else:
        source = tmp_path / "synthetic-input.json"
        source.write_text("{}")
        monkeypatch.setattr(c1_receiver, "ReferenceReceiver", lambda *args, **kwargs:
                            SimpleNamespace(expire=lambda: {"expired": 0}))
        args = SimpleNamespace(input=str(source), receipts_out=str(destination),
                               receiver_root=str(tmp_path / "receiver"), source_label="synthetic")
        def invoke():
            ext_contract._receive(args)
    owned = []
    error = failure("synthetic pre-transfer failure")
    def fail(fd, *args, **kwargs):
        owned.append(fd)
        raise error
    try:
        with monkeypatch.context() as patch:
            patch.setattr(safe_io.os, "fdopen", fail)
            with pytest.raises(failure) as caught:
                invoke()
        assert caught.value is error
        assert len(owned) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(owned[0])
        assert closed.value.errno == errno.EBADF
        assert not destination.exists()
    finally:
        for fd in owned:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
