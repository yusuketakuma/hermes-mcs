"""Private file verification logs only fixed reasons and never writes remotely."""
import asyncio
from types import SimpleNamespace

import pytest

import notify_spool
from adapters.common import registry
from adapters.slack import paths
from adapters.slack.delivery import DeliveryWorker, SlackCardAdapter
from slack_testkit import FakeClient, SCOPE


def _world(tmp_path):
    client = FakeClient()
    sender = SlackCardAdapter(SimpleNamespace(client=client),
                              **{key: value for key, value in SCOPE.items() if key != "transport"})
    assert asyncio.run(sender.bind())
    client.calls.clear()
    notify_spool.ensure_dirs(str(tmp_path))
    dirs = paths.ensure_dirs(str(tmp_path))
    logs = []
    worker = DeliveryWorker(sender=sender, settings=SCOPE, root=str(tmp_path),
                            reg=registry.Registry(dirs["state"], scope=SCOPE),
                            worker_id=registry.new_worker_id(),
                            log=lambda event, **fields: logs.append((event, fields)))
    part = {"kind": "attachment_part", "part_id": "attach:0001", "edit_only": True,
            "prior_remote_id": "F_SYNTHETIC_PRIVATE_ID", "name": "SYNTHETIC_PRIVATE_NAME.pdf",
            "bytes": 12345, "sha256": "a" * 64}
    spec = {"op": "update", "delivery": dict(SCOPE),
            "parts": {"slack": {"file_group": True}, "manifest": [part]}}
    file_obj = {"id": part["prior_remote_id"], "user": "U_BOT", "name": part["name"],
                "size": part["bytes"], "sha256": part["sha256"], "mode": "hosted",
                "is_external": False, "editable": False}
    client._files[part["prior_remote_id"]] = file_obj
    return worker, client, spec, part, file_obj, logs


@pytest.mark.parametrize("fault,reason,error,reads", [
    ("scope", "edit_scope_unverified", "file_target_unverified", 0),
    ("manifest", "edit_scope_unverified", "file_target_unverified", 0),
    ("client", "edit_native_client_unverified", "sdk_capability_missing", 0),
    ("capability", "edit_native_client_unverified", "sdk_capability_missing", 0),
    ("exception", "file_info_unverified", "edit_target_unverified", 1),
    ("response", "file_info_unverified", "edit_target_unverified", 1),
    ("file_missing", "file_info_unverified", "edit_target_unverified", 1),
    ("file_type", "file_info_unverified", "edit_target_unverified", 1),
    ("id", "edit_reference_missing", "edit_file_unverified", 1),
    ("user", "edit_author_unverified", "edit_file_unverified", 1),
    ("bot_identity", "edit_author_unverified", "edit_file_unverified", 1),
    ("name", "edit_file_name_mismatch", "edit_file_unverified", 1),
    ("size", "edit_file_size_mismatch", "edit_file_unverified", 1),
    ("size_type", "edit_file_size_mismatch", "edit_file_unverified", 1),
    ("sha256", "edit_file_hash_mismatch", "edit_file_unverified", 1),
    ("mode", "edit_file_mode_unverified", "edit_file_unverified", 1),
    ("is_external", "edit_file_external_unverified", "edit_file_unverified", 1),
    ("editable", "edit_file_editability_unverified", "edit_file_unverified", 1),
])
def test_private_verification_rejections_keep_results_and_log_only_fixed_reason(tmp_path, fault, reason, error, reads):
    worker, client, spec, part, file_obj, logs = _world(tmp_path)
    if fault == "scope":
        spec["delivery"]["channel_id"] = "C_SYNTHETIC_OTHER"
    elif fault == "manifest":
        spec["parts"]["manifest"] = []
    elif fault == "client":
        worker._sender.single_attempt = lambda: None
    elif fault == "capability":
        client.files_info = None
    elif fault in ("exception", "response", "file_missing", "file_type"):
        async def info(**kwargs):
            client.calls.append(("files_info", kwargs))
            if fault == "exception":
                raise TimeoutError("SYNTHETIC_PRIVATE_EXCEPTION")
            if fault == "response":
                return {"ok": False, "file": file_obj, "error": "SYNTHETIC_PRIVATE_ERROR"}
            return {"ok": True, **({"file": [file_obj]} if fault == "file_type" else {})}
        client.files_info = info
    elif fault == "bot_identity":
        worker._sender._bot_user_id = ""
    elif fault == "size_type":
        file_obj["size"] = True
    else:
        if fault in ("mode", "is_external", "editable"):
            file_obj.pop("sha256")
        file_obj[fault] = {"id": "F_SYNTHETIC_OTHER", "user": "U_SYNTHETIC_OTHER",
                           "name": "SYNTHETIC_OTHER_NAME.pdf", "size": 67890, "sha256": "b" * 64,
                           "mode": "external", "is_external": True, "editable": True}[fault]
    result = asyncio.run(worker._attachment_part(spec, part, {}))
    assert result == {"result": "unknown", "error_code": error}
    assert logs == [("edit_preflight_held", {"reason": reason})]
    assert [kind for kind, _ in client.calls] == ["files_info"] * reads
    assert client.upload_calls == [] and client.thread_posts == []


@pytest.mark.parametrize("proof", ["hash", "metadata", "operator"])
def test_verified_private_file_keeps_its_existing_success_contract(tmp_path, proof):
    worker, client, spec, part, file_obj, logs = _world(tmp_path)
    if proof == "metadata":
        file_obj.pop("sha256")
    elif proof == "operator":
        part["operator_verified"] = True
        file_obj["user"] = "U_SYNTHETIC_OTHER"
    result = asyncio.run(worker._attachment_part(spec, part, {}))
    assert result == {"result": "delivered", "remote_id": part["prior_remote_id"],
                      "attachment_origin": "edit"}
    assert logs == []
    assert [kind for kind, _ in client.calls] == ["files_info"]
    assert client.upload_calls == [] and client.thread_posts == []


def test_slack_reencoded_media_size_difference_still_verifies(tmp_path):
    worker, client, spec, part, file_obj, logs = _world(tmp_path)
    file_obj.pop("sha256")
    file_obj["mimetype"] = "image/jpeg"
    file_obj["size"] = part["bytes"] - 4096
    result = asyncio.run(worker._attachment_part(spec, part, {}))
    assert result == {"result": "delivered", "remote_id": part["prior_remote_id"],
                      "attachment_origin": "edit"}
    assert logs == []
    assert client.upload_calls == [] and client.thread_posts == []


@pytest.mark.parametrize("mimetype", ["application/pdf", "text/plain", None])
def test_non_reencoded_size_difference_still_refuses(tmp_path, mimetype):
    worker, client, spec, part, file_obj, logs = _world(tmp_path)
    file_obj.pop("sha256")
    if mimetype is None:
        file_obj.pop("mimetype", None)
    else:
        file_obj["mimetype"] = mimetype
    file_obj["size"] = part["bytes"] - 4096
    result = asyncio.run(worker._attachment_part(spec, part, {}))
    assert result == {"result": "unknown", "error_code": "edit_file_unverified"}
    assert logs == [("edit_preflight_held", {"reason": "edit_file_size_mismatch"})]
    assert client.upload_calls == [] and client.thread_posts == []


def test_private_file_verification_cancellation_still_propagates(tmp_path):
    worker, client, spec, part, _, logs = _world(tmp_path)

    async def cancelled(**kwargs):
        client.calls.append(("files_info", kwargs))
        raise asyncio.CancelledError

    client.files_info = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._attachment_part(spec, part, {}))
    assert logs == []
    assert [kind for kind, _ in client.calls] == ["files_info"]
    assert client.upload_calls == [] and client.thread_posts == []
