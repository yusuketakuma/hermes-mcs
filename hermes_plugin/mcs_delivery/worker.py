"""Transport-neutral delivery worker — claim -> transport_begin ->
grant -> send -> transport_receipt, journaled at every phase.

Ordering contract (plan §4/§5): a claim marker is not a send grant;
the grant is durable runner-side before HTTP begins; ``started`` is
fsync'd before the request fires; ``result`` is fsync'd before the
receipt is published. Crashes land between those records and are
classified honestly on the next start — pre-HTTP grants settle as
``not_sent`` (journal integrity + the scope lock prove it), post-HTTP
silences stay ``unknown`` for an operator to resolve, and an in-flight
send is never retried by a new worker.

The transport's own wire calls live behind ``_perform`` and
``_maybe_thread``; everything around them — claims, grants, journals,
receipts, retries — is shared here. All filesystem work happens via
``asyncio.to_thread``; the event loop never blocks on the spec/result
poll.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from typing import Any

from . import envelopes, journal, paths, registry
from . import spec as spec_mod
from contextlib import suppress

POLL_S = 2.0
REPUBLISH_BEGIN_S = 60.0       # re-send the same begin if no result
RETRY_IN_FLIGHT_S = 15.0       # denied_in_flight re-begin backoff
MAX_BEGIN_RETRIES = 20         # ~5min of in_flight before giving up
CLAIM_STALE_S = 60.0           # orphan .claimed marker age before reclaim

# HTTP statuses that prove the send was rejected outright — never
# committed server-side. Timeouts/cancellations are unknown instead.
NOT_SENT_STATUS = frozenset({400, 401, 403, 404, 405, 410})


def err_code(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"http_{status}"
    return type(exc).__name__.lower()


def is_definitive_reject(exc: BaseException) -> bool:
    """A 4xx status is an explicit answer — the send never committed.
    Timeouts, disconnects and cancellations cannot prove that."""
    status = getattr(exc, "status", None)
    return isinstance(status, int) and status in NOT_SENT_STATUS


async def _outcome_of(awaitable):
    """Attempt outcome for a past-the-wire call: CancelledError stays
    unknown-by-omission (re-raised), a definitive reject is not_sent,
    every other failure is unknown — journaled fact, never a resend."""
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if is_definitive_reject(exc):
            return {"result": "not_sent", "error_code": err_code(exc)}
        return {"result": "unknown", "error_code": err_code(exc)}


def _card_message_id(records: dict, delivery_id: str) -> str | None:
    """Latest factual card result for a delivery — dependent parts may
    only attach to a card the journal proves was posted."""
    res = [r for rows in records.values() for r in rows
           if r.get("phase") == "result"
           and r.get("delivery_id") == delivery_id
           and not r.get("part_id")]
    res.sort(key=lambda r: r.get("ts") or 0)
    last = res[-1] if res else None
    if last and last.get("result") == "delivered" \
            and last.get("message_id"):
        return str(last["message_id"])
    return None


class DeliveryWorker:
    """The durable claim/grant/send/receipt loop; a transport subclass
    supplies ``_perform`` and any companion-message bookkeeping."""

    transport = "discord"   # runner default; transports override

    def __init__(self, *, bot: Any, settings: dict,
                 root: str, reg: registry.Registry,
                 worker_id: str, log) -> None:
        self._bot = bot
        self._settings = settings
        self._root = root
        self._dirs = paths.notify_dirs(root)
        self._reg = reg
        self._worker_id = worker_id
        self._log = log
        self._lock_fd = None
        self._stopping = False

    # -- scope lock --------------------------------------------------

    def scope(self) -> dict:
        d = {"profile": self._settings.get("profile"),
             "application_id": self._settings.get("application_id"),
             "channel_id": self._settings.get("channel_id")}
        if self._settings.get("guild_id"):
            d["guild_id"] = self._settings["guild_id"]
        return d

    def acquire_scope_lock(self) -> bool:
        """fcntl lock keyed on the delivery scope — one live sender per
        scope, process-wide. Held for the worker's lifetime; released
        only when all tasks have stopped."""
        self._ensure_dirs()
        path = os.path.join(self._dirs["state"],
                            f"send-{registry.scope_key(self.scope())}.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def _ensure_dirs(self) -> None:
        paths.ensure_dirs(self._root)

    def release_scope_lock(self) -> None:
        if self._lock_fd is not None:
            with suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # -- journal helpers ----------------------------------------------

    def _journal(self, phase: str, **fields) -> None:
        journal.append(self._dirs["state"], self._worker_id,
                       {"phase": phase, **fields})

    # -- startup reconcile --------------------------------------------

    async def reconcile(self) -> dict:
        """Settle orphaned attempts before any new claim runs.

        - unreported results  -> republish the recorded receipt
        - pre-HTTP attempts   -> not_sent (journal integrity + held
          scope lock + dead worker prove the send never began)
        - post-HTTP attempts  -> unknown receipt; never resend
        """
        records = await asyncio.to_thread(journal.scan,
                                          self._dirs["state"])
        stats = {"receipt_republished": 0, "not_sent": 0, "unknown": 0}
        for aid, info in journal.unreported(records).items():
            row = info["record"]
            claim = self._reg.claims().get(row.get("delivery_id"))
            env = self._receipt_env(aid, info["rows"], claim)
            if env is None or not self._ours(env):
                continue
            env["result"] = row.get("result", "unknown")
            if row.get("message_id"):
                env["message_id"] = str(row["message_id"])
            if row.get("error_code"):
                env["error_code"] = row["error_code"]
            await asyncio.to_thread(
                envelopes.publish_command, self._dirs["cmd_int"], env)
            self._journal("receipt", attempt_id=aid,
                          delivery_id=row.get("delivery_id"),
                          result=env["result"], reconcile=True)
            stats["receipt_republished"] += 1
            await self._retire_reconciled(aid, env, claim)
        for aid, info in journal.unfinished(records).items():
            row = info["record"]
            claim = self._reg.claims().get(row.get("delivery_id"))
            env = self._receipt_env(aid, info["rows"], claim)
            if env is None or not self._ours(env):
                continue
            if info["phase"] == "pre_http":
                env["result"] = "not_sent"
                env["error_code"] = "worker_restart"
                stats["not_sent"] += 1
            else:
                env["result"] = "unknown"
                env["error_code"] = "worker_crash"
                stats["unknown"] += 1
            await asyncio.to_thread(
                envelopes.publish_command, self._dirs["cmd_int"], env)
            self._journal("receipt", attempt_id=aid,
                          delivery_id=row.get("delivery_id"),
                          result=env["result"], reconcile=True)
            await self._retire_reconciled(aid, env, claim)
        # A crash after receipt publication but before the registry flush
        # leaves an old granted claim. Its journal still forbids resending.
        for aid, rows in records.items():
            if not any(r.get("phase") == "receipt" for r in rows):
                continue
            claim = self._reg.claimed(rows[-1].get("delivery_id"))
            env = self._receipt_env(aid, rows, claim)
            if env is not None and self._ours(env):
                await self._retire_reconciled(aid, env, claim)
        return stats

    async def _retire_reconciled(self, aid, env, claim) -> None:
        if claim is not None and claim.get("attempt_id") == aid:
            await self._drop_claim(claim)
        elif claim is None and not self._reg.is_dead(env["delivery_id"]) \
                and os.path.isfile(os.path.join(
                    self._dirs["render"], env["delivery_id"] + ".json")):
            self._reg.mark_dead(env["delivery_id"])

    def _receipt_env(self, attempt_id, rows, claim) -> dict | None:
        """Rebuild a receipt envelope from claim state or the envelope
        the journal recorded at begin — never from memory alone."""
        import uuid
        env = next((r["receipt_envelope"] for r in reversed(rows)
                    if isinstance(r.get("receipt_envelope"), dict)),
                   None)
        if env:
            env = dict(env)
            env["command_id"] = str(uuid.uuid4())
            env["attempt_id"] = attempt_id
            return env
        if claim is not None and claim.get("attempt_id") == attempt_id:
            return envelopes.transport_receipt(claim, "unknown")
        return None

    # -- claim scan ----------------------------------------------------

    def _spec_files(self) -> list:
        try:
            names = sorted(n for n in os.listdir(self._dirs["render"])
                           if n.endswith(".json"))
        except OSError:
            return []
        return [os.path.join(self._dirs["render"], n) for n in names]

    @staticmethod
    def _claim_path(spec_path: str) -> str:
        return spec_path + ".claimed"

    def _ours(self, delivery: dict) -> bool:
        """Only claim specs addressed to this worker's exact scope."""
        mine = self.scope()
        for k in ("profile", "application_id", "channel_id"):
            if str(delivery.get(k) or "") != str(mine.get(k) or ""):
                return False
        return not (mine.get("guild_id") and str(delivery.get("guild_id") or "") != str(mine["guild_id"]))

    async def _claim_spec(self, spec_path: str, spec: dict) -> None:
        """Write the claim marker, journal it, register the claim and
        publish the transport_begin — in that order."""
        attempt_id = registry.new_attempt_id()
        claim = {"attempt_id": attempt_id, "worker_id": self._worker_id,
                 "spec": spec, "payload_hash": envelopes.payload_hash(spec),
                 "spec_path": spec_path,
                 "phase": "begin_sent", "begin_at": time.time(),
                 "begin_retries": 0}
        marker = {"worker_id": self._worker_id,
                  "attempt_id": attempt_id, "at": time.time()}
        raw = envelopes.canonical(marker)
        await asyncio.to_thread(
            self._write_marker, self._claim_path(spec_path), raw)
        self._journal("claimed", attempt_id=attempt_id,
                      delivery_id=spec["delivery_id"],
                      render_rev=spec["render_rev"],
                      op=spec["op"], worker_id=self._worker_id)
        # button token context from the spec itself — works before the
        # next snapshot carries the fresh tokens
        self._reg.put_tokens(spec_mod.token_map(spec))
        self._reg.claim(spec["delivery_id"], claim)
        await self._publish_begin(claim)

    @staticmethod
    def _write_marker(path: str, raw: bytes) -> None:
        paths.atomic_write(path, raw, tmp_prefix=".claim-",
                           dir_fsync=False)

    async def _publish_begin(self, claim: dict) -> None:
        # republish re-sends the SAME envelope/command_id — the runner
        # answers a replayed begin_command_id with the existing attempt
        # (idempotent), while a new command_id for the same attempt_id
        # would be an attempt_id_conflict
        env = claim.get("begin_env") or envelopes.transport_begin(claim)
        claim["begin_env"] = env
        claim["begin_cid"] = env["command_id"]
        claim["begin_at"] = time.time()
        await asyncio.to_thread(
            envelopes.publish_command, self._dirs["cmd_int"], env)
        self._journal("begin", attempt_id=claim["attempt_id"],
                      delivery_id=claim["spec"]["delivery_id"],
                      command_id=env["command_id"],
                      begin_envelope=env,
                      receipt_envelope=envelopes.transport_receipt(
                          claim, "unknown"))
        self._reg.claim(claim["spec"]["delivery_id"], claim)

    # -- result file ----------------------------------------------------

    def _read_begin_result(self, claim: dict) -> dict | None:
        cid = claim.get("begin_cid")
        if not cid:
            return None
        return paths.read_result(self._dirs["cmd_results"], cid)

    def _verify_grant(self, claim: dict, result: dict) -> bool:
        """The grant is only ours if every echoed identity field matches
        the claim — a grant for a different attempt/spec must not send."""
        spec = claim["spec"]
        if result.get("attempt_id") != claim["attempt_id"]:
            return False
        if result.get("worker_id") != self._worker_id:
            return False
        if str(result.get("delivery_id")) != str(spec["delivery_id"]):
            return False
        if result.get("render_rev") != spec["render_rev"]:
            return False
        if result.get("payload_hash") != claim["payload_hash"]:
            return False
        return result.get("route_epoch") == spec["delivery"]["route_epoch"]

    # -- transport edges (subclasses override) --------------------------

    async def _perform(self, claim: dict) -> dict:
        """One HTTP attempt. Returns {result, message_id?, error_code?}.
        Only a definitive rejection maps to not_sent; anything that can
        have committed is unknown (§4)."""
        raise NotImplementedError

    async def _maybe_thread(self, claim: dict, message_id: str) -> None:
        """Companion-message bookkeeping after a delivered send —
        transports with no second post-deliver call leave it a no-op."""
        return

    # -- durable render parts (T7) -------------------------------------

    async def _deliver_parts(self, claim: dict, message_id: str) -> None:
        """Dependent-part delivery after a settled card send — the
        sealed manifest's thread/body/attachment parts each journal
        their own started/result/receipt. A spec without a manifest
        falls back to the legacy companion-thread path."""
        spec = claim["spec"]
        manifest = (spec.get("parts") or {}).get("manifest")
        if not manifest:
            await self._maybe_thread(claim, message_id)
            return
        ctx = {"card_message_id": message_id, "thread": None,
               "thread_id": spec["delivery"].get("thread_id"),
               "history": None, "consumed": set()}
        records = await asyncio.to_thread(journal.scan, self._dirs["state"])
        await self._drive_parts(claim, manifest, ctx, records)

    async def _resume_parts(self, spec: dict,
                            records: dict | None = None) -> None:
        """Restart-resume a settled spec's dependent parts — only parts
        the journal proves never began; a started-only attempt stays
        honestly unknown and is never resent."""
        manifest = (spec.get("parts") or {}).get("manifest")
        if not manifest or len(manifest) < 2:
            return
        if records is None:
            records = await asyncio.to_thread(
                journal.scan, self._dirs["state"])
        mid = _card_message_id(records, spec["delivery_id"])
        if not mid:
            return            # card unproven — nothing to attach to
        claim = {"attempt_id": "resume", "worker_id": self._worker_id,
                 "spec": spec,
                 "payload_hash": envelopes.payload_hash(spec),
                 "spec_path": None, "phase": "settled"}
        ctx = {"card_message_id": mid, "thread": None,
               "thread_id": spec["delivery"].get("thread_id"),
               "history": None, "consumed": set()}
        await self._drive_parts(claim, manifest, ctx, records)

    async def _drive_parts(self, claim: dict, manifest: list,
                           ctx: dict, records: dict) -> None:
        """Walk the manifest in declared order; every unjournaled part
        gets exactly one attempt, journaled phases dedupe the rest.
        After a complete pass all parts are final, so the delivery is
        marked done — the registry flag bounds resume scans to one
        journal read per pending spec per tick."""
        spec = claim["spec"]
        for part in manifest:
            kind = part.get("kind")
            if kind == "card" or part.get("unavailable"):
                continue              # card mirrors the primary attempt;
                                      # unavailable was disclosed at issue
            aid = envelopes.part_attempt_id(spec["delivery_id"],
                                            part["part_id"])
            rows = records.get(aid, [])
            phases = {r.get("phase") for r in rows}
            if "receipt" not in phases and "result" in phases:
                await self._republish_part_receipt(claim, part, rows)
            if phases & {"result", "receipt", "denied"}:
                if kind == "thread":
                    res = next((r for r in reversed(rows)
                                if r.get("phase") == "result"), None)
                    ctx["thread_id"] = str(res["remote_id"]) \
                        if res and res.get("result") == "delivered" \
                        and res.get("remote_id") else None
                continue
            if "started" in phases:
                continue              # unknown — reconcile reports it
            if kind != "thread" and not ctx.get("thread_id"):
                continue              # held — dependents need a thread
            await self._attempt_part(claim, part, ctx)
        self._reg.put_parts_done(spec["delivery_id"])

    async def _republish_part_receipt(self, claim: dict, part: dict,
                                      rows: list) -> None:
        """A journaled result whose receipt never published gets its
        recorded envelope replayed — the stored truth, never a guess."""
        import uuid
        res = next((r for r in reversed(rows)
                    if r.get("phase") == "result"), None)
        env = res.get("receipt_envelope") if res else None
        if not isinstance(env, dict):
            return
        env = dict(env)
        env["command_id"] = str(uuid.uuid4())
        try:
            await asyncio.to_thread(
                envelopes.publish_command, self._dirs["cmd_int"], env)
        except OSError:
            return                        # next resume retries
        self._journal("receipt",
                      attempt_id=envelopes.part_attempt_id(
                          claim["spec"]["delivery_id"], part["part_id"]),
                      delivery_id=claim["spec"]["delivery_id"],
                      part_id=part["part_id"], kind=part["kind"],
                      result=env["result"], republish=True)

    async def _attempt_part(self, claim: dict, part: dict,
                            ctx: dict) -> None:
        """One journaled attempt for one part — the same ordering
        contract as the card send: started before HTTP, result before
        receipt, crash-anywhere classifiable."""
        spec = claim["spec"]
        aid = envelopes.part_attempt_id(spec["delivery_id"],
                                        part["part_id"])
        self._journal("started", attempt_id=aid,
                      delivery_id=spec["delivery_id"],
                      part_id=part["part_id"], kind=part["kind"],
                      receipt_envelope=envelopes.part_receipt(
                          claim, part, "unknown"))
        outcome = await _outcome_of(
            self._perform_part(claim, part, ctx))
        # past the wire — journal the true outcome before any fallible
        # publish so a crash replays fact, never a resend
        env = envelopes.part_receipt(
            claim, part, outcome["result"],
            remote_id=outcome.get("remote_id"),
            error_code=outcome.get("error_code"))
        self._journal("result", attempt_id=aid,
                      delivery_id=spec["delivery_id"],
                      part_id=part["part_id"], kind=part["kind"],
                      result=outcome["result"],
                      remote_id=outcome.get("remote_id"),
                      error_code=outcome.get("error_code"),
                      receipt_envelope=env)
        await asyncio.to_thread(
            envelopes.publish_command, self._dirs["cmd_int"], env)
        self._journal("receipt", attempt_id=aid,
                      delivery_id=spec["delivery_id"],
                      part_id=part["part_id"], kind=part["kind"],
                      result=outcome["result"])
        if part["kind"] == "thread":
            # the card's thread binding also rides the legacy
            # thread_receipt envelope — best-effort, because the
            # journaled part_receipt already carries the truth
            with suppress(OSError):
                env2 = envelopes.thread_receipt(
                    spec["delivery_id"], ctx["card_message_id"],
                    thread_id=outcome.get("remote_id")
                    if outcome["result"] == "delivered" else None,
                    error_code=outcome.get("error_code")
                    if outcome["result"] != "delivered" else None)
                await asyncio.to_thread(
                    envelopes.publish_command,
                    self._dirs["cmd_int"], env2)
            if outcome["result"] == "delivered" \
                    and outcome.get("remote_id"):
                ctx["thread_id"] = str(outcome["remote_id"])
                ctx["thread"] = outcome.get("thread")
            else:
                ctx["thread"] = None
                ctx["thread_id"] = None     # dependents hold, not retry

    async def _perform_part(self, claim: dict, part: dict,
                            ctx: dict) -> dict:
        """One part's wire call. Returns
        {result, remote_id?, error_code?}; definitive rejects map to
        not_sent exactly like the card attempt."""
        raise NotImplementedError

    # -- the per-claim step ----------------------------------------------

    async def _step_begin(self, claim: dict) -> None:
        """begin_sent phase: read the grant decision, retry or settle,
        and on grant fall through so the send happens in the same tick."""
        spec = claim["spec"]
        now = time.time()
        if now < claim.get("retry_at", 0):
            return
        result = await asyncio.to_thread(
            self._read_begin_result, claim)
        if result is None:
            if now - claim.get("begin_at", now) > REPUBLISH_BEGIN_S \
                    and claim.get("begin_retries", 0) \
                    < MAX_BEGIN_RETRIES:
                claim["begin_retries"] += 1
                await self._publish_begin(claim)   # same command_id
            return
        if not result.get("granted"):
            self._journal("denied",
                          attempt_id=claim["attempt_id"],
                          delivery_id=spec["delivery_id"],
                          error=result.get("error"))
            error = str(result.get("error") or "")
            if error == "denied_in_flight" \
                    and claim["begin_retries"] < MAX_BEGIN_RETRIES:
                # another attempt owns the card — a re-begin needs a
                # fresh attempt_id AND a fresh envelope (the stored
                # begin_env still carries the dead attempt_id and
                # re-sending it just replays the same denial)
                claim["begin_retries"] += 1
                claim["attempt_id"] = registry.new_attempt_id()
                claim["begin_env"] = None
                claim["begin_cid"] = None
                claim["begin_at"] = 0
                claim["retry_at"] = now + RETRY_IN_FLIGHT_S
                self._reg.claim(spec["delivery_id"], claim)
                return
            # transient denials — the queued render stays legitimate,
            # so no dead tombstone: the flags check above already stops
            # the re-claim churn, and the spec must be claimable again
            # once the window lifts (interactive_off kill switch,
            # restore_pending gate)
            await self._drop_claim(
                claim, dead=error not in (
                    "denied_interactive_off",
                    "denied_restore_pending"))
            return
        if not self._verify_grant(claim, result):
            self._journal("denied",
                          attempt_id=claim["attempt_id"],
                          delivery_id=spec["delivery_id"],
                          error="grant_mismatch")
            await self._drop_claim(claim)
            return
        self._journal("granted",
                      attempt_id=claim["attempt_id"],
                      delivery_id=spec["delivery_id"],
                      correlation=spec["delivery"]["correlation"])
        claim["phase"] = "granted"
        self._reg.claim(spec["delivery_id"], claim)
        # fall through — the send happens in the same tick

    async def _step_claim(self, claim: dict) -> None:
        spec = claim["spec"]
        if claim["phase"] == "begin_sent":
            await self._step_begin(claim)
        if claim["phase"] == "granted":
            # fsync'd BEFORE the HTTP request — the single line that
            # separates provable-not-sent from honest-unknown on crash
            self._journal("started",
                          attempt_id=claim["attempt_id"],
                          delivery_id=spec["delivery_id"],
                          correlation=spec["delivery"]["correlation"])
            claim["phase"] = "started"
            outcome = await _outcome_of(self._perform(claim))
            # Move past HTTP before any fallible journal/receipt I/O. A
            # retry settles this outcome; it must never call the transport
            # again.
            claim["outcome"] = outcome
            claim["phase"] = "result"
        if claim["phase"] == "started":
            claim["outcome"] = {"result": "unknown",
                                "error_code": "worker_crash"}
            claim["phase"] = "result"
        if claim["phase"] == "result":
            outcome = claim["outcome"]
            self._journal("result",
                          attempt_id=claim["attempt_id"],
                          delivery_id=spec["delivery_id"],
                          result=outcome["result"],
                          message_id=outcome.get("message_id"),
                          error_code=outcome.get("error_code"))
            env = envelopes.transport_receipt(
                claim, outcome["result"],
                message_id=outcome.get("message_id"),
                error_code=outcome.get("error_code"))
            await asyncio.to_thread(
                envelopes.publish_command, self._dirs["cmd_int"], env)
            self._journal("receipt",
                          attempt_id=claim["attempt_id"],
                          delivery_id=spec["delivery_id"],
                          result=outcome["result"])
            claim["phase"] = "settled"
            mid = outcome.get("message_id")
            if outcome["result"] == "delivered" and mid:
                await self._deliver_parts(claim, str(mid))
        if claim["phase"] == "settled":
            await self._drop_claim(claim)

    async def _drop_claim(self, claim: dict, dead: bool = True) -> None:
        spec = claim["spec"]
        with suppress(OSError):
            await asyncio.to_thread(
                os.unlink, self._claim_path(claim["spec_path"]))
        if dead:
            self._reg.drop_claim(spec["delivery_id"])
        else:
            # transient drop — release the claim but leave the
            # delivery_id alive so a later tick may re-claim it
            self._reg.release_claim(spec["delivery_id"])

    # -- one poll tick ---------------------------------------------------

    def _scan_specs(self) -> list:
        """Sync part of the tick — directory scan + parse + validate.
        Returns [(path, spec)] for claimable specs; rejected specs are
        logged and skipped (never half-parsed into a send)."""
        out = []
        for path in self._spec_files():
            try:
                with open(path, "rb") as handle:
                    spec = json.loads(handle.read().decode("utf-8"))
            except ValueError:
                # publication is atomic — a readable file that fails to
                # parse is permanently corrupt; quarantine instead of
                # re-reading it every tick (the runner re-publishes a
                # queued render whose spec vanished)
                self._log("spec_corrupt",
                          delivery_id=os.path.basename(path)[:-5])
                with suppress(OSError):
                    os.replace(path, path + ".invalid")
                continue
            except OSError:
                continue                       # transient — next tick
            try:
                self._validate_spec(spec)
            except ValueError as e:
                self._log("spec_rejected",
                          delivery_id=os.path.basename(path)[:-5],
                          error=str(e))
                continue
            if self._ours(spec["delivery"]):
                out.append((path, spec))
        return out

    def _validate_spec(self, spec: dict) -> None:
        spec_mod.validate(spec)

    async def _fresh_claim(self, path: str, spec: dict,
                           now: float) -> dict | None:
        """Claim a live spec — an orphan marker (no registry claim) must
        age past CLAIM_STALE_S first, or it could still belong to a
        just-started peer whose lock acquisition we can't see."""
        delivery_id = spec["delivery_id"]
        marker = self._claim_path(path)
        if await asyncio.to_thread(os.path.exists, marker):
            # the scope lock makes us the only live sender, so a marker
            # without a registry claim is an orphan left by a worker
            # that died mid-claim.
            try:
                st = await asyncio.to_thread(os.stat, marker)
            except OSError:
                st = None
            if st is None:
                return None                     # vanished — next tick
            if now - st.st_mtime < CLAIM_STALE_S:
                return None                     # fresh claim in flight
            try:
                await asyncio.to_thread(os.unlink, marker)
            except OSError:
                return None                     # vanished — next tick
            self._log("claim_stale_reclaimed", delivery_id=delivery_id)
        try:
            await self._claim_spec(path, spec)
        except OSError as e:
            self._log("claim_failed", delivery_id=delivery_id,
                      error=type(e).__name__)
            return None
        return self._reg.claimed(delivery_id)

    async def _resume_dead(self, resume: list) -> None:
        """dead specs whose dependent parts never finished get their
        unjournaled remainder driven once per tick — journal phases
        dedupe everything already proven."""
        records = await asyncio.to_thread(
            journal.scan, self._dirs["state"])
        for spec in resume:
            try:
                await self._resume_parts(spec, records)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log("parts_resume_error",
                          delivery_id=spec["delivery_id"],
                          error=type(e).__name__)

    async def _settle_orphan_claims(self, live_ids: set) -> None:
        """settle claims whose spec vanished mid-flight — the runner
        may have cancelled the render; a granted attempt must not hang
        as an unsettled row forever."""
        for delivery_id, claim in list(self._reg.claims().items()):
            if delivery_id in live_ids:
                continue
            if claim["phase"] in ("started", "result", "settled"):
                await self._step_claim(claim)
                continue
            if claim["phase"] == "begin_sent":
                result, code = "not_sent", "spec_withdrawn"
            elif claim["phase"] == "granted":
                started = await asyncio.to_thread(
                    self._started, claim)
                result = "unknown" if started else "not_sent"
                code = "spec_withdrawn" if not started \
                    else "worker_crash"
            else:
                await self._drop_claim(claim)
                continue
            env = envelopes.transport_receipt(
                claim, result, error_code=code)
            try:
                await asyncio.to_thread(
                    envelopes.publish_command,
                    self._dirs["cmd_int"], env)
                self._journal("receipt",
                              attempt_id=claim["attempt_id"],
                              delivery_id=delivery_id,
                              result=result, error_code=code)
            except OSError:
                continue                         # keep claim — retry next
            await self._drop_claim(claim)

    async def tick(self) -> None:
        if self._stopping:
            return
        now = time.time()
        scanned = await asyncio.to_thread(self._scan_specs)
        # the runner-published flag is the cheap local kill switch —
        # claiming during an interactive-off window only earns a
        # non-final denial AND a dead tombstone that outlives the
        # window, stranding the queued render. Skip new claims; in-
        # flight claims still step (settlement is not a send).
        flags = await asyncio.to_thread(paths.read_flags, self._root)
        # restore_pending: a DB restore is unreconciled — claiming a
        # spec now can only earn a denial; in-flight claims still step
        # (settlement is not a send)
        claimable = flags.get("interactive") is not False \
            and flags.get("transport", "discord") == self.transport \
            and not flags.get("restore_pending")
        live_ids = set()
        # batch registry saves across the whole pass — one flush per
        # tick instead of ~3 full-file rewrites per claim (RC20: the
        # O(n^2) serialization was the delivery bottleneck at 1k cards)
        with self._reg.batch():
            self._reg.expire()
            resume = []
            for path, spec in scanned:
                delivery_id = spec["delivery_id"]
                live_ids.add(delivery_id)
                if self._reg.is_dead(delivery_id) \
                        and self._reg.claimed(delivery_id) is None:
                    if not self._reg.parts_done(delivery_id):
                        resume.append(spec)
                    continue          # dropped claims never re-claim
                claim = self._reg.claimed(delivery_id)
                if claim is None and not claimable:
                    continue
                if claim is None:
                    claim = await self._fresh_claim(path, spec, now)
                if claim is not None:
                    try:
                        await self._step_claim(claim)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._log("claim_step_error",
                                  delivery_id=delivery_id,
                                  error=type(e).__name__)
            if resume:
                await self._resume_dead(resume)
            await self._settle_orphan_claims(live_ids)

    def _started(self, claim: dict) -> bool:
        """Conservative check — 'granted' phase means journal 'started'
        is either written or imminent; anything else is unknown."""
        records = journal.scan(self._dirs["state"])
        rows = records.get(claim["attempt_id"], [])
        return any(r.get("phase") == "started" for r in rows)

    def stop(self) -> None:
        self._stopping = True
