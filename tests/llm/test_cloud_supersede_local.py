"""Synthetic checks for recording a patient-context request as replaced by local work."""
from contextlib import closing
import hashlib
import json
import os

import pytest

import anthropic_client
import cloud_policy as policy
from semantic_testkit import _ledger, _message, _patient

CFG = {"cloud_llm": {"mode": "on", "project_ids": [1], "provider": "anthropic"}}
OWNER = {"workday_id": "synthetic", "source_sig": "synthetic"}
BINDING = "b" * 64
STALE = 2 + policy.RESULT_STALE_MARGIN_S   # _reserve's attempt timeout plus the margin


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "pricing_valid", lambda *args, **kwargs: True)
    with closing(_ledger(tmp_path)) as db:
        _patient(db)
        db.save_messages([_message()])
        yield db


def _body(prompt):
    return anthropic_client.AnthropicClient("synthetic").body(prompt, 100, None)


def _reserve(ledger, prompt="synthetic prompt", *, unit="synthetic-unit", route="patient_context",
             binding=None, timeout=2):
    return policy.usage_reserver(ledger, CFG, route=route, project_id=1, message_id=1,
        unit_key=unit, owner=OWNER, input_binding=binding)(_body(prompt), timeout)


def _settle(ledger, reservation, status, error=None, usage=None, **extra):
    policy.record_result(ledger, reservation, {
        "model": reservation["model"], "request_fp": reservation["request_fp"], "usage": usage,
        "http_class": status, "request_id": None, "error_kind": error, **extra})


USAGE = {"input_tokens": 10, "output_tokens": 2}


def _factory(prompt="synthetic prompt", *, unit="synthetic-unit", route="patient_context", binding=None):
    body = _body(prompt)
    return lambda: (policy._request_identity(policy._cfg(CFG), body, route=route, project_id=1,
        unit_key=unit, message_id=1, owner=OWNER, selection=None, input_binding=binding), body)


def _supersede(ledger, *, now=None, **kwargs):
    route = kwargs.pop("route", "patient_context")
    unit = kwargs.pop("unit", "synthetic-unit")
    ledger.db.execute("BEGIN IMMEDIATE")
    try:
        written = policy.supersede_local_tx(ledger, route=route, project_id=1, message_id=1,
            unit_key=unit, request_factory=_factory(unit=unit, route=route, **kwargs), now=now)
    except BaseException:
        ledger.db.rollback()
        raise
    ledger.db.commit()
    return written


def _guard(ledger, *, allow=False, **kwargs):
    unit = kwargs.pop("unit", "synthetic-unit")
    policy.guard_local_fallback(ledger, route="patient_context", project_id=1, message_id=1,
        unit_key=unit, request_factory=_factory(unit=unit, **kwargs), allow_supersede=allow)


def _refusal(call):
    with pytest.raises(anthropic_client.AnthropicError) as caught:
        call()
    assert (caught.value.kind, caught.value.detail) == ("budget_exceeded", "cloud_unit_unresolved")
    return caught.value


def _financial(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]


def _dispositions(ledger):
    return [json.loads(row["meta"]) for row in ledger.db.execute(
        "SELECT meta FROM artifacts WHERE kind='cloud_usage_disposition' ORDER BY artifact_id")]


def _created(ledger, reservation):
    return ledger.db.execute("SELECT created_at FROM artifacts WHERE kind='cloud_usage_reserve' "
                             "AND json_extract(meta,'$.reservation_id')=?",
                             (reservation["reservation_id"],)).fetchone()[0]


@pytest.mark.parametrize("history", [False, True])
def test_supersede_requires_caller_transaction(ledger, history):
    if history:
        _settle(ledger, _reserve(ledger), "timeout", "timeout")
    with pytest.raises(RuntimeError, match="^cloud_disposition_transaction_required$"):
        policy.supersede_local_tx(ledger, route="patient_context", project_id=1, message_id=1,
            unit_key="synthetic-unit", request_factory=_factory())
    assert _dispositions(ledger) == []


def test_supersede_rejects_other_routes(ledger, monkeypatch):
    monkeypatch.setattr(policy, "send_route", lambda route: True)
    _settle(ledger, _reserve(ledger, route="reextract"), "timeout", "timeout")
    with pytest.raises(ValueError, match="^cloud_supersede_route_invalid$"):
        _supersede(ledger, route="reextract")
    with pytest.raises(ValueError, match="^cloud_supersede_scope_invalid$"):
        policy.guard_local_fallback(ledger, route="reextract", project_id=1, message_id=1,
            unit_key="synthetic-unit", request_factory=_factory(route="reextract"), allow_supersede=True)
    # the route is refused before any history is read
    with pytest.raises(ValueError, match="^cloud_supersede_scope_invalid$"):
        policy.guard_local_fallback(ledger, route="reextract", project_id=1, message_id=None,
            unit_key="synthetic-unit", request_factory=_factory(route="reextract"), allow_supersede=True)
    candidate, body = _factory()()
    for kwargs in ({"local_fallback": False, "supersede": []}, {"local_fallback": True, "supersede": ()}):
        with pytest.raises(ValueError, match="^cloud_supersede_scope_invalid$"):
            policy._guard_repeated_request(ledger, candidate, current_body=body, **kwargs)
    assert _dispositions(ledger) == []


@pytest.mark.parametrize("status,error,usage", [
    ("timeout", "timeout", None),                 # repeat_unsettled
    ("http_200", None, USAGE),                    # repeat_success
    ("http_200", "contract_error", USAGE),        # repeat_failed
])
def test_supersede_appends_one_row_per_verified_same_request_and_changes_no_receipt(
        ledger, status, error, usage):
    rejected = _reserve(ledger)
    _settle(ledger, rejected, "http_429", "rate_limited")          # a confirmed rejection: no row
    paid = _reserve(ledger)
    _settle(ledger, paid, status, error, usage)
    other = _reserve(ledger, unit="synthetic-other", prompt="synthetic other")
    _settle(ledger, other, "timeout", "timeout")
    before, report = _financial(ledger), policy.usage_report(ledger)
    total = policy._tally(ledger).total
    _refusal(lambda: _guard(ledger))
    written = _supersede(ledger)
    assert len(written) == 1 and _dispositions(ledger) == [
        {**policy._receipt_binding(paid), "disposition": "superseded_local"}]
    assert _financial(ledger) == before and policy.usage_report(ledger) == report
    assert policy._tally(ledger).total == total
    _guard(ledger)                       # the strict local guard now passes
    _guard(ledger, allow=True)
    assert _supersede(ledger) == [] and len(_dispositions(ledger)) == 1   # first disposition wins
    # the request itself is never sent again
    refused = _refusal(lambda: policy.request_send_block_reason(ledger, CFG, _body("synthetic prompt"),
        route="patient_context", project_id=1, unit_key="synthetic-unit", message_id=1, owner=OWNER))
    assert refused.history_refusal == "disposition" and refused.history_superseded is True
    _refusal(lambda: _reserve(ledger))
    # another unit's history is untouched
    _refusal(lambda: _guard(ledger, unit="synthetic-other", prompt="synthetic other"))
    assert _financial(ledger) == before


def test_a_young_missing_result_is_never_superseded(ledger):
    young = _reserve(ledger)
    refused = _refusal(lambda: _supersede(ledger, now=_created(ledger, young) + 1))
    assert refused.history_refusal == "result_missing" and refused.history_stale is False
    _refusal(lambda: _guard(ledger, allow=True))
    assert _dispositions(ledger) == []


def test_a_legacy_unproven_request_is_never_superseded(ledger):
    paid = _reserve(ledger)
    _settle(ledger, paid, "http_200", usage=USAGE)
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                          "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')")
    before = _financial(ledger)
    assert _refusal(lambda: _supersede(ledger)).history_refusal == "repeat_success"
    assert _refusal(lambda: _guard(ledger, allow=True)).history_refusal == "repeat_success"
    assert _dispositions(ledger) == [] and _financial(ledger) == before


def test_an_inconsistent_receipt_is_never_superseded(ledger):
    _reserve(ledger)
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'",
                          ('{"unit_key":"other","unit_key":"synthetic-unit"}',))
    assert _refusal(lambda: _supersede(ledger)).history_refusal == "receipt_inconsistent"
    assert _dispositions(ledger) == []


@pytest.mark.parametrize("value", ["synthetic_unknown", ["superseded_local"], None])
def test_an_unknown_disposition_value_blocks_every_mode(ledger, value):
    paid = _reserve(ledger)
    _settle(ledger, paid, "timeout", "timeout")
    ledger.artifact_add("cloud_usage_disposition", "{}", project_id=1, message_id=1, model=paid["model"],
                        meta={**policy._receipt_binding(paid), "disposition": value})
    for call in (lambda: _guard(ledger), lambda: _guard(ledger, allow=True), lambda: _supersede(ledger),
                 lambda: _reserve(ledger)):
        assert _refusal(call).history_refusal == "disposition"
    assert len(_dispositions(ledger)) == 1


@pytest.mark.parametrize("timeout,stored", [(2, None), (2, 0), (2, 601), (2, "2"), (2, True)])
def test_stale_missing_result_is_superseded_only_past_the_threshold(ledger, timeout, stored):
    reservation = _reserve(ledger, timeout=timeout)
    if stored is not None:
        reservation = dict(reservation, timeout_seconds=stored)
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'",
                              (json.dumps(reservation),))
    wait = (timeout if stored is None else 600) + policy.RESULT_STALE_MARGIN_S
    created = _created(ledger, reservation)
    refused = _refusal(lambda: _supersede(ledger, now=created + wait - 1))
    assert refused.history_refusal == "result_missing" and refused.history_stale is False
    assert _dispositions(ledger) == []
    assert len(_supersede(ledger, now=created + wait)) == 1
    assert [row["disposition"] for row in _dispositions(ledger)] == ["superseded_local"]
    _guard(ledger)


def test_allow_mode_reads_the_stale_clock_from_cloud_policy(ledger, monkeypatch):
    reservation = _reserve(ledger)
    created = _created(ledger, reservation)
    monkeypatch.setattr(policy.time, "time", lambda: created + STALE - 1)
    refused = _refusal(lambda: _guard(ledger, allow=True))
    assert refused.history_stale is False
    monkeypatch.setattr(policy.time, "time", lambda: created + STALE)
    _guard(ledger, allow=True)                       # a stale missing result is a supersede target
    assert _refusal(lambda: _guard(ledger)).history_stale is True   # the strict guard still holds it
    assert _dispositions(ledger) == []               # allow mode writes nothing


def test_late_result_after_supersede_settles_but_is_never_adopted(ledger):
    reservation = _reserve(ledger, binding=BINDING)
    created = _created(ledger, reservation)
    assert len(_supersede(ledger, binding=BINDING, now=created + STALE)) == 1
    text = '{"synthetic": true}'
    _settle(ledger, reservation, "http_200", usage=USAGE,
            answer_text_sha256_v1=hashlib.sha256(text.encode()).hexdigest(), answer_finish_reason_v1="stop")
    assert len(ledger.artifacts("cloud_usage_result")) == 1
    aid = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve'").fetchone()[0]
    with pytest.raises(ValueError, match="^cloud_receipt_invalid$"):
        policy.reservation_evidence_batch(ledger.db, [aid])
    with pytest.raises(ValueError, match="^cloud_result_superseded$"):
        policy.recover_response(ledger, route="patient_context", project_id=1, message_id=1,
            unit_key="synthetic-unit", input_binding=BINDING, request_fp=reservation["request_fp"])
    assert _refusal(lambda: _reserve(ledger, binding=BINDING)).history_refusal == "disposition"
    _guard(ledger, binding=BINDING)                  # local work stays allowed


def test_a_missing_superseded_result_recovers_as_superseded(ledger):
    reservation = _reserve(ledger, binding=BINDING)
    _supersede(ledger, binding=BINDING, now=_created(ledger, reservation) + STALE)
    with pytest.raises(ValueError, match="^cloud_result_superseded$"):
        policy.recover_response(ledger, route="patient_context", project_id=1, message_id=1,
            unit_key="synthetic-unit", input_binding=BINDING, request_fp=reservation["request_fp"])


def test_discard_after_superseded_local_writes_nothing(ledger):
    reservation = _reserve(ledger)
    _settle(ledger, reservation, "timeout", "timeout")
    [first] = _supersede(ledger)
    for reason in ("discarded_scope", "discarded_workday", "superseded_local"):
        ledger.db.execute("BEGIN IMMEDIATE")
        assert policy.record_disposition_tx(ledger, reservation, reason) == first
        ledger.db.commit()
    assert [row["disposition"] for row in _dispositions(ledger)] == ["superseded_local"]


def test_a_discarded_row_is_never_replaced_by_superseded_local(ledger):
    reservation = _reserve(ledger)
    _settle(ledger, reservation, "timeout", "timeout")
    ledger.db.execute("BEGIN IMMEDIATE")
    policy.record_disposition_tx(ledger, reservation, "discarded_scope")
    ledger.db.commit()
    ledger.db.execute("BEGIN IMMEDIATE")
    with pytest.raises(anthropic_client.AnthropicError, match="^pricing_unverified$") as caught:
        policy.record_disposition_tx(ledger, reservation, "superseded_local")
    ledger.db.rollback()
    assert caught.value.detail == "disposition_conflict"
    assert [row["disposition"] for row in _dispositions(ledger)] == ["discarded_scope"]


@pytest.mark.parametrize("inline", [False, True])
def test_allow_mode_passes_discarded_without_writing(ledger, inline):
    reservation = _reserve(ledger)
    if inline:
        _settle(ledger, reservation, "http_200", usage=USAGE, disposition="discarded_scope")
    else:
        _settle(ledger, reservation, "http_200", usage=USAGE)
        ledger.db.execute("BEGIN IMMEDIATE")
        policy.record_disposition_tx(ledger, reservation, "discarded_workday")
        ledger.db.commit()
    before = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    assert _refusal(lambda: _guard(ledger)).history_refusal == "disposition"      # strict: unchanged
    _guard(ledger, allow=True)
    assert _supersede(ledger) == []
    assert _refusal(lambda: _reserve(ledger)).history_refusal == "disposition"    # never resent
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before


@pytest.mark.parametrize("status,error,usage,expected", [
    ("timeout", "timeout", None, "repeat_unsettled"),
    ("http_200", None, USAGE, "repeat_success"),
    ("http_200", "contract_error", USAGE, "repeat_failed"),
    (None, None, None, "result_missing"),
    ("disposition", "discarded_scope", None, "disposition"),
    ("disposition", "superseded_local", None, "disposition"),
    ("disposition", ["discarded_scope"], None, "disposition"),     # a malformed value (review S-1)
    ("disposition", {"kind": "discarded_scope"}, None, "disposition"),
    ("inconsistent", None, None, "receipt_inconsistent"),
])
def test_default_guard_path_is_unchanged(ledger, status, error, usage, expected):
    reservation = _reserve(ledger)
    if status == "disposition":
        _settle(ledger, reservation, "timeout", "timeout")
        ledger.artifact_add("cloud_usage_disposition", "{}", project_id=1, message_id=1,
                            model=reservation["model"],
                            meta={**policy._receipt_binding(reservation), "disposition": error})
    elif status == "inconsistent":
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE kind='cloud_usage_reserve'",
                              ('{"unit_key":"other","unit_key":"synthetic-unit"}',))
    elif status is not None:
        _settle(ledger, reservation, status, error, usage)
    before = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    send = _refusal(lambda: _reserve(ledger))
    check = _refusal(lambda: policy.request_send_block_reason(ledger, CFG, _body("synthetic prompt"),
        route="patient_context", project_id=1, unit_key="synthetic-unit", message_id=1, owner=OWNER))
    assert send.history_refusal == check.history_refusal == expected
    assert not hasattr(send, "history_stale") and not hasattr(check, "history_stale")   # no clock read
    if expected in ("disposition", "receipt_inconsistent") and error != "superseded_local":
        assert _refusal(lambda: _guard(ledger)).history_refusal == expected
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before


def test_unit_reserved_is_conservative_on_unreadable_meta(ledger):
    def reserved(unit="synthetic-unit", message_id=1, route="patient_context"):
        return policy.unit_reserved(ledger.db, route=route, project_id=1, message_id=message_id, unit_key=unit)
    assert reserved() is False
    _reserve(ledger)
    assert reserved() is True
    assert reserved(unit="synthetic-other") is False and reserved(message_id=None) is False
    assert reserved(route="reextract") is False
    ledger.artifact_add("cloud_usage_reserve", "{}", project_id=1, message_id=None, model="synthetic",
                        meta={"route": "patient_context", "unit_key": "synthetic-unit"})
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta='{not json' WHERE kind='cloud_usage_reserve' "
                          "AND message_id IS NULL")
    assert reserved(message_id=None) is True and reserved(unit="synthetic-other", message_id=None) is True
    assert reserved(unit="synthetic-other") is False


def test_selected_key_problem_names_the_fixed_code(tmp_path):
    root = str(tmp_path)
    cfg = {"cloud_llm": {"mode": "on", "provider": "anthropic", "project_ids": [1]}}
    assert policy.selected_key_problem(root, cfg) == "env_file_missing"
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n")
    os.chmod(env, 0o600)
    assert policy.selected_key_problem(root, cfg) == "key_missing"
    env.write_text(f"{anthropic_client.KEY_ENV}=synthetic-key\n")
    assert policy.selected_key_problem(root, cfg) is None
    os.chmod(env, 0o644)
    assert policy.selected_key_problem(root, cfg) == "env_file_mode_not_0600"


DAY = 86400


def _age(ledger, days):
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET created_at=created_at-? "
                          "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result')", (days * DAY,))


@pytest.mark.parametrize("days", [61, 63, 70])
def test_an_old_paid_request_is_replaced_like_a_recent_one(ledger, days):
    # maintenance keeps receipts a current patient context still cites past the 62-day prune
    paid = _reserve(ledger)
    _settle(ledger, paid, "http_200", usage=USAGE)
    _age(ledger, days)
    before = _financial(ledger)
    _refusal(lambda: _guard(ledger))
    _guard(ledger, allow=True)
    [first] = _supersede(ledger)
    assert _dispositions(ledger) == [{**policy._receipt_binding(paid), "disposition": "superseded_local"}]
    _guard(ledger)
    _guard(ledger, allow=True)
    assert _supersede(ledger) == []
    for reason in ("discarded_scope", "discarded_workday", "superseded_local"):
        ledger.db.execute("BEGIN IMMEDIATE")
        assert policy.record_disposition_tx(ledger, paid, reason) == first
        ledger.db.commit()
    assert len(_dispositions(ledger)) == 1 and _financial(ledger) == before
    _guard(ledger)


def test_an_old_discard_still_needs_a_retained_reservation(ledger):
    # the discard path itself is unchanged: an unretained reservation is not found
    paid = _reserve(ledger)
    _settle(ledger, paid, "timeout", "timeout")
    _age(ledger, 63)
    ledger.db.execute("BEGIN IMMEDIATE")
    with pytest.raises(anthropic_client.AnthropicError, match="^pricing_unverified$") as caught:
        policy.record_disposition_tx(ledger, paid, "discarded_scope")
    ledger.db.rollback()
    assert caught.value.detail == "reservation_missing" and _dispositions(ledger) == []


def test_supersede_replaces_only_this_units_own_requests(ledger):
    paid = _reserve(ledger, unit="synthetic-a")
    _settle(ledger, paid, "http_200", usage=USAGE)
    # another unit key with the same prompt is the same request: it is never sent, never replaced
    assert _refusal(lambda: _guard(ledger, allow=True, unit="synthetic-b")).history_refusal == "repeat_success"
    assert _refusal(lambda: _supersede(ledger, unit="synthetic-b")).history_refusal == "repeat_success"
    assert _dispositions(ledger) == []
    assert len(_supersede(ledger, unit="synthetic-a")) == 1
    assert [row["unit_key"] for row in _dispositions(ledger)] == ["synthetic-a"]
    _guard(ledger, unit="synthetic-b")             # the replaced request no longer holds either


def test_a_legacy_unproven_missing_result_is_never_superseded(ledger):
    reservation = _reserve(ledger)
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                          "WHERE kind='cloud_usage_reserve'")
    past = _created(ledger, reservation) + STALE + 3600
    refused = _refusal(lambda: _supersede(ledger, now=past))
    assert refused.history_refusal == "result_missing" and refused.history_stale is True
    assert _dispositions(ledger) == []


def test_a_legacy_unproven_missing_result_holds_allow_mode(ledger, monkeypatch):
    reservation = _reserve(ledger)
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.logical_request_fp') "
                          "WHERE kind='cloud_usage_reserve'")
    monkeypatch.setattr(policy.time, "time", lambda: _created(ledger, reservation) + STALE + 3600)
    assert _refusal(lambda: _guard(ledger, allow=True)).history_refusal == "result_missing"


def test_an_invalid_tally_refuses_without_writing(ledger):
    paid = _reserve(ledger)
    _settle(ledger, paid, "timeout", "timeout")
    # an orphan result elsewhere leaves the month's totals unverifiable
    ledger.artifact_add("cloud_usage_result", "{}", project_id=1, message_id=None, model=paid["model"],
                        meta={**policy._receipt_binding(paid), "reservation_id": "f" * 32,
                              "http_class": "timeout", "error_kind": "timeout", "usage": None,
                              "cost_usd": paid["max_cost_usd"], "route": "patient_context",
                              "unit_key": "synthetic-elsewhere"})
    assert policy._tally(ledger).invalid
    for call in (lambda: _supersede(ledger), lambda: _guard(ledger, allow=True)):
        with pytest.raises(anthropic_client.AnthropicError) as caught:
            call()
        assert (caught.value.kind, caught.value.detail) == ("budget_exceeded", "cloud_unit_unresolved")
    assert _dispositions(ledger) == []


@pytest.mark.parametrize("value", ["synthetic", "superseded_local", ["discarded_scope"]])
def test_a_malformed_inline_disposition_blocks_allow_mode(ledger, value):
    reservation = _reserve(ledger)
    _settle(ledger, reservation, "http_200", usage=USAGE, disposition="discarded_scope")
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.disposition',json(?)) "
                          "WHERE kind='cloud_usage_result'", (json.dumps(value),))
    before = _financial(ledger)
    for call in (lambda: _guard(ledger, allow=True), lambda: _supersede(ledger)):
        assert _refusal(call).history_refusal in ("disposition", "receipt_inconsistent")
    assert _dispositions(ledger) == [] and _financial(ledger) == before


def test_a_write_failure_after_an_earlier_target_leaves_no_row(ledger):
    first = _reserve(ledger)
    _settle(ledger, first, "timeout", "timeout")
    # a second reservation of the same request (synthetic copy) whose disposition sits under another message
    second = {**first, "reservation_id": "e" * 32}
    ledger.artifact_add("cloud_usage_reserve", "{}", project_id=1, message_id=1, model=first["model"], meta=second)
    _settle(ledger, second, "timeout", "timeout")
    ledger.artifact_add("cloud_usage_disposition", "{}", project_id=1, message_id=None, model=first["model"],
                        meta={**policy._receipt_binding(second), "disposition": "discarded_scope"})
    before = [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    ledger.db.execute("BEGIN IMMEDIATE")
    with pytest.raises(anthropic_client.AnthropicError) as caught:
        policy.supersede_local_tx(ledger, route="patient_context", project_id=1, message_id=1,
            unit_key="synthetic-unit", request_factory=_factory())
    assert caught.value.detail == "disposition_conflict"
    ledger.db.commit()          # even a caller that commits anyway keeps no partial replacement
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before
