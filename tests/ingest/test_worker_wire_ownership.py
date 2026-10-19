"""Worker wire calls do not import or construct the parent session owner."""
import builtins

import pytest

import mcs_cdp
import mcs_download
import mcs_worker


@pytest.mark.parametrize("operation", ["download", "cdp_eval"])
def test_wire_dispatch_does_not_import_parent_adapter(monkeypatch, operation):
    observed = []

    def download(url, partial, *, token, timeout):
        observed.append((url, partial, token, timeout))
        return {"bytes": 3, "sha256": "synthetic-digest"}

    def evaluate(url, expression, timeout):
        observed.append((url, expression, timeout))
        return "synthetic-value"

    monkeypatch.setattr(mcs_download, "download_to_part", download)
    monkeypatch.setattr(mcs_cdp, "evaluate", evaluate)
    real_import = builtins.__import__

    def no_parent(name, *args, **kwargs):
        if name == "mcs_adapter":
            raise AssertionError("worker must not import the parent session owner")
        return real_import(name, *args, **kwargs)

    envelope = {"operation": operation, "timeout": 1}
    if operation == "download":
        envelope.update(url="https://www.medical-care.net/f", partial="synthetic.part",
                        token="synthetic-token")
        expected = {"bytes": 3, "sha256": "synthetic-digest"}
        arguments = (envelope["url"], "synthetic.part", "synthetic-token", 1)
    else:
        envelope.update(url="ws://127.0.0.1:9333/devtools/synthetic", expression="1+1")
        expected = {"value": "synthetic-value"}
        arguments = (envelope["url"], "1+1", 1)
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "__import__", no_parent)
        assert mcs_worker._execute(envelope) == expected
    assert observed == [arguments]
