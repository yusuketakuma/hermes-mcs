"""Explicit edit/broadcast part capabilities reject unproven targets and old workers."""
import copy
import pytest
from adapters.common import spec
from adapters.common.envelopes import payload_hash
from discord_delivery_testkit import _spec


def planned():
    value = _spec(["placeholder"], op="update", thread_id="7700", prior={"body:0001": "6001"})
    value["delivery"]["message_id"] = "9001"
    visible = {key: value["parts"][key] for key in ("containers", "footer", "action_rows")}
    value["parts"]["manifest"][0]["sha256"] = payload_hash(visible)
    value["parts"]["manifest"][2]["edit_only"] = True
    return value


def test_attachment_edit_capability_requires_existing_caption():
    value = planned()
    part = {"part_id": "attach:0001", "kind": "attachment_part", "index": 3,
            "attachment_id": 1, "name": "synthetic.pdf", "path": "attachments/1.pdf", "bytes": 10,
            "sha256": "a" * 64, "prior_remote_id": "6002", "caption": "metadata", "edit_only": True}
    value["parts"]["manifest"].append(part)
    spec.validate(value)
    part.pop("caption")
    with pytest.raises(ValueError, match="bad_edit_only"):
        spec.validate(value)


def test_edit_only_discord_default_and_old_worker_keys(monkeypatch):
    value = planned()
    assert spec.validate(value) is value
    monkeypatch.setattr(spec, "PART_ENTRY_KEYS", spec.PART_ENTRY_KEYS - {"edit_only"})
    with pytest.raises(ValueError, match="unsupported_part_key"):
        spec.validate(value)


@pytest.mark.parametrize("fault", ["bool", "null", "string", "prior", "create", "lineworks", "thread", "card"])
def test_edit_only_rejects_unsealed_or_unsupported_targets(fault):
    value = copy.deepcopy(planned())
    part = value["parts"]["manifest"][2]
    if fault in ("bool", "null", "string"):
        part["edit_only"] = {"bool": 1, "null": None, "string": "true"}[fault]
    elif fault == "prior":
        part.pop("prior_remote_id")
    elif fault == "create":
        value["op"] = "create"
    elif fault == "lineworks":
        value["delivery"]["transport"] = "lineworks"
    elif fault == "thread":
        value["delivery"].pop("thread_id")
    else:
        value["parts"]["manifest"][0]["edit_only"] = True
    with pytest.raises(ValueError, match="bad_edit_only"):
        spec.validate(value)


def broadcast_plan():
    value = planned()
    value["delivery"]["transport"] = "slack"
    part = value["parts"]["manifest"][2]
    part.pop("edit_only")
    part.pop("prior_remote_id")
    part.update(name="m:100:header#1", broadcast=True)
    return value


def test_broadcast_capability_is_explicit_and_old_workers_hold(monkeypatch):
    value = broadcast_plan()
    assert spec.validate(value) is value
    value["parts"]["manifest"][2]["broadcast"] = False
    value["delivery"]["intent_event_ids"] = []
    assert spec.validate(value) is value
    monkeypatch.setattr(spec, "PART_ENTRY_KEYS", spec.PART_ENTRY_KEYS - {"broadcast"})
    with pytest.raises(ValueError, match="unsupported_part_key"):
        spec.validate(value)
    value["parts"]["manifest"][2].pop("broadcast")
    assert spec.validate(value) is value  # old queued payloads remain valid


@pytest.mark.parametrize("fault", ["int", "null", "string", "transport", "card", "digest",
                                    "create", "intent", "prior", "edit_only"])
def test_broadcast_rejects_unsealed_or_unsupported_targets(fault):
    value = broadcast_plan()
    part = value["parts"]["manifest"][2]
    if fault in ("int", "null", "string"):
        part["broadcast"] = {"int": 1, "null": None, "string": "true"}[fault]
    elif fault == "transport":
        value["delivery"]["transport"] = "discord"
    elif fault == "card":
        part.pop("broadcast")
        value["parts"]["manifest"][0]["broadcast"] = True
    elif fault == "digest":
        value["kind"] = "digest"
    elif fault == "create":
        value["op"] = "create"
    elif fault == "intent":
        value["delivery"]["intent_event_ids"] = []
    elif fault == "prior":
        part["prior_remote_id"] = "6001"
    else:
        part.update(edit_only=True, prior_remote_id="6001")
    with pytest.raises(ValueError, match="bad_broadcast"):
        spec.validate(value)
