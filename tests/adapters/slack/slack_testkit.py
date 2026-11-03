"""Shared synthetic fixtures for the Slack test family — the Slack
runner scope/config, a neutral v2 card spec, and the fake native client
plus worker wiring.  Not a test module (no ``test_`` prefix); sibling
files import it via the tests/ sys.path bootstrap."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import notify_spool
import notify_cmds as runner_cmds
from adapters.common import registry
from adapters.slack import paths as slack_paths
from adapters.slack.delivery import DeliveryWorker, SlackCardAdapter
from common_testkit import ledger_fixture
from notify_testkit import _dispatch, _intent, _latest_render, _seed_thread

SCOPE = {"transport": "slack", "profile": "synthetic-slack",
         "application_id": "A_SYNTHETIC", "team_id": "T_SYNTHETIC",
         "channel_id": "C_SYNTHETIC"}
SLACK = {"notify": {"interactive": "slack", "route_epoch": 1,
                    "card_thread": True,
                    "slack": {k: v for k, v in SCOPE.items() if k != "transport"}},
         "signals": {"notify": True}}


class FakeClient:
    def __init__(self, *, team="T_SYNTHETIC", retries=None, uploads=True):
        self.retry_handlers = [] if retries is None else retries
        self.team = team
        self.calls = []
        self.ephemeral_calls = []
        self.send_retry_handlers = []
        self.failure: Exception | None = None
        self.thread_failure: Exception | None = None
        # indexes into thread_posts that must fail exactly once
        self.thread_fail_at: set[int] = set()
        self.update_failure: Exception | None = None
        self.thread_posts = []
        self.replies: dict[str, list] = {}
        # upload edge — uploads=False models an SDK without
        # files_upload_v2 (instance attr so the single_attempt copy
        # inherits the capability exactly)
        self.upload_calls = []
        self.upload_fail_at: set[int] = set()
        self.upload_failure: Exception | None = None
        self._fid_n = 0
        self._files: dict = {}
        if uploads:
            self.files_upload_v2 = self._files_upload_v2
        self.bot_id = "B_SYNTHETIC"
        # one counter shared with the single_attempt() copies the worker
        # posts through — Slack ts values are unique per message
        self._ts_n = [1]

    def _next_ts(self):
        self._ts_n[0] += 1
        return f"1790000000.{self._ts_n[0]:06d}"

    async def auth_test(self):
        self.calls.append(("auth_test", {}))
        return {"ok": True, "team_id": self.team, "bot_id": self.bot_id,
                "user_id": "U_BOT"}

    async def chat_postMessage(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.send_retry_handlers.append(self.retry_handlers)
        if self.failure is not None:
            raise self.failure
        if "thread_ts" in kwargs:
            if len(self.thread_posts) in self.thread_fail_at:
                self.thread_fail_at.discard(len(self.thread_posts))
                raise self.thread_failure or TimeoutError("synthetic")
            ts = self._next_ts()
            self.thread_posts.append(kwargs)
            self.replies.setdefault(kwargs["thread_ts"], []).append(
                {"ts": ts, "text": kwargs["text"],
                 "bot_id": self.bot_id})
            return {"ok": True, "channel": kwargs["channel"], "ts": ts}
        return {"ok": True, "channel": kwargs["channel"],
                "ts": "1790000000.000001"}

    async def _files_upload_v2(self, **kwargs):
        self.calls.append(("files_upload_v2", kwargs))
        self.send_retry_handlers.append(self.retry_handlers)
        if len(self.upload_calls) in self.upload_fail_at:
            self.upload_fail_at.discard(len(self.upload_calls))
            raise self.upload_failure or TimeoutError("synthetic")
        self.upload_calls.append(kwargs)
        self._fid_n += 1
        blob = kwargs.get("file") or b""
        entry = {"id": f"F_SYNTHETIC_{self._fid_n:04d}", "user": "U_BOT",
                 "name": kwargs.get("filename"),
                 "size": len(blob),
                 "sha256": hashlib.sha256(blob).hexdigest(),
                 "mode": "hosted", "is_external": False, "editable": False}
        self._files[entry["id"]] = entry
        ts = self._next_ts()
        if kwargs.get("thread_ts"):
            self.replies.setdefault(kwargs["thread_ts"], []).append(
                {"ts": ts, "bot_id": self.bot_id, "files": [entry]})
        return {"ok": True, "files": [entry]}

    async def files_info(self, **kwargs):
        self.calls.append(("files_info", kwargs))
        f = self._files.get(kwargs.get("file"))
        return {"ok": f is not None, "file": f or {}}

    async def conversations_replies(self, **kwargs):
        self.calls.append(("replies", kwargs))
        root = kwargs["ts"]
        msgs = [{"ts": root, "text": "<card>", "bot_id": self.bot_id}]
        msgs += list(self.replies.get(root, []))
        return {"ok": True, "messages": msgs}

    async def chat_update(self, **kwargs):
        self.calls.append(("update", kwargs))
        if self.update_failure is not None:
            raise self.update_failure
        for msgs in self.replies.values():      # reply text follows the edit
            for m in msgs:
                if m["ts"] == kwargs["ts"] and "text" in kwargs:
                    m["text"] = kwargs["text"]
        return {"ok": True, "channel": kwargs.get("channel"), "ts": kwargs["ts"]}

    async def chat_delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):
        self.ephemeral_calls.append(kwargs)
        return {"ok": True}


def _mkworld(led, team=SCOPE["team_id"], channel=SCOPE["channel_id"],
             uploads=True):
    """Dispatch a slack render and wire the worker boundary."""
    root = Path(notify_spool.data_root(led))
    notify_spool.publish_flags(SLACK, str(root))
    dirs = slack_paths.ensure_dirs(str(root))
    reg = registry.Registry(dirs["state"], scope=SCOPE)
    client = FakeClient(team=team, uploads=uploads)
    sender = SlackCardAdapter(
        SimpleNamespace(client=client),
        team_id=SCOPE["team_id"], application_id=SCOPE["application_id"],
        channel_id=SCOPE["channel_id"], profile=SCOPE["profile"])
    worker = DeliveryWorker(
        sender=sender, settings=SCOPE, root=str(root),
        reg=reg, worker_id=registry.new_worker_id(),
        log=lambda *_args, **_kw: None)
    return SimpleNamespace(root=root, dirs=dirs, reg=reg,
                           client=client, sender=sender, worker=worker)


async def _granted_card(w, led, root):
    """tick -> grant -> tick — card delivered, parts driven."""
    assert await w._sender.bind()
    w.acquire_scope_lock()
    try:
        await w.tick()
        result = {"errors": []}
        runner_cmds.drain_int_commands(led, result, SLACK, str(root))
        await w.tick()
        runner_cmds.drain_int_commands(
            led, {"errors": []}, SLACK, str(root))
    finally:
        w.release_scope_lock()


# isolated ledger fixture ``led`` (data dir included) for the Slack family
isolated_slack_ledger = ledger_fixture(data_dir=True)


ROOT_TS = "1790000000.000001"


def _rewrite_world(led):
    """A delivered thread card holding its first body reply, plus the
    update spec a later render carries for that thread."""
    _seed_thread(led)
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    spec = {**json.loads(_latest_render(led)["spec_json"]), "op": "update"}
    return w, spec, w.client.replies[ROOT_TS]


def _updates(w):
    return [kw for kind, kw in w.client.calls if kind == "update"]


def legacy_attachment_parts(monkeypatch):
    """Model an older caption-in-thread plan without weakening current validators."""
    import notify_parts
    build = notify_parts.build_manifest

    def legacy_manifest(*args, **kwargs):
        parts, hold = build(*args, **kwargs)
        parts["manifest"] = [part for part in parts["manifest"] if part["kind"] != "file_group"]
        parts.get("slack", {}).pop("file_group", None)
        if not parts.get("slack"):
            parts.pop("slack", None)
        return parts, hold

    monkeypatch.setattr(notify_parts, "build_manifest", legacy_manifest)


def _attach(led, tmp_path, mid=100, name="syn.bin",
            blob=b"synthetic-bytes", state="downloaded"):
    """Seed a real on-disk file as a downloaded attachment on a shown
    message — the part manifest then seals path+sha256+bytes."""
    (tmp_path / "data" / "attachments").mkdir(parents=True, exist_ok=True)
    f = tmp_path / "data" / "attachments" / name
    f.write_bytes(blob)
    cur = led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "state) VALUES(?,?,?,?,?)",
        (mid, f"file-{name}", name, str(f), state))
    aid = cur.lastrowid
    if state == "downloaded":
        led.attachment_saved(aid, str(f), len(blob),
                             hashlib.sha256(blob).hexdigest())
    return aid, f, blob
