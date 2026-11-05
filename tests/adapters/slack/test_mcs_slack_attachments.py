"""Synthetic mcs slack attachments contracts with isolated shared fixtures."""
import asyncio
import hashlib
import json
import pytest
from adapters.slack.delivery import DeliveryWorker
from adapters.common import registry
from notify_testkit import _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import ROOT_TS, SCOPE, SLACK, _attach, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as isolated_slack_ledger
from mcs_slack_testkit import _hand_ctx as _hand_ctx, _hand_spec as _hand_spec, _parts as _parts, _pin_wall_clock as _pin_wall_clock


def test_slack_attachment_uploads_inside_bound_thread(led, tmp_path):
    _seed_thread(led)
    ids = [_attach(led, tmp_path, name=f"syn-{i}.bin",
                  blob=f"synthetic-file-{i}".encode())
           for i in range(2)]
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    kinds = [p["kind"] for p in spec["parts"]["manifest"]]
    assert kinds.count("attachment_part") == 2
    asyncio.run(_granted_card(w.worker, led, w.root))
    # Each file is prepared privately; one update binds the complete set
    # to this card on the bound channel, with no separate file-share post.
    assert len(w.client.upload_calls) == 2
    assert all(u.get("channel") is None and u.get("thread_ts") is None
               for u in w.client.upload_calls)
    shared = [payload for operation, payload in w.client.calls
              if operation == "update" and "file_ids" in payload]
    assert len(shared) == 1
    assert shared[0]["channel"] == SCOPE["channel_id"]
    assert shared[0]["ts"] == "1790000000.000001"
    assert shared[0]["file_ids"] == [
        _parts(led, render["delivery_id"])[f"attach:{aid:04d}"]["remote_id"] for aid, _, _ in ids]
    group = _parts(led, render["delivery_id"])["file_group"]
    assert group["state"] == "delivered" and group["remote_id"] == shared[0]["ts"]
    assert not any(message.get("files") for rows in w.client.replies.values() for message in rows)
    by_name = {u["filename"]: u for u in w.client.upload_calls}
    for aid, f, blob in ids:
        u = by_name[f.name]
        assert u["file"] == blob          # the verified bytes went out
        parts = _parts(led, render["delivery_id"])
        row = parts[f"attach:{aid:04d}"]
        assert row["state"] == "delivered"
        assert row["remote_id"].startswith("F_SYNTHETIC")


def test_slack_attachment_corrupt_after_seal_is_not_sent(led, tmp_path):
    _seed_thread(led)
    aid, f, _blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    f.write_bytes(b"mutated-after-seal")        # post-seal mutation
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    assert not w.client.upload_calls            # never sent bad bytes
    row = _parts(led, _latest_render(led)["delivery_id"])[
        f"attach:{aid:04d}"]
    assert row["state"] == "not_sent"
    assert row["error_code"] == "attachment_mismatch"


def test_slack_attachment_sdk_capability_missing_is_held(led, tmp_path):
    _seed_thread(led)
    aid, _f, _blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    # an SDK without files_upload_v2 — the part is explicitly held,
    # never reported complete
    w = _mkworld(led, uploads=False)
    asyncio.run(_granted_card(w.worker, led, w.root))
    row = _parts(led, _latest_render(led)["delivery_id"])[
        f"attach:{aid:04d}"]
    assert row["state"] == "not_sent"
    assert row["error_code"] == "sdk_capability_missing"
    assert led.db.execute(
        "SELECT parts_state FROM notification_renders").fetchone()[0] \
        == "incomplete"


def test_slack_attachment_timeout_is_unknown_no_resend(led, tmp_path):
    _seed_thread(led)
    aid, _f, _blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    w.client.upload_fail_at = {0}
    w.client.upload_failure = TimeoutError("synthetic")
    asyncio.run(_granted_card(w.worker, led, w.root))
    attempts = [c for c in w.client.calls if c[0] == "files_upload_v2"]
    assert len(attempts) == 1
    did = _latest_render(led)["delivery_id"]
    row = _parts(led, did)[f"attach:{aid:04d}"]
    assert row["state"] == "unknown"
    spec = json.loads(_latest_render(led)["spec_json"])
    asyncio.run(DeliveryWorker(
        sender=w.sender, settings=SCOPE, root=str(w.root),
        reg=registry.Registry(w.dirs["state"], scope=SCOPE),
        worker_id=registry.new_worker_id(),
        log=lambda *_a, **_k: None)._resume_parts(spec))
    # started past the wire once — the ambiguous outcome is never
    # silently re-uploaded
    assert [c for c in w.client.calls
            if c[0] == "files_upload_v2"] == attempts


def test_slack_attachment_update_binds_remote_file(led, tmp_path, monkeypatch):
    from slack_testkit import legacy_attachment_parts
    legacy_attachment_parts(monkeypatch)
    _seed_thread(led)
    aid, f, blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    uploads = len(w.client.upload_calls)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    fid = w.client.upload_calls and \
        w.client.replies["1790000000.000001"][-1]["files"][0]["id"]
    receipt = led.db.execute(
        "SELECT state, remote_id FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
        (spec["delivery_id"], part["part_id"])).fetchone()
    assert receipt["state"] == "delivered" and receipt["remote_id"] == fid
    out = asyncio.run(w.worker._attachment_part(
        {**spec, "op": "update"}, {**part, "prior_remote_id": fid},
        {"thread_id": "1790000000.000001",
         "history": None, "consumed": set()}))
    # the same file already remote binds its real id — no re-upload
    assert out["result"] == "delivered" and out["remote_id"] == fid
    assert len(w.client.upload_calls) == uploads


@pytest.mark.parametrize("editable", [False, True])
def test_slack_sealed_prior_file_without_remote_hash_reuses_only_immutable_upload(
        led, tmp_path, editable, monkeypatch):
    from slack_testkit import legacy_attachment_parts
    legacy_attachment_parts(monkeypatch)
    _seed_thread(led)
    _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    uploads = len(w.client.upload_calls)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    remote = w.client.replies[ROOT_TS][-1]["files"][0]
    fid = remote["id"]
    remote.pop("sha256")  # Slack's documented file object has no sha256 promise
    remote.update(mode="hosted", is_external=False, editable=editable)
    # This id comes from an earlier delivered receipt for these sealed bytes.
    part["prior_remote_id"] = fid
    ctx = {"thread_id": ROOT_TS, "history": None, "consumed": set()}
    out = asyncio.run(w.worker._attachment_part({**spec, "op": "update"}, part, ctx))
    assert len(w.client.upload_calls) == uploads
    if editable:
        assert out == {"result": "unknown", "error_code": "file_target_unverified"}
    else:
        assert out["result"] == "delivered"
        assert out["remote_id"] == fid
        assert fid in ctx["consumed"]


@pytest.mark.parametrize("invalid_proof", ["foreign", "external", "hash_conflict", "no_receipt"])
def test_slack_prior_file_receipt_never_bypasses_remote_proof(led, tmp_path, invalid_proof, monkeypatch):
    from slack_testkit import legacy_attachment_parts
    legacy_attachment_parts(monkeypatch)
    _seed_thread(led)
    _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    uploads = len(w.client.upload_calls)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
    message = w.client.replies[ROOT_TS][-1]
    remote = message["files"][0]
    remote.pop("sha256")
    remote.update(mode="hosted", is_external=False, editable=False)
    part["prior_remote_id"] = remote["id"]
    if invalid_proof == "foreign":
        message["bot_id"] = "B_FOREIGN"
    elif invalid_proof == "external":
        remote["is_external"] = True
    elif invalid_proof == "hash_conflict":
        remote["sha256"] = "0" * 64
    else:
        part.pop("prior_remote_id")
    out = asyncio.run(w.worker._attachment_part({**spec, "op": "update"}, part,
        {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    if invalid_proof == "no_receipt":
        assert out["result"] == "delivered" and out["remote_id"] != remote["id"]
        assert len(w.client.upload_calls) == uploads + 1
    else:
        assert out == {"result": "unknown", "error_code": "file_target_unverified"}
        assert len(w.client.upload_calls) == uploads


def test_slack_attachment_foreign_file_never_binds(led, tmp_path):
    _seed_thread(led)
    aid, _f, blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    # a foreign user dropped a same-name/size/hash file in the thread —
    # it can never satisfy our part's remote verification
    w.client.replies["1790000000.000001"] = [
        {"ts": "1790000000.000099", "user": "U_FOREIGN",
         "files": [{"id": "F_FOREIGN", "name": "syn.bin",
                    "size": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest()}]}]
    out = asyncio.run(w.worker._attachment_part(
        {**spec, "op": "update"}, part,
        {"thread_id": "1790000000.000001",
         "history": None, "consumed": set()}))
    assert out["result"] == "delivered"
    assert out["remote_id"] != "F_FOREIGN"
    assert len(w.client.upload_calls) == 1


def test_slack_attachment_metadata_without_hash_is_not_delivery_proof(led, tmp_path):
    _seed_thread(led)
    _aid, _f, blob = _attach(led, tmp_path)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    assert asyncio.run(w.sender.bind())
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"]
                if p["kind"] == "attachment_part")
    w.client.replies["1790000000.000001"] = [{
        "ts": "1790000000.000099", "bot_id": w.client.bot_id,
        "files": [{"id": "F_UNVERIFIED", "name": part["name"], "size": len(blob)}]}]
    out = asyncio.run(w.worker._attachment_part(
        {**spec, "op": "update"}, part,
        {"thread_id": "1790000000.000001", "history": None, "consumed": set()}))
    assert out["result"] == "delivered" and out["remote_id"] != "F_UNVERIFIED"
    assert len(w.client.upload_calls) == 1


def test_slack_attachment_unavailable_stays_disclosed(led, tmp_path):
    _seed_thread(led)
    aid, _f, _blob = _attach(led, tmp_path, state="failed")
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    row = _parts(led, _latest_render(led)["delivery_id"])[
        f"attach:{aid:04d}"]
    # a terminally unavailable source file is never uploaded — its
    # runner caption is posted as plain text in the thread instead
    assert row["state"] == "delivered"
    assert not w.client.upload_calls
    caption = next(p for p in json.loads(
        _latest_render(led)["spec_json"])["parts"]["manifest"]
        if p["part_id"] == f"attach:{aid:04d}")["caption"]
    assert any(p["text"] == caption and p["mrkdwn"] is False
               for p in w.client.thread_posts)


def test_slack_attachment_caption_and_unavailable_caption(led, tmp_path):
    import hashlib
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    assert asyncio.run(w.sender.bind())
    blob = b"synthetic-bytes"
    (tmp_path / "data" / "attachments").mkdir(parents=True, exist_ok=True)
    f = tmp_path / "data" / "attachments" / "a.jpg"
    f.write_bytes(blob)
    part = {"part_id": "attach:0001", "kind": "attachment_part",
            "attachment_id": 1, "name": "a.jpg", "path": str(f),
            "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
            "caption": "📎 a.jpg — 合成様 10-01 09:40 <合成>"}
    out = asyncio.run(w.worker._attachment_part(_hand_spec(), part,
                                                _hand_ctx()))
    assert out["result"] == "delivered"
    assert w.client.upload_calls[0]["initial_comment"] == \
        "📎 a.jpg — 合成様 10-01 09:40 &lt;合成&gt;"
    gone = {"part_id": "attach:0002", "kind": "attachment_part",
            "attachment_id": 2, "name": "b.jpg", "unavailable": True,
            "caption": "📎 b.jpg — 取得失敗"}
    out = asyncio.run(w.worker._attachment_part(_hand_spec(), gone,
                                                _hand_ctx()))
    assert out["result"] == "delivered"
    assert [p["text"] for p in w.client.thread_posts] == ["📎 b.jpg — 取得失敗"]
    # already posted earlier — never a second caption post
    again = asyncio.run(w.worker._attachment_part(
        _hand_spec(), {**gone, "prior_remote_id": "1790000000.000042"},
        _hand_ctx()))
    assert again == {"result": "delivered",
                     "remote_id": "1790000000.000042"}
    assert len(w.client.thread_posts) == 1 and len(w.client.upload_calls) == 1
