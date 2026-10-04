"""Synthetic explicit C1 grants preserve all legacy authorization boundaries."""

import json

import pytest

from c1_contract import C1_FIELDS
import ext_contract as ext
from test_c1_contract import _body, _message
from test_ext_contract import NOW, _auth, _records


def test_explicit_c1_grant_still_cannot_enter_legacy_envelope(tmp_path):
    path = _auth(tmp_path, fields=list(C1_FIELDS), max_snapshot_age_s=3600)
    authorization = ext.load_authorization(path, NOW, c1=True)
    assert authorization["fields"] == list(C1_FIELDS)
    assert authorization["confirm_human"] is True
    with pytest.raises(ext.ContractError, match="auth_field_not_exportable"):
        ext.load_authorization(path, NOW)
    with pytest.raises(ext.ContractError, match="auth_field_not_exportable"):
        ext.build_envelope(_records(), authorization, NOW - 10, now=NOW)


@pytest.mark.parametrize("field", ["fields", "patients", "max_snapshot_age_s"])
def test_c1_requires_explicit_profile_fields_but_legacy_keeps_defaults(tmp_path, field):
    path = _auth(tmp_path)
    raw = json.loads(path.read_text())
    del raw[field]
    path.write_text(json.dumps(raw))
    assert ext.load_authorization(path, NOW) == raw
    with pytest.raises(ext.ContractError):
        ext.load_authorization(path, NOW, c1=True)


@pytest.mark.parametrize("override", [
    {"fields": list(C1_FIELDS[:-1])},
    {"fields": [*C1_FIELDS, "stat"]},
    {"fields": [*C1_FIELDS, "message"]},
    {"patients": [1]},
    {"max_snapshot_age_s": 3601},
    {"max_snapshot_age_s": True},
    {"retention_days": 31},
    {"confirm_human": False},
    {"revoked": True},
    {"expires_at": NOW},
    {"scope": "detail"},
    {"reason": ""},
])
def test_c1_retains_consent_expiry_scope_and_profile_refusals(tmp_path, override):
    values = {"fields": list(C1_FIELDS), "max_snapshot_age_s": 3600, **override}
    with pytest.raises(ext.ContractError):
        ext.load_authorization(_auth(tmp_path, **values), NOW, c1=True)


@pytest.mark.parametrize("text", ["", "SYNTHETIC BODY"])
def test_c1_body_exception_is_only_for_paired_root_body_text(text):
    body, message = _body(text), _message()
    assert ext._check_record_keys(body, c1=True, message=message) is None
    with pytest.raises(ext.ContractError, match="forbidden_field:body_text"):
        ext._check_record_keys(body)
    with pytest.raises(ext.ContractError, match="forbidden_field:body_text"):
        ext._check_record_keys({**message, "body_text": text}, c1=True)


@pytest.mark.parametrize("extra", [
    {"patient_name": "SYNTHETIC"},
    {"sender": "SYNTHETIC"},
    {"note": "SYNTHETIC"},
    {"future_metadata": {"body_text": "SYNTHETIC"}},
])
def test_c1_body_exception_does_not_exempt_other_or_nested_private_keys(extra):
    with pytest.raises(ext.ContractError, match="forbidden_field:"):
        ext._check_record_keys({**_body(), **extra}, c1=True, message=_message())


@pytest.mark.parametrize("text", [None, {"sender": "SYNTHETIC"}])
def test_c1_body_exception_still_requires_a_string(text):
    with pytest.raises(ext.ContractError, match="record_field_not_exportable"):
        ext._check_record_keys({**_body(), "body_text": text},
                               c1=True, message=_message())
