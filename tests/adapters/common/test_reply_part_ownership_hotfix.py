"""Old synthetic journals cannot authorize two cards to edit one remote part."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from adapters.common import envelopes, journal
from discord_delivery_testkit import _claim, _mkworker, _receipts, _spec


CURRENT_CARD = "9002"
PRIOR_PART = "6001"
OLD_DELIVERY = "00000000-0000-4000-8000-00000000d001"


@pytest.fixture
def world(tmp_path):
    worker, reg, _ = _mkworker(tmp_path)
    calls = []

    async def perform(claim, part, ctx):
        calls.append((claim["spec"]["delivery_id"], part["part_id"], ctx["card_message_id"]))
        return {"result": "delivered", "remote_id": part["prior_remote_id"]}

    worker._perform_part = perform
    return SimpleNamespace(worker=worker, reg=reg, calls=calls)


def _post_spec(kind, marker="post_source_id"):
    spec = _spec(["合成更新文"], thread_id="7700", op="update")
    spec["render_rev"] = 2
    spec["parts"]["context"] = {"project_id": 1, "source_message_id": 202}
    if marker == "post_source_id":
        spec["parts"]["post_source_id"] = 202
    elif marker == "reply_key":
        spec["card_key"] += "|reply:202"
    part = {"kind": kind, "index": 1, "prior_remote_id": PRIOR_PART,
            "part_id": "body:0001" if kind == "body_part" else "attach:0001",
            "bytes": 10, "sha256": "a" * 64}
    if kind == "attachment_part":
        part.update(attachment_id=1, name="synthetic.pdf")
    spec["parts"]["manifest"] = [{"kind": "card", "part_id": "card", "index": 0}, part]
    return spec, part


def _primary(world, spec, message_id, **fields):
    claim = _claim(spec)
    claim["attempt_id"] = "primary-" + spec["delivery_id"]
    journal.append(world.worker._dirs["state"], "synthetic-old", {
        "phase": "result", "attempt_id": claim["attempt_id"],
        "delivery_id": spec["delivery_id"], "result": "delivered", "message_id": message_id,
        "receipt_envelope": envelopes.transport_receipt(claim, "delivered", message_id=message_id), **fields})


def _old_owner(world, spec, part, *, primary="9001", foreign_scope=None, invalid_timestamp=False):
    old = copy.deepcopy(spec)
    old.update(delivery_id=OLD_DELIVERY, render_rev=1, op="create")
    if foreign_scope:
        old["delivery"].update(foreign_scope)
    if primary is not None:
        _primary(world, old, primary, **({"ts": "invalid"} if invalid_timestamp else {}))
    old_part = {key: value for key, value in part.items() if key != "prior_remote_id"}
    journal.append(world.worker._dirs["state"], "synthetic-old", {
        "phase": "result", "attempt_id": envelopes.part_attempt_id(OLD_DELIVERY, part["part_id"]),
        "delivery_id": OLD_DELIVERY, "part_id": part["part_id"], "kind": part["kind"],
        "result": "delivered", "remote_id": PRIOR_PART,
        "receipt_envelope": envelopes.part_receipt(_claim(old), old_part, "delivered", remote_id=PRIOR_PART)})


def _run(world, spec, part, entry="deliver"):
    _primary(world, spec, CURRENT_CARD)
    if entry == "resume":
        asyncio.run(world.worker._resume_parts(spec))
    else:
        asyncio.run(world.worker._deliver_parts(_claim(spec), CURRENT_CARD))
    aid = envelopes.part_attempt_id(spec["delivery_id"], part["part_id"])
    rows = journal.scan(world.worker._dirs["state"])[aid]
    assert [row["phase"] for row in rows] == ["started", "result", "receipt"]
    result = rows[1]
    receipts = _receipts(world.worker._dirs["cmd_int"])
    assert len(receipts) == 1 and receipts[0] == result["receipt_envelope"]
    assert world.reg.parts_done(spec["delivery_id"])
    return result


@pytest.mark.parametrize("entry", ["deliver", "resume"])
@pytest.mark.parametrize("marker", ["post_source_id", "reply_key"])
@pytest.mark.parametrize("kind", ["body_part", "attachment_part"])
def test_another_primary_owning_the_prior_holds_without_performing(world, entry, marker, kind):
    spec, part = _post_spec(kind, marker)
    _old_owner(world, spec, part)
    result = _run(world, spec, part, entry)
    assert world.calls == []
    assert result["result"] == "unknown" and result["error_code"] == "part_owner_ambiguous"
    assert "remote_id" not in result["receipt_envelope"]


@pytest.mark.parametrize("kind", ["body_part", "attachment_part"])
def test_an_older_render_of_the_same_primary_may_update_its_prior(world, kind):
    spec, part = _post_spec(kind)
    _old_owner(world, spec, part, primary=CURRENT_CARD)
    result = _run(world, spec, part)
    assert world.calls == [(spec["delivery_id"], part["part_id"], CURRENT_CARD)]
    assert result["result"] == "delivered" and result["remote_id"] == PRIOR_PART


@pytest.mark.parametrize("scope_key", ["profile", "application_id", "channel_id", "guild_id"])
def test_same_remote_id_in_another_scope_does_not_block_this_card(world, scope_key):
    spec, part = _post_spec("body_part")
    _old_owner(world, spec, part, foreign_scope={scope_key: "synthetic-other"})
    result = _run(world, spec, part)
    assert world.calls == [(spec["delivery_id"], part["part_id"], CURRENT_CARD)]
    assert result["result"] == "delivered" and result["remote_id"] == PRIOR_PART


@pytest.mark.parametrize("kind", ["body_part", "attachment_part"])
def test_legacy_card_preserves_its_existing_part_delivery_contract(world, kind):
    spec, part = _post_spec(kind, marker="legacy")
    _old_owner(world, spec, part)
    result = _run(world, spec, part)
    assert world.calls == [(spec["delivery_id"], part["part_id"], CURRENT_CARD)]
    assert result["result"] == "delivered" and result["remote_id"] == PRIOR_PART


@pytest.mark.parametrize("fault", ["missing", "invalid_timestamp"])
def test_unproven_primary_of_a_recorded_owner_holds_the_prior(world, fault):
    spec, part = _post_spec("body_part")
    _old_owner(world, spec, part, primary=None if fault == "missing" else "9001",
               invalid_timestamp=fault == "invalid_timestamp")
    result = _run(world, spec, part, entry="resume")
    assert world.calls == []
    assert result["result"] == "unknown" and result["error_code"] == "part_owner_ambiguous"


@pytest.mark.parametrize("bad_delivery_id", [[], {}])
def test_malformed_owner_delivery_is_unknown_without_breaking_the_index(world, bad_delivery_id):
    records = {"synthetic-bad": [{
        "phase": "result", "result": "delivered", "kind": "body_part",
        "delivery_id": bad_delivery_id, "remote_id": PRIOR_PART,
        "receipt_envelope": world.worker.scope()}]}
    assert world.worker._part_owners(records, {}) == {PRIOR_PART: {(None, None)}}


def test_resume_batch_builds_ownership_index_once_and_drives_every_part(world, monkeypatch):
    specs = []
    for index in range(16):
        spec, part = _post_spec("body_part")
        spec["delivery_id"] = f"00000000-0000-4000-8000-{index + 100:012x}"
        spec["parts"]["post_source_id"] = spec["parts"]["context"]["source_message_id"] = 202 + index
        if index >= 8:
            part["prior_remote_id"] = str(7000 + index)
        _primary(world, spec, str(9002 + index))
        specs.append(spec)
    _old_owner(world, specs[0], specs[0]["parts"]["manifest"][1])
    index_calls, primary_calls = [], []
    real_owners = world.worker._part_owners
    real_primaries = journal.primary_results

    def owners(records, primaries=None):
        index_calls.append(True)
        return real_owners(records, primaries)

    def primaries(records):
        primary_calls.append(True)
        return real_primaries(records)

    monkeypatch.setattr(world.worker, "_part_owners", owners)
    monkeypatch.setattr(journal, "primary_results", primaries)
    asyncio.run(world.worker._resume_dead(specs))
    assert len(index_calls) == len(primary_calls) == 1
    assert world.calls == [(spec["delivery_id"], "body:0001", str(9002 + index))
                           for index, spec in enumerate(specs) if index >= 8]
    receipts = {row["delivery_id"]: row for row in _receipts(world.worker._dirs["cmd_int"])}
    assert len(receipts) == len(specs)
    for index, spec in enumerate(specs):
        receipt = receipts[spec["delivery_id"]]
        assert world.reg.parts_done(spec["delivery_id"])
        if index < 8:
            assert receipt["result"] == "unknown" and receipt["error_code"] == "part_owner_ambiguous"
        else:
            assert receipt["result"] == "delivered" and receipt["remote_id"] == str(7000 + index)


@pytest.mark.parametrize("entry", ["deliver", "resume"])
@pytest.mark.parametrize("op", ["create", "update"])
def test_slack_reply_inherits_only_its_proven_parent_attachment(world, monkeypatch, entry, op):
    monkeypatch.setattr(world.worker, "_ours", lambda envelope: True)
    spec, part = _post_spec("attachment_part", marker="reply_key")
    spec["op"] = op
    spec["delivery"].update(transport="slack", team_id="T_SYNTHETIC")
    spec["parts"]["source_thread"] = True
    part["prior_owner_message_id"] = spec["delivery"]["thread_id"]
    _old_owner(world, spec, part, primary=spec["delivery"]["thread_id"])
    result = _run(world, spec, part, entry)
    assert result["result"] == "delivered"
    assert world.calls == [(spec["delivery_id"], part["part_id"], CURRENT_CARD)]


@pytest.mark.parametrize("fault", ["foreign_owner", "missing_owner", "missing_parent", "wrong_thread",
                                   "discord", "body", "update_other", "no_source", "root_card"])
def test_inherited_marker_never_authorizes_unproven_or_foreign_owner(world, monkeypatch, fault):
    monkeypatch.setattr(world.worker, "_ours", lambda envelope: True)
    spec, part = _post_spec("body_part" if fault == "body" else "attachment_part", marker="reply_key")
    spec["op"] = "update" if fault == "update_other" else "create"
    spec["delivery"].update(transport="discord" if fault == "discord" else "slack", team_id="T_SYNTHETIC")
    spec["parts"]["source_thread"] = fault != "no_source"
    part["prior_owner_message_id"] = "incorrect" if fault == "wrong_thread" else spec["delivery"]["thread_id"]
    if fault == "root_card":
        spec["card_key"] = "synthetic-root"
        spec["parts"]["post_source_id"] = 202
    _old_owner(world, spec, part, primary=None if fault == "missing_owner" else
               "9001" if fault in ("missing_parent", "update_other") else spec["delivery"]["thread_id"])
    if fault == "foreign_owner":
        records = world.worker._jview.refresh()
        owners = world.worker._part_owners(records)
        owners[PRIOR_PART].add(("foreign-delivery", "9001"))
        monkeypatch.setattr(world.worker, "_part_owners", lambda *_args: owners)
    result = _run(world, spec, part)
    assert result["result"] == "unknown" and result["error_code"] == "part_owner_ambiguous"
    assert world.calls == []


def test_authority_for_inherited_attachment_does_not_authorize_same_id_body(world, monkeypatch):
    monkeypatch.setattr(world.worker, "_ours", lambda envelope: True)
    spec, attachment = _post_spec("attachment_part", marker="reply_key")
    spec["op"] = "create"
    spec["delivery"].update(transport="slack", team_id="T_SYNTHETIC")
    spec["parts"]["source_thread"] = True
    attachment["prior_owner_message_id"] = spec["delivery"]["thread_id"]
    body = {"kind": "body_part", "index": 2, "part_id": "body:0002", "prior_remote_id": PRIOR_PART}
    spec["parts"]["manifest"].append(body)
    _old_owner(world, spec, attachment, primary=spec["delivery"]["thread_id"])
    _primary(world, spec, CURRENT_CARD)
    asyncio.run(world.worker._deliver_parts(_claim(spec), CURRENT_CARD))
    receipts = {r["part_id"]: r for r in _receipts(world.worker._dirs["cmd_int"])}
    assert receipts[attachment["part_id"]]["result"] == "delivered"
    assert receipts[body["part_id"]]["result"] == "unknown"
    assert receipts[body["part_id"]]["error_code"] == "part_owner_ambiguous"


def test_reply_update_allows_parent_and_same_reply_but_no_other_card(world, monkeypatch):
    monkeypatch.setattr(world.worker, "_ours", lambda envelope: True)
    spec, part = _post_spec("attachment_part", marker="reply_key")
    spec["delivery"].update(transport="slack", team_id="T_SYNTHETIC")
    spec["parts"]["source_thread"] = True
    part["prior_owner_message_id"] = spec["delivery"]["thread_id"]
    _old_owner(world, spec, part, primary=spec["delivery"]["thread_id"])
    owners = world.worker._part_owners(world.worker._jview.refresh())
    owners[PRIOR_PART].add(("same-reply-earlier-render", CURRENT_CARD))
    monkeypatch.setattr(world.worker, "_part_owners", lambda *_args: owners)
    assert _run(world, spec, part)["result"] == "delivered"


def test_marker_without_any_parent_receipt_never_authorizes_reuse(world, monkeypatch):
    monkeypatch.setattr(world.worker, "_ours", lambda envelope: True)
    spec, part = _post_spec("attachment_part", marker="reply_key")
    spec["op"] = "create"
    spec["delivery"].update(transport="slack", team_id="T_SYNTHETIC")
    spec["parts"]["source_thread"] = True
    part["prior_owner_message_id"] = spec["delivery"]["thread_id"]
    assert _run(world, spec, part)["error_code"] == "part_owner_ambiguous"
    assert world.calls == []
