"""Synthetic regressions: HTTP 425 is a try-again-later status in every ingest path."""
import mcs_contract
import io
import json
import urllib.error

import pytest

import mcs_adapter
import mcs_worker
from mcs_util import RETRY_LATER_4XX


@pytest.mark.parametrize("status", sorted(RETRY_LATER_4XX) + [404])
def test_download_classifies_4xx_like_the_job_ledger(tmp_path, monkeypatch, status):
    def refuse(url):
        raise urllib.error.HTTPError(url, status, "synthetic", {}, None)
    adapter = mcs_adapter.MCSAdapter()
    monkeypatch.setattr(adapter, "_open_download", refuse)
    with pytest.raises(mcs_contract.MCSError) as error:
        adapter._download_to_part("https://www.medical-care.net/f",
                                  str(tmp_path / "f.part"))
    assert error.value.status == status
    assert error.value.retryable is (status in RETRY_LATER_4XX)


@pytest.mark.parametrize("status", sorted(RETRY_LATER_4XX) + [404])
def test_worker_classifies_raw_4xx_like_the_job_ledger(monkeypatch, capsys, status):
    def refuse(envelope):
        raise urllib.error.HTTPError("https://www.medical-care.net/x", status,
                                     "synthetic", {}, io.BytesIO(b""))
    monkeypatch.setattr(mcs_worker, "_execute", refuse)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert mcs_worker.worker_main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == status
    assert out["retryable"] is (status in RETRY_LATER_4XX)
