"""T17 — the eight-IS recovery narrative: one ordered synthetic E2E.

Plan task-17 wires the REAL Python modules against temp SQLite, an
isolated HOME, SDK native-client fakes, fake Qwen HTTP (advertising
three slots), and a fake external consumer:

  ingest >40 clinical facts + attachment + late reply
  -> the single self-correcting inference path under the admission
     broker (primary extract -> Jev evaluation -> bounded secondary
     repair -> re-audit -> verdict -> PASS-only v4 publication)
  -> full canonical coverage resolved on every target
  -> Slack AND Discord cards, every thread body part, every file part
  -> a staged failed update and a restore to a PRE-SEND DB with a held
     external journal -> per-restore human consent -> reconcile ->
     resume with zero duplicate wire sends
  -> the read model and the governed external contract reading the
     SAME snapshot generation.

Recoverable failure is injected at each boundary (Jev transient,
mid-part worker crash, restore consent hold, lost journal). Synthetic
only: no real MCS, no real Jev, no platform API, no Keychain, no
launchctl, no patient DB, no deploy. This file collects without a
hermes checkout — it never imports gateway.* (see ../conftest.py).
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
from contextlib import suppress
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcs"))
import _mcs_path  # noqa: E402,F401  registers mcs/* subdirs as roots
for _d in ("adapters/discord", "adapters/slack", "notify", "semantic", "ops", "core"):
    sys.path.insert(0, str(ROOT / "tests" / _d))
sys.path.insert(0, str(ROOT))

import brain_export  # noqa: E402
import ext_contract as ext  # noqa: E402
import ledger as _ledger  # noqa: E402
import llm_admission as adm  # noqa: E402
import local_llm  # noqa: E402
import mcs_update  # noqa: E402
import notify_cards  # noqa: E402
import notify_cmds  # noqa: E402
import notify_reconcile  # noqa: E402
import notify_transport  # noqa: E402
import read_model  # noqa: E402
import semantic  # noqa: E402
import semantic_facts as sf  # noqa: E402
import semantic_v4 as v4  # noqa: E402

import discord_testkit as TD  # noqa: E402
import slack_testkit as TS  # noqa: E402
from notify_testkit import _dispatch, _intent, _latest_render  # noqa: E402
from ops_testkit import (_git, _make_repo, _mk_schema,  # noqa: E402
                         _seed_consent)
from semantic_testkit import (_canonical_cfg, _message, _PassJev,  # noqa: E402
                              _patient)
from slack_testkit import SLACK  # noqa: E402

NO_FACTS = {c: "none" for c in sf.MANDATORY_CATEGORIES}
NOW = 1_790_000_000.0


# ------------------------------------------------------------------
# discord module fake — same SDK shim the T11 harness installs
# ------------------------------------------------------------------

class _FakeFile:
    def __init__(self, path, filename=None):
        self.path, self.filename = path, filename


_td_mod = TD._fake_discord()
_td_mod.File = _FakeFile

_td_thread_init = TD.FakeThread.__init__


def _td_thread_init2(self, tid):
    _td_thread_init(self, tid)
    self.files = []
    self._next_id = 8000


async def _td_thread_send(self, content=None, **kw):
    f = kw.get("file")
    self._next_id += 1
    if f is not None:
        self.files.append({"path": f.path, "filename": f.filename})
    else:
        self.sent.append(content)
    return SimpleNamespace(id=self._next_id)


@pytest.fixture(autouse=True)
def isolated_narrative_state(monkeypatch):
    """Keep SDK fakes and updater path stubs local to each test."""
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    monkeypatch.setitem(sys.modules, "discord", _td_mod)
    monkeypatch.setattr(TD.FakeThread, "__init__", _td_thread_init2)
    monkeypatch.setattr(TD.FakeThread, "send", _td_thread_send)
    for name in ("REPO", "LEDGER", "DATA", "STATE_PATH", "UPDATE_LOCK",
                 "RUN_LOCK", "MARKER_PATH", "REPORT_PATH", "RESTORE_REPORT_PATH",
                 "BACKUP_DIR", "MANIFEST_PATH", "quiesce", "_services_reconcile",
                 "_reconcile_membership", "_postcheck", "load_config",
                 "_enqueue_notice", "restart_gateway", "restart_agents"):
        monkeypatch.setattr(mcs_update, name, getattr(mcs_update, name))


def _threads(bot):
    return [c for c in bot.channels.values()
            if isinstance(c, TD.FakeThread)]


# ------------------------------------------------------------------
# canned model + Jev
# ------------------------------------------------------------------

def _narrative_llm(n_facts=None):
    """Fake Qwen completion: v2 extraction emitting one fact per
    ``メドNN番`` token actually present in the prompt — chunk-split
    bodies therefore merge back to the full corpus with no duplicate
    fact_ids. The summarizer prompt gets an empty claims doc.
    ``n_facts`` (failure-path knob) drops indices >= the cap."""
    def llm(prompt):
        if "要約器" in prompt:
            return json.dumps({"claims": [], "limitations": []},
                              ensure_ascii=False)
        idxs = sorted({int(m) for m in
                       re.findall(r"メド(\d+)番", prompt)})
        if n_facts is not None:
            idxs = [i for i in idxs if i < n_facts]
        facts = [{"statement": f"メド{i:02d}番を投与",
                  "kind": "medication_event", "action": "continue",
                  "subject_role": "patient", "polarity": "affirmed",
                  "workflow_status": "performed", "importance": "T1",
                  "evidence_quote": f"メド{i:02d}番"}
                 for i in idxs]
        presence = dict(NO_FACTS)
        presence["medication"] = "one" if facts else "none"
        return json.dumps({"facts": facts,
                           "category_presence": presence},
                          ensure_ascii=False)
    return llm


class _NarrativeJev(_PassJev):
    """Per-chunk presence adjudication: the stock fake says
    ``has_medication=present`` unconditionally, which leaves
    ``jev_pre`` obligations open on chunks carrying no medication
    text (``present`` + no linked facts -> ``open``). The narrative
    body only mentions メド in chunks that actually contain it."""

    def evaluate(self, state, questions, deadline):
        out = super().evaluate(state, questions, deadline)
        text = ((state or {}).get("target") or {}).get("text") or ""
        ans = out.get("answers") or {}
        if "has_medication" in ans and "メド" not in text:
            ans["has_medication"]["choice"] = "absent"
        return out


class _RepairThenPassJev(_NarrativeJev):
    """First fact-support batch -> not_supported (forces S2 NEEDS_REVIEW
    -> S3 repair dispatch); every later batch passes — the bounded
    repair -> re-audit -> PASS arc inside one drain."""
    def __init__(self):
        super().__init__()
        self._fact_batches = 0

    def evaluate(self, state, questions, deadline):
        if any(k.startswith("fact_") for k in questions):
            self._fact_batches += 1
            if self._fact_batches == 1:
                self.requests_made += 1
                return {"answers": {
                    k: {"type": "choice", "choice": "not_supported",
                        "confidence": 0.9,
                        "probabilities": {"not_supported": 1.0}}
                    for k in questions}, "model": "jev-fake"}
        return super().evaluate(state, questions, deadline)


# ------------------------------------------------------------------
# narrative world
# ------------------------------------------------------------------

def _drain(db, jev, llm=None):
    cfg = _canonical_cfg()
    # the 40+-fact corpus spends far more than the stock 50-request
    # daily budget — the narrative's Jev is a fake, so scale the cap
    # to the corpus instead of truncating coverage mid-drain
    cfg["semantic"]["daily_request_budget"] = 100_000
    return semantic.run_due(
        db, cfg, {"errors": []},
        time.monotonic() + 300,
        jev_client=jev, llm_fn=llm or _narrative_llm())


def _repend_semantic(db):
    db.db.execute(
        "UPDATE fetch_jobs SET state='pending', next_try=0 "
        "WHERE kind='semantic'")
    db.db.commit()


def _attach(led, tmp_path, mid=1, fid="file-att-1", name="att-01.bin",
            blob=b"synthetic-attachment-bytes"):
    # the collector stores files in <data root>/attachments — delivery
    # workers refuse sealed paths outside it
    f = Path(notify_cards.data_root(led)) / "attachments" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(blob)
    cur = led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "state) VALUES(?,?,?,?,?)",
        (mid, fid, name, str(f), "downloaded"))
    led.attachment_saved(cur.lastrowid, str(f), len(blob),
                         hashlib.sha256(blob).hexdigest())


def _parts(led, did):
    return {r["part_id"]: dict(r) for r in led.db.execute(
        "SELECT * FROM notification_render_parts WHERE delivery_id=?",
        (did,))}


def _patch_updater(up, tmp_path):
    """The updater fixture's path rebinding, factored for both pytest
    and the evidence builder."""
    for name, path in (
            ("DATA", tmp_path / "data"),
            ("STATE_PATH", tmp_path / "data" / "update_state.json"),
            ("UPDATE_LOCK", tmp_path / "data" / "update.lock"),
            ("RUN_LOCK", tmp_path / "data" / "run.lock"),
            ("MARKER_PATH", tmp_path / "data" / "marker"),
            ("REPORT_PATH",
             tmp_path / "data" / "recovery_report.json"),
            ("RESTORE_REPORT_PATH",
             tmp_path / "data" / "restore_report.json"),
            ("BACKUP_DIR", tmp_path / "data" / "backups"),
            ("MANIFEST_PATH",
             tmp_path / "data" / "service_manifest.json")):
        setattr(up, name, str(path))


def _stub_updater_boundaries(up, restarts):
    up.quiesce = lambda: (up._write_marker(), [])[1]
    up._services_reconcile = lambda: None
    up._reconcile_membership = lambda m: []
    up._postcheck = lambda s, e: []
    up.load_config = lambda: {}
    up._enqueue_notice = lambda *a, **k: True
    up.restart_gateway = lambda c: None
    up.restart_agents = lambda: restarts.append(1) or []


class _Narrative:
    """Ordered shared state for the whole scenario."""

    def __init__(self, tmp_path):
        self.tmp = Path(tmp_path)
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.checks = []
        self.sends = []
        self.tokens = []

    def check(self, name, met, detail=None):
        self.checks.append(
            {"name": name, "met": bool(met), "detail": detail or {}})

    # -- stage helpers ------------------------------------------------

    def open_admission(self):
        """Broker at the SELECTED slot count (the backend advertises
        three — admission is capped by the chosen SLOT_COUNT, not by
        the wire advertisement) + fake Qwen HTTP transport."""
        self.adm_path = str(self.data / "adm.db")
        b = adm.Broker(self.adm_path, slots=local_llm.SLOT_COUNT)
        b.open_epoch(lambda: True)
        b.close()
        os.environ["MCS_LLM_ADMISSION"] = self.adm_path
        local_llm._BROKERS.clear()
        n = self

        def fake_transport(endpoint, method, body, timeout, deadline):
            if method == "GET":
                return 200, {}, json.dumps(
                    {"data": [{"slots": 3, "state": 0} for _ in
                              range(3)]}).encode()
            n.sends.append(body)
            n.tokens.append(body.get("admission_token"))
            text = _narrative_llm()(body["messages"][0]["content"])
            return 200, {}, json.dumps({"choices": [{
                "message": {"content": text},
                "finish_reason": "stop"}]}).encode()

        self._real_bounded = local_llm.bounded_request
        local_llm.bounded_request = fake_transport

    def close_admission(self):
        local_llm.bounded_request = self._real_bounded
        os.environ.pop("MCS_LLM_ADMISSION", None)
        for b in local_llm._BROKERS.values():
            b.close()
        local_llm._BROKERS.clear()

    def overlap(self):
        """Temporal overlap across the durable permit ledger — any RT
        permit whose occupancy window [admitted_at, terminal_at]
        intersects a BACKLOG permit's is a class-mixing violation.
        'waiting' permits hold no backend slot, so their wait period
        (created_at..admitted_at) is NOT occupancy."""
        con = sqlite3.connect(
            f"file:{self.adm_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT cls,admitted_at,terminal_at FROM permits "
                "WHERE admitted_at IS NOT NULL").fetchall()
        finally:
            con.close()
        INF = float("inf")
        wins = [(r[0], r[1], r[2] if r[2] is not None else INF)
                for r in rows]
        rt = [w for w in wins if w[0] == "RT"]
        bg = [w for w in wins if w[0] == "BACKLOG"]
        return any(a[1] < b[2] and b[1] < a[2] for a in rt for b in bg)

    def live_permits(self):
        con = sqlite3.connect(
            f"file:{self.adm_path}?mode=ro", uri=True)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM permits WHERE state NOT IN "
                "('terminal','fenced')").fetchone()[0]
        finally:
            con.close()


def _run_narrative(tmp_path):
    """The ordered scenario — returns the probe list."""
    N = _Narrative(tmp_path)
    led = _ledger.Ledger(str(N.data / "ledger.db"))
    N.open_admission()
    try:
        # ---- 1. ingest: parent w/ 41-fact body (>4 body parts) + attachment
        p = _patient(led)
        body_lines = [f"メド{i:02d}番を投与します。" for i in range(41)]
        body = ("\n".join(body_lines)
                + "\nSYNTHETIC-NARRATIVE-BODY "
                + "補足記載。" * 1400)          # ~10k chars -> >4 parts
        p.messages = [_message(1, body=body)]
        led.save_patient(p, notify={"source": "unread"}, semantic=True)
        _attach(led, N.tmp, mid=1)
        N.check("ingest_seeded", led.db.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0] == 1
            and led.db.execute(
                "SELECT COUNT(*) FROM attachments").fetchone()[0] == 1,
            {})

        # ---- 2. drain 1 — recoverable failure at the Jev boundary ----
        out1 = _drain(led, _PassJev(error=TimeoutError("synthetic")),
                      llm=semantic.llm_chat)
        v4rows1 = led.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind=?",
            (v4.KIND_V4,)).fetchone()[0]
        N.check("jev_transient_never_pass",
                out1["done"] == 0 and v4rows1 == 0,
                {"done": out1["done"], "failed": out1.get("failed")})

        # ---- 3. drain 2 — primary -> audit FAIL -> bounded repair
        #         -> re-audit PASS -> v4 published (late reply too)
        _repend_semantic(led)
        out2 = _drain(led, _RepairThenPassJev(), llm=semantic.llm_chat)
        fp1 = semantic.thread_bundle(led, 1, 1)["source_fingerprint"]
        stages = {s["stage"]: s["status"]
                  for s in v4.stage_ledger(led, 1, fp1)}
        v4row = led.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? "
            "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1",
            (v4.KIND_V4,)).fetchone()
        v4doc = json.loads(v4row["content"]) if v4row else {}
        v4meta = json.loads(v4row["meta"]) if v4row else {}
        nfacts = len(v4doc.get("canonical_facts") or [])
        N.check("v4_self_correcting_pass_over_40_facts",
                out2["done"] == 1 and nfacts > 40
                and v4meta.get("engine_version") == 4
                and stages.get("s3_repair") == "completed"
                and stages.get("s8_publish") == "PASS"
                and "s4_reaudit" in stages,
                {"facts": nfacts, "stages": stages,
                 "done": out2["done"], "failed": out2.get("failed")})
        N.check("admission_tokens_on_every_send",
                bool(N.sends) and all(N.tokens)
                and not N.overlap(),
                {"sends": len(N.sends), "tokens": all(N.tokens),
                 "overlap": N.overlap()})

        # the ledger-level overlap probe is vacuous without RT traffic
        # — exercise the boundary directly: an RT arrival while a
        # BACKLOG send occupies the backend must wait; new BACKLOG
        # stays closed until the RT retires
        brk = adm.Broker(N.adm_path, slots=local_llm.SLOT_COUNT)
        bg = brk.acquire("mcs.semantic", "BACKLOG")
        sent = brk.sent(bg["permit_id"], "narr-bg-send")
        rt_wait = brk.acquire("hermes.interactive", "RT")
        bg_blocked = brk.acquire("mcs.extract", "BACKLOG")
        brk.terminal(bg["permit_id"], "completed")
        rt_after = brk.poll(rt_wait["permit_id"])
        bg_during_rt = brk.acquire("mcs.extract", "BACKLOG")
        rt_sent = brk.sent(rt_wait["permit_id"], "narr-rt-send")
        brk.terminal(rt_wait["permit_id"], "completed")
        bg_free = brk.acquire("mcs.extract", "BACKLOG")
        if bg_free["admitted"]:
            brk.terminal(bg_free["permit_id"], "completed")
        brk.close()
        N.check("rt_arrival_waits_then_no_overlap",
                bg["admitted"] and sent["sent"]
                and not rt_wait["admitted"]
                and rt_wait["reason"] == "waiting"
                and not bg_blocked["admitted"]
                and bg_blocked["reason"] == "rt_pending"
                and rt_after.get("state") == "admitted"
                and not bg_during_rt["admitted"]
                and bg_during_rt["reason"] == "rt_pending"
                and rt_sent["sent"]
                and bg_free["admitted"]
                and not N.overlap(),
                {"rt_wait": rt_wait, "rt_after": rt_after,
                 "bg_blocked": bg_blocked.get("reason"),
                 "bg_during_rt": bg_during_rt.get("reason"),
                 "overlap": N.overlap()})

        # ---- 4. late reply -> second target coverage ----------------
        p2 = _patient(led)
        p2.messages = [_message(2, parent=1,
                                body="メド41番の効果を確認しました。")]
        led.save_patient(p2, semantic=True)
        out3 = _drain(led, _NarrativeJev(), llm=semantic.llm_chat)
        v4r2 = led.db.execute(
            "SELECT 1 FROM artifacts WHERE kind=? AND message_id=2 "
            "LIMIT 1", (v4.KIND_V4,)).fetchone()
        N.check("late_reply_resolves_own_v4_generation",
                v4r2 is not None and out3["done"] >= 1,
                {"done": out3["done"]})

        # ---- 5. Slack: card + ALL thread body parts + file part ----
        ev = _intent(led, payload={"message_ids": [1, 2]})
        assert _dispatch(led, ev, SLACK)["dispatched"]
        sw = TS._mkworld(led)
        asyncio.run(TS._granted_card(sw.worker, led, sw.root))
        render = _latest_render(led, card_id=1)
        did = render["delivery_id"]
        parts = _parts(led, did)
        kinds = {p_["kind"] for p_ in parts.values()}
        card_ts = "1790000000.000001"
        slack_ok = (
            render["parts_state"] == "complete"
            and kinds == {"card", "thread", "body_part",
                          "attachment_part"}
            and sum(1 for p_ in parts.values()
                    if p_["kind"] == "body_part") > 4
            and all(p_["state"] == "delivered" and p_["remote_id"]
                    for p_ in parts.values())
            and all(pp["thread_ts"] == card_ts
                    for pp in sw.client.thread_posts)
            and all(c["thread_ts"] == card_ts
                    for c in sw.client.upload_calls))
        N.check("slack_card_full_thread_file_parts", slack_ok, {
            "kinds": sorted(kinds),
            "body_parts": sum(1 for p_ in parts.values()
                              if p_["kind"] == "body_part"),
            "states": {k: v["state"] for k, v in parts.items()},
            "posts": len(sw.client.thread_posts),
            "uploads": len(sw.client.upload_calls)})

        # ---- 6. Discord: same content on the second platform --------
        ddata = Path(notify_cards.data_root(led))
        eid = led.outbox_add("new_messages", 1,
                             {"message_ids": [1, 2]})
        ev2 = dict(led.db.execute(
            "SELECT * FROM notify_outbox WHERE event_id=?",
            (eid,)).fetchone())
        assert notify_cards.dispatch_intent(led, ev2, TD.CFG,
                                            now=TD.NOW)["dispatched"]
        # the runner-published flag names the live transport — the
        # discord adapter claims its spec only when ITS transport is up
        notify_cards.publish_flags(TD.CFG, str(ddata))
        from hermes_plugin.mcs_delivery import registry as _reg_mod
        from hermes_plugin.mcs_discord import delivery as _discord_delivery
        reg = _reg_mod.Registry(str(ddata / "discord_state"))
        bot = TD.FakeBot()
        worker = _discord_delivery.DeliveryWorker(
            bot=bot, settings=TD.SETTINGS, root=str(ddata), reg=reg,
            worker_id=_reg_mod.new_worker_id(),
            log=lambda *_a, **_k: None)

        async def _ddeliver(w):
            for _ in range(3):
                await w.tick()
                notify_cmds.drain_int_commands(led, {}, TD.CFG,
                                               str(ddata))

        asyncio.run(_ddeliver(worker))
        drender = dict(led.db.execute(
            "SELECT * FROM notification_renders ORDER BY render_rev "
            "DESC LIMIT 1").fetchone())
        dparts = _parts(led, drender["delivery_id"])
        thread_posts = [s for t in _threads(bot) for s in t.sent]
        thread_files = [f for t in _threads(bot) for f in t.files]
        dcard = dict(led.db.execute(
            "SELECT * FROM notification_cards WHERE transport='discord'"
        ).fetchone())
        N.check("discord_card_full_thread_file_parts",
                dcard["delivery_state"] == "delivered"
                and drender["parts_state"] == "complete"
                and all(pp["state"] == "delivered"
                        for pp in dparts.values())
                and thread_posts and thread_files
                and dcard["thread_id"],
                {"posts": len(thread_posts),
                 "files": len(thread_files),
                 "states": {k: v["state"] for k, v in dparts.items()}})

        # ---- 7. read model + governed external export, same gen ----
        led.close()
        snap = _ledger.publish_snapshot(str(N.data / "ledger.db"),
                                        str(N.tmp / "snaps"))
        led = _ledger.Ledger(str(N.data / "ledger.db"))
        sdb = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        sdb.row_factory = sqlite3.Row
        try:
            rm = read_model.read_model(sdb)
            gen = rm["snapshot"]["generation_id"]
            recs = rm["records"]
            mrec = next(r for r in recs
                        if r.get("message_id") == 1)
        finally:
            sdb.close()
        exp_dir = N.tmp / "exp"
        bres = brain_export.run(exp_dir, Path(snap))
        lines = [json.loads(ln) for ln in
                 (exp_dir / "export.jsonl").read_text(
                     encoding="utf-8").splitlines() if ln]
        gen_at = next(r["snapshot"]["generated_at"] for r in lines
                      if r["type"] == "meta")
        auth = ext.load_authorization(
            _auth(N.tmp, fields=list(ext.RECORD_TYPES)))
        envelope = ext.build_envelope(lines, auth, gen_at)
        sink = ext.LocalSink(N.tmp / "sink")
        sink.drop_ack = True          # recoverable failure: lost ack
        exporter = ext.GovernedExporter(N.tmp / "exp-state")
        res_a = exporter.deliver(envelope, sink, auth_path=N.tmp
                               / "auth.json")
        res_b = exporter.deliver(envelope, sink, auth_path=N.tmp / "auth.json")
        sink.drop_ack = False
        sink.receive(envelope)        # the late ack lands
        res_c = exporter.reconcile(envelope["envelope_id"], sink)
        N.check("read_model_and_external_contract_same_generation",
                gen == bres["snapshot_generation_id"]
                == envelope["snapshot_generation_id"]
                and mrec["extraction"]["semantic_facts_v4"][
                    "state"] == "current"
                and mrec["extraction"]["semantic_facts_v4"][
                    "engine_version"] == 4
                and res_a["status"] == "held"
                and res_b["status"] == "held"
                and res_c["status"] == "acked",
                {"generation": gen,
                 "v4_state": mrec["extraction"].get(
                     "semantic_facts_v4", {}).get("state"),
                 "deliver": [res_a["status"], res_b["status"],
                             res_c["status"]]})

        # ---- 8. staged update -> failed -> schema-bump restore ------
        # a second slack delivery round: dispatch, then backup the DB
        # PRE-SEND (render+parts exist, no attempts, no wire posts) —
        # the restore below rolls back to exactly this state while the
        # disk journal outlives it.
        ev3 = _intent(led, payload={"message_ids": [1, 2]})
        assert _dispatch(led, ev3, SLACK)["dispatched"]
        led.db.commit()
        led.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup = N.data / "backups" / "pre-send.db"
        backup.parent.mkdir(exist_ok=True)
        led.close()
        shutil.copy2(N.data / "ledger.db", backup)
        led = _ledger.Ledger(str(N.data / "ledger.db"))
        sw2 = TS._mkworld(led)

        async def _crash():
            assert await sw2.sender.bind()
            sw2.worker.acquire_scope_lock()
            try:
                await sw2.worker.tick()
                notify_cmds.drain_int_commands(led, {"errors": []},
                                               SLACK, str(sw2.root))
                orig = sw2.worker._perform_part

                async def exploding(claim, part, ctx):
                    if part["part_id"].startswith("body:"):
                        raise asyncio.CancelledError()
                    return await orig(claim, part, ctx)

                sw2.worker._perform_part = exploding
                with suppress(asyncio.CancelledError):
                    await sw2.worker.tick()
            finally:
                sw2.worker.release_scope_lock()

        asyncio.run(_crash())
        posts_at_crash = len(sw2.client.thread_posts)

        # the staged update migrated the live schema forward — the
        # restore below must roll back one schema step (live==backup
        # would skip the per-restore consent gate entirely)
        ver = led.db.execute("PRAGMA user_version").fetchone()[0]
        led.db.execute(f"PRAGMA user_version={ver + 1}")
        led.db.commit()

        # the staged update applies a tag carrying a schema_bump entry
        up_dir = N.tmp / "upd"
        up_dir.mkdir()
        (up_dir / "data").mkdir()
        _patch_updater(mcs_update, up_dir)
        repo, _bare = _make_repo(up_dir)
        mcs_update.REPO = str(repo)
        restarts = []
        _stub_updater_boundaries(mcs_update, restarts)
        # restore machinery points at the NARRATIVE db + data dir so
        # the consent marker holds the real delivery boundary
        mcs_update.LEDGER = str(N.data / "ledger.db")
        mcs_update.DATA = str(N.data)
        mcs_update.MARKER_PATH = str(N.data / "marker")
        mcs_update.RESTORE_REPORT_PATH = str(
            N.data / "restore_report.json")
        before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
        after = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
        state = mcs_update._default_state()
        state["applied"] = [{"tag": "v1.1.0", "sha": after,
                             "prev_sha": before, "schema_bump": True,
                             "backup_path": str(backup),
                             "at": time.time()}]
        mcs_update.save_state(state)
        live_bytes = (N.data / "ledger.db").read_bytes()
        rc = mcs_update.rollback("cid-rb")
        # consent hold: no receipt -> no swap, marker up, grant denied
        grant_denied = notify_transport.apply_transport_begin(
            led, {"version": 1, "op": "transport_begin",
                  "command_id": "f" * 8 + "-0000-4000-8000-"
                  + "f" * 12,
                  "attempt_id": "a" * 16, "worker_id": "b" * 8,
                  "delivery_id": "held-probe", "render_rev": 1,
                  "payload_hash": "h", "route_epoch": 1,
                  **TS.SCOPE}, SLACK, now=NOW)
        N.check("restore_held_without_receipt",
                rc == 2
                and (N.data / "ledger.db").read_bytes() == live_bytes
                and notify_cards.restore_pending(str(N.data)) is
                not None
                and grant_denied.get("granted") is False
                and grant_denied.get("error")
                == "denied_restore_pending",
                {"rc": rc, "grant": grant_denied})

        # ---- 9. consent -> swap -> reconcile -> resume no dup ------
        _seed_consent(str(N.data / "ledger.db"), str(backup))
        # Production quiescence closes every writer before replacing the DB.
        led.close()
        assert mcs_update.recover_interrupted() == 0
        led = _ledger.Ledger(str(N.data / "ledger.db"))
        rec = notify_reconcile.reconcile_after_restore(led, SLACK)
        N.check("reconcile_after_restore_settles_journal",
                notify_cards.restore_pending(str(N.data)) is None
                and rec.get("verdicts") is not None,
                {"verdicts": [v.get("verdict") for v in
                              rec.get("verdicts") or []],
                 "held": len(rec.get("held") or [])})
        sw3 = TS._mkworld(led)

        async def _resume():
            assert await sw3.sender.bind()
            sw3.worker.acquire_scope_lock()
            try:
                await sw3.worker.reconcile()
                for _ in range(3):
                    await sw3.worker.tick()
                    notify_cmds.drain_int_commands(
                        led, {"errors": []}, SLACK, str(sw3.root))
            finally:
                sw3.worker.release_scope_lock()

        asyncio.run(_resume())
        # body chunks legitimately share text (the corpus is a repeated
        # padding string) — "no duplicates" is a per-PART property:
        # every resumed post must map to exactly one distinct part, and
        # the part whose send outcome was never proven (worker_crash)
        # must stay 'unknown' — held for reconcile, never re-sent.
        r3 = led.db.execute(
            "SELECT * FROM notification_renders WHERE transport='slack' "
            "ORDER BY card_id DESC, render_rev DESC LIMIT 1").fetchone()
        parts3 = _parts(led, r3["delivery_id"])
        body3 = [p for p in parts3.values() if p["kind"] == "body_part"]
        delivered3 = [p for p in body3 if p["state"] == "delivered"]
        unknown3 = [p for p in body3 if p["state"] == "unknown"]
        posts3 = sw3.client.thread_posts
        N.check("resume_after_restore_no_duplicates",
                len(posts3) + posts_at_crash == len(delivered3)
                and len(unknown3) == 1
                and unknown3[0]["error_code"] == "worker_crash"
                and all(p["remote_id"] for p in delivered3)
                and r3["parts_state"] == "incomplete",
                {"posts_before": posts_at_crash,
                 "posts_resumed": len(posts3),
                 "delivered": len(delivered3),
                 "unknown": [p["part_id"] for p in unknown3],
                 "parts_state": r3["parts_state"]})
        N.check("no_permit_leaks", N.live_permits() == 0
                and not N.overlap(),
                {"live": N.live_permits()})
    finally:
        with suppress(Exception):
            led.close()
        N.close_admission()
    return N


def _auth(tmp_path, **over):
    auth = {"contract": ext.AUTH_CONTRACT, "auth_id": "auth-narr-1",
            "purpose": "synthetic review mirror", "actor": "pharm-synth",
            "destination": "fake-knowledge-store",
            "scope": "aggregate", "patients": "all",
            "fields": list(ext.RECORD_TYPES),
            "expires_at": time.time() + 3600, "max_snapshot_age_s": 600,
            "retention_days": 30, "revoked": False,
            "confirm_human": True, "reason": "synthetic narrative",
            "created_at": time.time()}
    auth.update(over)
    p = tmp_path / "auth.json"
    p.write_text(json.dumps(auth), encoding="utf-8")
    return p


# ------------------------------------------------------------------
# the test
# ------------------------------------------------------------------

def test_mcs_recovery_narrative(tmp_path):
    res = _run_narrative(tmp_path)
    failed = [c["name"] for c in res.checks if not c["met"]]
    assert not failed, f"unmet narrative probes: {failed} " \
        f"-> {[c for c in res.checks if not c['met']]}"


# ------------------------------------------------------------------
# failure injections — each must surface failed/held, never complete
# ------------------------------------------------------------------

def test_failure_fact_dropped_means_not_all_facts(tmp_path):
    """fact 41 removed: the verifier must see fewer verified facts
    than the corpus demands — never a silent 'complete'."""
    tmp = Path(tmp_path)
    (tmp / "data").mkdir()
    led = _ledger.Ledger(str(tmp / "data" / "ledger.db"))
    p = _patient(led)
    p.messages = [_message(1, body="\n".join(
        f"メド{i:02d}番を投与します。" for i in range(41)))]
    led.save_patient(p, semantic=True)
    try:
        out = _drain(led, _NarrativeJev(), llm=_narrative_llm(n_facts=40))
        row = led.db.execute(
            "SELECT content FROM artifacts WHERE kind=? "
            "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1",
            (v4.KIND_V4,)).fetchone()
        nfacts = len(json.loads(row["content"])[
                         "canonical_facts"]) if row else 0
        assert out["done"] == 1
        assert nfacts == 40, nfacts    # corpus demanded 41 — honest gap
    finally:
        led.close()


def test_failure_attachment_absent_no_file_part(tmp_path):
    """attachment 2 removed: no attachment_part may be claimed —
    the render manifest drops it rather than inventing delivery."""
    tmp = Path(tmp_path)
    (tmp / "data").mkdir()
    led = _ledger.Ledger(str(tmp / "data" / "ledger.db"))
    try:
        led.db.execute(
            "INSERT INTO patients(project_id,patient_name,"
            "is_archived) VALUES(1,'患者A',0)")
        for m in (10, 11):
            led.db.execute(
                "INSERT INTO messages(message_id,project_id,"
                "sender_name,posted_at,posted_at_ts,body_text,"
                "content_hash,body_state,parent_id) VALUES"
                "(?,?,?,?,?,?,?,?,?)",
                (m, 1, "職員", f"2026-09-24T08:{m:02d}",
                 1790000000 + m, "本文", f"{m:064x}", "full",
                 10 if m != 10 else None))
        led.db.commit()
        ev = _intent(led, payload={"message_ids": [10, 11]})
        assert _dispatch(led, ev, SLACK)["dispatched"]
        render = _latest_render(led, card_id=1)
        spec = json.loads(render["spec_json"])
        kinds = {p_["kind"] for p_ in spec["parts"]["manifest"]}
        assert "attachment_part" not in kinds
    finally:
        led.close()


def test_failure_lost_journal_holds_not_completes(tmp_path):
    """Journal deleted after a granted/unknown attempt: reconcile's
    reverse scan holds the delivery — a missing journal never
    proves a send."""
    tmp = Path(tmp_path)
    data = tmp / "data"
    data.mkdir()
    led = _ledger.Ledger(str(data / "ledger.db"))
    try:
        led.db.execute(
            "INSERT INTO patients(project_id,patient_name,"
            "is_archived) VALUES(1,'患者A',0)")
        led.db.execute(
            "INSERT INTO messages(message_id,project_id,sender_name,"
            "posted_at,posted_at_ts,body_text,content_hash,"
            "body_state,parent_id) VALUES(10,1,'職員','t',1,'b',"
            "'h','full',NULL)")
        led.db.commit()
        # a granted-or-unknown attempt row with NO journal on disk
        led.db.execute(
            "INSERT INTO notification_renders(delivery_id,op,"
            "render_rev,route_epoch,payload_hash,correlation,"
            "state,created_at,updated_at) VALUES('d1','create',1,"
            "1,'h','corr-1','sending',1,1)")
        led.db.execute(
            "INSERT INTO notification_delivery_attempts("
            "attempt_id,delivery_id,begin_command_id,state,"
            "created_at) VALUES('a1','d1','c1','unknown',1)")
        led.db.commit()
        notify_cards.mark_restored(str(data), backup_path="b")
        res = notify_reconcile.reconcile_after_restore(led, SLACK)
        verdicts = [v["verdict"] for v in res.get("verdicts") or []]
        assert res.get("held") or "held" in verdicts \
            or res.get("events_held", 0) > 0, res
        assert all(v != "complete" for v in verdicts)
    finally:
        led.close()


def test_failure_missing_consent_receipt_stays_held(tmp_path):
    """Restore human receipt deleted: the schema-bump rollback keeps
    the consent hold — the live DB is never swapped unapproved."""
    tmp = Path(tmp_path)
    data = tmp / "data"
    data.mkdir()
    _patch_updater(mcs_update, tmp)
    repo, _bare = _make_repo(tmp)
    mcs_update.REPO = str(repo)
    restarts = []
    _stub_updater_boundaries(mcs_update, restarts)
    live = data / "ledger.db"
    back = data / "backups" / "pre.db"
    back.parent.mkdir(exist_ok=True)
    _mk_schema(back, 7, messages=2)
    _mk_schema(live, 8, messages=5)
    mcs_update.LEDGER = str(live)
    before = _git(repo, "rev-parse", "v1.0.0").stdout.strip()
    after = _git(repo, "rev-list", "-n1", "v1.1.0").stdout.strip()
    state = mcs_update._default_state()
    state["applied"] = [{"tag": "v1.1.0", "sha": after,
                         "prev_sha": before, "schema_bump": True,
                         "backup_path": str(back), "at": time.time()}]
    mcs_update.save_state(state)
    live_bytes = live.read_bytes()
    assert mcs_update.rollback("cid-rb") == 2
    # no receipt seeded — recover must stay held
    mcs_update.recover_interrupted()
    marker = notify_cards.restore_pending(str(data))
    assert live.read_bytes() == live_bytes      # never swapped
    assert marker is not None
    assert marker.get("phase") == "awaiting_consent"
    assert restarts == []


def test_failure_eval_label_drop_means_diagnostic_not_pass(tmp_path):
    """Evaluation label dropped: Jev answers reject -> NEEDS_REVIEW
    diagnostic only, never a PASS v4 row."""
    from semantic_testkit import _AuditFailJev
    tmp = Path(tmp_path)
    (tmp / "data").mkdir()
    led = _ledger.Ledger(str(tmp / "data" / "ledger.db"))
    p = _patient(led)
    p.messages = [_message(1, body="メド00番を投与します。")]
    led.save_patient(p, semantic=True)
    try:
        _drain(led, _AuditFailJev(
            choice_map={f"has_{c}": "absent"
                        for c in sf.MANDATORY_CATEGORIES}
            | {"has_medication": "present"}),
            llm=_narrative_llm(n_facts=3))
        assert not led.db.execute(
            "SELECT 1 FROM artifacts WHERE kind=? AND message_id=1",
            (v4.KIND_V4,)).fetchone()
        assert led.db.execute(
            "SELECT 1 FROM artifacts WHERE kind=? AND message_id=1",
            (v4.KIND_V4_DIAG,)).fetchone()
    finally:
        led.close()
