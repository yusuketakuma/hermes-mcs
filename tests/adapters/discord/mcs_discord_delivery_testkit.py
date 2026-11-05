"""Synthetic mcs discord delivery testkit contracts with isolated shared fixtures."""
from __future__ import annotations
import asyncio
import hashlib
import json
import types
from adapters.common import envelopes, journal, paths
from discord_delivery_testkit import DELIVERY_ID, FakeThread, _mkworker, _receipts, _sent_parts, _spec, _state
from discord_delivery_testkit import synthetic_discord_sdk as synthetic_discord_sdk


def _journal_card_delivered(state_dir, message_id="9001"):
    """The card attempt's durable result — the gate _resume_parts reads
    before it may attach any dependent part."""
    journal.append(str(state_dir), "w0",
                   {"phase": "result", "attempt_id": "ab" * 8,
                    "delivery_id": DELIVERY_ID,
                    "result": "delivered", "message_id": message_id})


def _card_delivered(tmp_path, message_id="9001"):
    _journal_card_delivered(_state(tmp_path), message_id)


# ---------- in-place rewrite of a changed chunk ------------------------------

def _prior_thread(tmp_path, old_texts):
    """A companion thread the create render already filled: one bot post
    per old chunk text, oldest first."""
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    for t in old_texts:
        asyncio.run(th.send(t))
    return w, bot, th


def _update_spec(chunks, th, prior_idx):
    """An update spec whose chunk i names thread post prior_idx[i]."""
    prior = {f"body:{i + 1:04d}": str(th.sent[j].id)
             for i, j in prior_idx.items()}
    return _spec(chunks, op="update", thread_id="7700", prior=prior)


def _attach_update(tmp_path, prior):
    """An update render whose attachment part names the message that
    already carries the same file (``prior_remote_id``)."""
    blob = b"synthetic-attachment"
    (tmp_path / "attachments").mkdir(exist_ok=True)
    f = tmp_path / "attachments" / "att.bin"
    f.write_bytes(blob)
    att = {"part_id": "attach:0007", "kind": "attachment_part",
           "index": 0, "attachment_id": 7, "name": "att.bin",
           "path": str(f), "sha256": hashlib.sha256(blob).hexdigest(),
           "bytes": len(blob)}
    if prior is not None:
        att["prior_remote_id"] = prior
    w, reg, bot = _mkworker(tmp_path)
    th = FakeThread(7700)
    bot.channels[42].threads.append(th)
    spec = _spec([], op="update", thread_id="7700")
    att["index"] = len(spec["parts"]["manifest"])
    spec["parts"]["manifest"].append(att)
    return w, th, spec


# ---------- journal growth / scan bounds (U04-F05) --------------------------

def _sealed(spec):
    """The runner seals the card part hash; the synthetic spec must too
    before the tick's validate() will accept it."""
    parts = spec["parts"]
    parts["manifest"][0]["sha256"] = hashlib.sha256(envelopes.canonical(
        {k: parts[k] for k in ("containers", "footer", "action_rows")}
    )).hexdigest()
    return spec


def _publish_spec(tmp_path, spec):
    (tmp_path / "discord_render" / f"{spec['delivery_id']}.json").write_text(
        json.dumps(spec))


def bot_sent_parts(tmp_path):
    return _sent_parts(_state(tmp_path))


def _backup(tmp_path, mtime):
    import os
    (tmp_path / "backups").mkdir(exist_ok=True)
    path = tmp_path / "backups" / "ledger-20260101.db"
    path.write_bytes(b"")
    os.utime(path, (mtime, mtime))


REVOKED_ID = "00000000-0000-4000-8000-00000000dead"


def _card_spec(delivery_id, kind="signal"):
    """A legacy card with no manifest — the tick sends only the card."""
    return {"schema": "mcs-card-render/v1",
            "delivery_id": delivery_id,
            "logical_intent_id": f"v1|{kind}|1|sig-1",
            "card_key": f"v1|{kind}|1|sig-1", "kind": kind, "op": "create",
            "render_rev": 1, "source_generation": 1,
            "presentation_generation": 1, "ui_revision": 1,
            "delivery": {"route_epoch": 1, "correlation": "cd" * 16,
                         "profile": "mcs", "application_id": "1",
                         "channel_id": "42", "guild_id": "7",
                         "intent_event_ids": [1]},
            "parts": {"containers": [{"type": "text",
                                      "text": f"{kind} card"}]}}


def _write_begin_result(tmp_path, begin, *, granted, error=None):
    """The runner's begin-result shape, written where the worker reads it."""
    body = {"granted": granted, "command_id": begin["command_id"],
            "attempt_id": begin["attempt_id"],
            "delivery_id": begin["delivery_id"],
            "attempt_state": "granted" if granted else "not_sent",
            "worker_id": begin["worker_id"],
            "render_rev": begin["render_rev"],
            "payload_hash": begin["payload_hash"],
            "route_epoch": begin["route_epoch"]}
    if error:
        body["error"] = error
    name = paths.safe_name(begin["command_id"]) + ".json"
    (tmp_path / "cmd_results" / name).write_text(json.dumps(body))


def _spec_path(tmp_path, delivery_id):
    return tmp_path / "discord_render" / f"{delivery_id}.json"


def _journal_rows(tmp_path):
    rec = journal.scan(str(_state(tmp_path)))
    return [r for rows in rec.values() for r in rows]


def _begins(tmp_path, delivery_id=DELIVERY_ID):
    return [e for e in _receipts(tmp_path / "cmd_int")
            if e.get("op") == "transport_begin"
            and e.get("delivery_id") == delivery_id]


def _clock(monkeypatch, start=1_000_000.0):
    from adapters.common import worker as worker_mod
    now = [start]
    monkeypatch.setattr(worker_mod, "time",
                        types.SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]))
    return now


# ---------- incremental journal view for part dedupe ------------------------

def _did(i):
    return f"00000000-0000-4000-8000-{i:012x}"


def _same_as_full_scan(view, state):
    full = journal.scan(str(state))
    assert list(view) == list(full)
    assert {aid: view[aid] for aid in view} == full


def _count_journal_reads(monkeypatch):
    """Byte counts of every read the journal module makes — a full
    scan's line iteration and ScanCache's reads alike."""
    import builtins
    got = []

    class Counted:
        def __init__(self, handle):
            self._h = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._h.close()

        def __getattr__(self, name):
            return getattr(self._h, name)

        def __iter__(self):
            for line in self._h:
                got.append(len(line))
                yield line

        def read(self, *a):
            data = self._h.read(*a)
            got.append(len(data))
            return data

    def counted_open(path, mode="r", *a, **kw):
        handle = builtins.open(path, mode, *a, **kw)
        return Counted(handle) if mode == "rb" else handle

    monkeypatch.setattr(journal, "open", counted_open, raising=False)
    return got


# ---------- replaced button contexts ----------------------------------------

def _token_world(tmp_path, monkeypatch):
    """An update spec whose claim-time tokens (t=2000) follow the old
    render's (t=1000) for the same card, beside another card's token
    and a result-issued task_status token."""
    from adapters.common import registry as registry_mod
    from adapters.common import spec as spec_mod
    clock = [1000.0]
    monkeypatch.setattr(registry_mod, "time", types.SimpleNamespace(
        time=lambda: clock[0]))
    w, reg, _ = _mkworker(tmp_path)

    def update(card_key, **buttons):
        spec = _spec([], manifest=[], op="update")
        spec["card_key"] = card_key
        spec["delivery"]["message_id"] = "9001"
        spec["parts"]["action_rows"] = [
            [{"id": a, "token": t} for a, t in buttons.items()]]
        return spec

    card = "v1|thread|1|100"
    reg.put_tokens(spec_mod.token_map(
        update(card, ack="old-ack", next="old-next")))
    reg.put_tokens(spec_mod.token_map(
        update("v1|thread|1|999", ack="other-ack")))
    reg.put_tokens({"task-tok": {"action": "task_status", "card_key": card,
                                 "kind": "thread", "project_id": 1}})
    clock[0] = 2000.0
    spec = update(card, ack="new-ack", next="new-next")
    reg.put_tokens(spec_mod.token_map(spec))       # the claim-time put
    return w, reg, spec, clock, update


def _part_ctx(thread):
    return {"thread": thread, "thread_id": str(thread.id),
            "history": None, "consumed": set()}
