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
    safe = cloud_cli._safe_log({"kind": "cloud_usage_result", "created_at": now, "model": policy.openai_client.MODEL}, meta)
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
        cloud_cli._safe_log({"kind": "cloud_usage_result", "created_at": 1, "model": policy.openai_client.MODEL},
                           {"model": policy.openai_client.MODEL, "usage": usage})


@pytest.mark.parametrize("code", ["insufficient_quota", "credit_balance_exhausted",
                                   "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
                                   "organization_usage_limit_exceeded"])
def test_confirmed_openai_quota_log_exposes_only_fixed_code(code):
    meta = {"model": policy.openai_client.MODEL, "http_class": "http_429",
            "error_kind": "payment_required", "usage": None,
            "openai_error_code": code, "body": "synthetic-secret-canary",
            "request_id": "synthetic-secret-canary"}
    safe = cloud_cli._safe_log({"kind": "cloud_usage_result", "created_at": 1, "model": policy.openai_client.MODEL}, meta)
    assert safe["openai_error_code"] == code
    assert "synthetic-secret-canary" not in json.dumps(safe)
    assert "usage" not in safe


@pytest.mark.parametrize("changes,remove", [
    ({"model": policy.MODEL}, ()),
    ({"model": "synthetic-secret-canary"}, ()),
    ({"http_class": "http_400"}, ()),
    ({"http_class": "http_500"}, ()),
    ({"error_kind": "rate_limited"}, ()),
    ({"error_kind": "synthetic-secret-canary"}, ()),
    ({"usage": USAGE}, ()),
    ({}, ("usage",)),
    ({"openai_error_code": "synthetic-secret-canary"}, ()),
    ({"openai_error_code": None}, ()),
    ({"openai_error_code": ["insufficient_quota"]}, ()),
])
def test_unconfirmed_openai_quota_log_omits_code(changes, remove):
    meta = {"model": policy.openai_client.MODEL, "http_class": "http_429",
            "error_kind": "payment_required", "usage": None,
            "openai_error_code": "insufficient_quota", "body": "synthetic-secret-canary"}
    meta.update(changes)
    for key in remove:
        meta.pop(key)
    safe = cloud_cli._safe_log({"kind": "cloud_usage_result", "created_at": 1, "model": policy.openai_client.MODEL}, meta)
    assert "openai_error_code" not in safe
    assert "synthetic-secret-canary" not in json.dumps(safe)


def test_other_artifact_kinds_never_expose_quota_code():
    meta = {"model": policy.openai_client.MODEL, "http_class": "http_429",
            "error_kind": "payment_required", "usage": None, "openai_error_code": "insufficient_quota"}
    for kind in ("cloud_usage_reserve", "cloud_usage_dryrun", "cloud_usage_disposition", "cloud_compare_v1"):
        safe = cloud_cli._safe_log({"kind": kind, "created_at": 1, "model": policy.openai_client.MODEL}, meta)
        assert "openai_error_code" not in safe


def test_observe_includes_confirmed_code_without_secret(local, monkeypatch):
    root, ledger, now = local
    monkeypatch.setattr(policy, "provider_key", lambda *a, **kw: None)
    meta = {"model": policy.openai_client.MODEL, "route": "reextract", "http_class": "http_429",
            "error_kind": "payment_required", "usage": None, "openai_error_code": "insufficient_quota",
            "body": "synthetic-secret-canary", "request_id": "synthetic-secret-canary"}
    with ledger.db:
        aid = ledger.artifact_add_tx("cloud_usage_result", "synthetic-secret-canary",
                                     model=policy.openai_client.MODEL, project_id=1, meta=meta)
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (now, aid))
    before = ledger.db.total_changes
    report = cloud_cli.observe({"cloud_llm": {"provider": "openai", "mode": "off"}}, root=str(root))
    assert report["log"][0]["openai_error_code"] == "insufficient_quota"
    assert "synthetic-secret-canary" not in json.dumps(report)
    assert ledger.db.total_changes == before and not ledger.db.in_transaction


@pytest.mark.parametrize("row_model", [policy.MODEL, None, "synthetic-secret-canary"])
def test_quota_log_rejects_disagreement_between_row_and_meta_model(row_model):
    meta = {"model": policy.openai_client.MODEL, "http_class": "http_429",
            "error_kind": "payment_required", "usage": None,
            "openai_error_code": "insufficient_quota", "body": "synthetic-secret-canary"}
    safe = cloud_cli._safe_log(
        {"kind": "cloud_usage_result", "created_at": 1, "model": row_model}, meta)
    assert "openai_error_code" not in safe
    assert "synthetic-secret-canary" not in json.dumps(safe)


def test_observe_omits_quota_code_for_mismatched_row_model(local, monkeypatch):
    root, ledger, now = local
    monkeypatch.setattr(policy, "provider_key", lambda *a, **kw: None)
    meta = {"model": policy.openai_client.MODEL, "route": "reextract", "http_class": "http_429",
            "error_kind": "payment_required", "usage": None, "openai_error_code": "insufficient_quota",
            "body": "synthetic-secret-canary"}
    with ledger.db:
        aid = ledger.artifact_add_tx("cloud_usage_result", "synthetic-secret-canary",
                                     model=policy.MODEL, project_id=1, meta=meta)
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (now, aid))
    before = ledger.db.total_changes
    report = cloud_cli.observe({"cloud_llm": {"provider": "openai", "mode": "off"}}, root=str(root))
    assert "openai_error_code" not in report["log"][0]
    assert "synthetic-secret-canary" not in json.dumps(report)
    assert ledger.db.total_changes == before and not ledger.db.in_transaction


@pytest.mark.parametrize("duplicate", [
    '"openai_error_code":"synthetic-secret-canary"',
    '"error_kind":"rate_limited"',
    '"model":"other-provider"',
    '"http_class":"http_500"',
    '"usage":{}',
    '"extra":{"private":"first","private":"synthetic-secret-canary"}',
])
def test_observe_omits_quota_code_when_meta_has_duplicate_keys(local, monkeypatch, duplicate):
    root, ledger, now = local
    monkeypatch.setattr(policy, "provider_key", lambda *a, **kw: None)
    meta = {"model": policy.openai_client.MODEL, "route": "reextract", "http_class": "http_429",
            "error_kind": "payment_required", "usage": None, "openai_error_code": "insufficient_quota",
            "extra": {}, "body": "synthetic-secret-canary"}
    if duplicate.startswith('"extra"'):
        meta.pop("extra")
    raw = "{" + duplicate + "," + json.dumps(meta)[1:]
    with ledger.db:
        aid = ledger.artifact_add_tx("cloud_usage_result", "synthetic-secret-canary",
                                     model=policy.openai_client.MODEL, project_id=1, meta=meta)
        ledger.db.execute("UPDATE artifacts SET created_at=?,meta=? WHERE artifact_id=?", (now, raw, aid))
    before = ledger.db.total_changes
    report = cloud_cli.observe({"cloud_llm": {"provider": "openai", "mode": "off"}}, root=str(root))
    entry = report["log"][0]
    assert "openai_error_code" not in entry
    assert entry["route"] == "reextract" and entry["http_class"] == "http_429"
    assert entry["error_kind"] == "payment_required"
    assert "synthetic-secret-canary" not in json.dumps(report)
    assert ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0] == raw
    assert ledger.db.total_changes == before and not ledger.db.in_transaction


@pytest.mark.parametrize('diagnostic', [
    'rate_limit_exceeded', 'slow_down', 'rate_limit_error', 'missing_code', 'unknown_code',
    'unknown_type', 'conflicting_fields', 'invalid_field_type', 'body_not_bytes',
    'body_too_large', 'body_invalid_utf8', 'body_invalid_json', 'body_duplicate_keys',
    'body_nonfinite', 'body_too_deep', 'body_not_object', 'error_missing', 'error_not_object',
])
def test_openai_rate_log_shows_only_fixed_diagnostic(diagnostic):
    meta = {'model': policy.openai_client.MODEL, 'http_class': 'http_429', 'error_kind': 'rate_limited',
            'usage': None, 'openai_429_diagnostic': diagnostic, 'body': 'synthetic-secret-canary'}
    row = {'kind': 'cloud_usage_result', 'created_at': 1, 'model': policy.openai_client.MODEL}
    safe = cloud_cli._safe_log(row, meta)
    assert safe['openai_429_diagnostic'] == diagnostic
    assert 'synthetic-secret-canary' not in json.dumps(safe)


@pytest.mark.parametrize('changes,remove', [
    ({'openai_429_diagnostic': 'synthetic-secret-canary'}, ()),
    ({'openai_429_diagnostic': ['slow_down']}, ()),
    ({'http_class': 'http_500'}, ()),
    ({'error_kind': 'timeout'}, ()),
    ({'usage': USAGE}, ()),
    ({}, ('usage',)),
    ({'openai_error_code': 'insufficient_quota'}, ()),
    ({'model': policy.MODEL}, ()),
])
def test_openai_rate_log_omits_unconfirmed_diagnostic(changes, remove):
    meta = {'model': policy.openai_client.MODEL, 'http_class': 'http_429', 'error_kind': 'rate_limited',
            'usage': None, 'openai_429_diagnostic': 'slow_down', 'body': 'synthetic-secret-canary'}
    meta.update(changes)
    for key in remove:
        meta.pop(key)
    row = {'kind': 'cloud_usage_result', 'created_at': 1, 'model': policy.openai_client.MODEL}
    safe = cloud_cli._safe_log(row, meta)
    assert 'openai_429_diagnostic' not in safe and 'openai_error_code' not in safe
    assert 'synthetic-secret-canary' not in json.dumps(safe)


def test_observe_rate_diagnostic_rejects_model_mismatch_and_duplicate_keys(local, monkeypatch):
    root, ledger, now = local
    monkeypatch.setattr(policy, 'provider_key', lambda *a, **kw: None)
    meta = {'model': policy.openai_client.MODEL, 'route': 'reextract', 'http_class': 'http_429',
            'error_kind': 'rate_limited', 'usage': None, 'openai_429_diagnostic': 'slow_down',
            'body': 'synthetic-secret-canary'}
    duplicate = '{"openai_429_diagnostic":"synthetic-secret-canary",' + json.dumps(meta)[1:]
    with ledger.db:
        mismatch = ledger.artifact_add_tx('cloud_usage_result', 'synthetic-secret-canary',
                                          model=policy.MODEL, project_id=1, meta=meta)
        malformed = ledger.artifact_add_tx('cloud_usage_result', 'synthetic-secret-canary',
                                           model=policy.openai_client.MODEL, project_id=1, meta=meta)
        ledger.db.execute('UPDATE artifacts SET created_at=? WHERE artifact_id=?', (now, mismatch))
        ledger.db.execute('UPDATE artifacts SET created_at=?,meta=? WHERE artifact_id=?', (now, duplicate, malformed))
    before = ledger.db.total_changes
    report = cloud_cli.observe({'cloud_llm': {'provider': 'openai', 'mode': 'off'}}, root=str(root))
    assert len(report['log']) == 2
    assert all('openai_429_diagnostic' not in entry for entry in report['log'])
    assert all(entry['route'] == 'reextract' and entry['http_class'] == 'http_429' for entry in report['log'])
    assert 'synthetic-secret-canary' not in json.dumps(report)
    assert ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (malformed,)).fetchone()[0] == duplicate
    assert ledger.db.total_changes == before and not ledger.db.in_transaction


def test_observe_shows_confirmed_openai_rate_diagnostic(local, monkeypatch):
    root, ledger, now = local
    monkeypatch.setattr(policy, 'provider_key', lambda *a, **kw: None)
    meta = {'model': policy.openai_client.MODEL, 'route': 'reextract', 'http_class': 'http_429',
            'error_kind': 'rate_limited', 'usage': None, 'openai_429_diagnostic': 'body_duplicate_keys',
            'body': 'synthetic-secret-canary', 'request_id': 'synthetic-secret-canary'}
    with ledger.db:
        aid = ledger.artifact_add_tx('cloud_usage_result', 'synthetic-secret-canary',
                                     model=policy.openai_client.MODEL, project_id=1, meta=meta)
        ledger.db.execute('UPDATE artifacts SET created_at=? WHERE artifact_id=?', (now, aid))
    before = ledger.db.total_changes
    report = cloud_cli.observe({'cloud_llm': {'provider': 'openai', 'mode': 'off'}}, root=str(root))
    assert report['log'][0]['openai_429_diagnostic'] == 'body_duplicate_keys'
    assert 'synthetic-secret-canary' not in json.dumps(report)
    assert ledger.db.total_changes == before and not ledger.db.in_transaction


def test_openai_log_and_observe_preserve_priced_30m_cache_details(local, monkeypatch):
    root, ledger, now = local
    monkeypatch.setattr(policy, "provider_key", lambda *a, **kw: None)
    details = {"cached_tokens": 2, "cache_write_tokens": 4,
               "cache_write_tokens_details": {"ttl_30m_tokens": 4, "ttl_12h_tokens": 0}}
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "input_tokens_details": details, "output_tokens_details": {"reasoning_tokens": 0}}
    meta = {"model": policy.openai_client.MODEL, "price_version": policy.OPENAI_PRICE_VERSION,
            "route": "reextract", "usage": usage, "http_class": "http_200",
            "private": "synthetic-secret-canary"}
    with ledger.db:
        aid = ledger.artifact_add_tx("cloud_usage_result", "synthetic-secret-canary",
                                     model=policy.openai_client.MODEL, project_id=1, meta=meta)
        ledger.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (now, aid))
    before = ledger.db.total_changes
    safe = cloud_cli._safe_log({"kind": "cloud_usage_result", "created_at": now,
                                "model": policy.openai_client.MODEL}, meta)
    assert safe["usage"]["input_tokens_details"] == details
    report = cloud_cli.observe({"cloud_llm": {"provider": "openai", "mode": "off"}}, root=str(root))
    assert report["log"][0]["usage"]["input_tokens_details"] == details
    assert "synthetic-secret-canary" not in json.dumps(report)
    assert ledger.db.total_changes == before and not ledger.db.in_transaction


@pytest.mark.parametrize("breakdown", [
    {"ttl_30m_tokens": 3, "ttl_12h_tokens": 1},
    {"ttl_30m_tokens": 3, "ttl_12h_tokens": 0},
    {"ttl_30m_tokens": 4},
    {"ttl_30m_tokens": 4, "ttl_12h_tokens": 0, "synthetic-secret-canary": 0},
])
def test_openai_log_rejects_unpriced_or_inconsistent_cache_details(breakdown):
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 4,
                                      "cache_write_tokens_details": breakdown},
             "output_tokens_details": {"reasoning_tokens": 0}}
    meta = {"model": policy.openai_client.MODEL, "price_version": policy.OPENAI_PRICE_VERSION,
            "usage": usage, "http_class": "http_200"}
    row = {"kind": "cloud_usage_result", "created_at": 1, "model": policy.openai_client.MODEL}
    with pytest.raises(policy.AnthropicError) as error:
        cloud_cli._safe_log(row, meta)
    assert "synthetic-secret-canary" not in str(error.value)
