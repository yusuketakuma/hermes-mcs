"""Cloud observations validate each provider's usage without exposing private fields."""
from contextlib import closing
import json

import pytest

import cloud_cli
import cloud_policy as policy
from ledger import Ledger
from test_cloud_doctor import local

__all__ = ["local"]

USAGE = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
         "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
         "output_tokens_details": {"reasoning_tokens": 0}}


def test_openai_log_and_observation_accept_the_provider_usage_schema(local, monkeypatch):
    root, ledger, now = local
    monkeypatch.setattr(policy, "provider_key", lambda *a, **kw: None)
    meta = {"model": policy.openai_client.MODEL, "route": "reextract", "usage": USAGE,
            "private": "synthetic-hidden-metadata", "http_class": "http_200"}
    with ledger.db:
        aid = ledger.artifact_add_tx("cloud_usage_result", "synthetic-hidden-content",
            model=policy.openai_client.MODEL, project_id=1, meta=meta)
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (now, aid))
    before = ledger.db.total_changes
    safe = cloud_cli._safe_log({"kind": "cloud_usage_result", "created_at": now}, meta)
    assert safe["usage"] == policy.openai_client.validate_usage(USAGE)
    report = cloud_cli.observe({"cloud_llm": {"provider": "openai", "mode": "off"}}, root=str(root))
    assert report["log"][0]["usage"] == safe["usage"]
    assert ledger.db.total_changes == before and not ledger.db.in_transaction
    assert "synthetic-hidden" not in json.dumps(report)
    # Observation must not create accounting evidence for this unpaired result.
    with closing(Ledger(str(root / "data/ledger.db"))) as reread:
        assert len(reread.artifacts("cloud_usage_result")) == 1
        assert reread.artifacts("cloud_usage_reserve") == []


@pytest.mark.parametrize("usage", [{**USAGE, "total_tokens": 99},
                                  {**USAGE, "input_tokens": True},
                                  {**USAGE, "extra": "synthetic-hidden"}])
def test_malformed_openai_usage_remains_rejected(usage):
    with pytest.raises(policy.AnthropicError):
        cloud_cli._safe_log({"kind": "cloud_usage_result", "created_at": 1},
                           {"model": policy.openai_client.MODEL, "usage": usage})
