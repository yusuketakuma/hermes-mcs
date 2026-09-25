"""Delivery worker — claim -> transport_begin -> grant -> Discord send
-> transport_receipt, journaled at every phase.

Ordering contract (plan §4/§5): a claim marker is not a send grant;
the grant is durable runner-side before HTTP begins; ``started`` is
fsync'd before the request fires; ``result`` is fsync'd before the
receipt is published. Crashes land between those records and are
classified honestly on the next start — pre-HTTP grants settle as
``not_sent`` (journal integrity + the scope lock prove it), post-HTTP
silences stay ``unknown`` for an operator to resolve, and an in-flight
send is never retried by a new worker.

All filesystem work happens via ``asyncio.to_thread``; the event loop
never blocks on the spec/result poll.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from typing import Any

from . import actions, cards, envelopes, journal, paths, registry

POLL_S = 2.0
REPUBLISH_BEGIN_S = 60.0       # re-send the same begin if no result
RETRY_IN_FLIGHT_S = 15.0       # denied_in_flight re-begin backoff
MAX_BEGIN_RETRIES = 20         # ~5min of in_flight before giving up
CLAIM_STALE_S = 60.0           # orphan .claimed marker age before reclaim

# HTTP statuses that prove the send was rejected outright — never
# committed server-side. Timeouts/cancellations are unknown instead.
NOT_SENT_STATUS = frozenset({400, 401, 403, 404, 405, 410})
# Discord delete of an already-gone message achieves the revoke goal.
REVOKE_GONE_STATUS = frozenset({404, 410})


def _err_code(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"http_{status}"
    return type(exc).__name__.lower()


def _is_definitive_reject(exc: BaseException) -> bool:
    """A 4xx status is an explicit answer — the send never committed.
    Timeouts, disconnects and cancellations cannot prove that."""
    status = getattr(exc, "status", None)
    return isinstance(status, int) and status in NOT_SENT_STATUS


class DeliveryWorker:
    transport = "discord"

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
        """fcntl lock keyed on (profile, application_id, channel_id) —
        one live sender per delivery scope, process-wide. Held for the
        worker's lifetime; released only when all tasks have stopped."""
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
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
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
        if mine.get("guild_id") \
                and str(delivery.get("guild_id") or "") \
                != str(mine["guild_id"]):
            return False
        return True

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
        self._reg.put_tokens(cards.token_map(spec))
        self._reg.claim(spec["delivery_id"], claim)
        await self._publish_begin(claim)

    @staticmethod
    def _write_marker(path: str, raw: bytes) -> None:
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=".claim-", suffix=".tmp",
                                   dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "wb") as s:
                s.write(raw)
                s.flush()
                os.fsync(s.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

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
        if result.get("route_epoch") \
                != spec["delivery"]["route_epoch"]:
            return False
        return True

    # -- Discord ops ----------------------------------------------------

    async def _channel(self, channel_id: str):
        ch = self._bot.get_channel(int(channel_id))
        if ch is None:
            ch = await self._bot.fetch_channel(int(channel_id))
        return ch

    async def _perform(self, claim: dict) -> dict:
        """One HTTP attempt. Returns {result, message_id?, error_code?}.
        Only a definitive rejection maps to not_sent; anything that can
        have committed is unknown (§4)."""
        spec = claim["spec"]
        delivery = spec["delivery"]
        op = spec["op"]
        channel = await self._channel(delivery["channel_id"])
        if op == "revoke":
            mid = delivery.get("message_id")
            if not mid:
                return {"result": "not_sent",
                        "error_code": "no_target"}
            try:
                msg = await channel.fetch_message(int(mid))
                await msg.delete()
            except Exception as exc:
                status = getattr(exc, "status", None)
                if isinstance(status, int) and status in REVOKE_GONE_STATUS:
                    # already gone — the revoke goal holds
                    return {"result": "delivered", "message_id": mid}
                raise
            return {"result": "delivered", "message_id": mid}
        view = cards.build_view(spec)
        if op == "update":
            mid = delivery.get("message_id")
            if not mid:
                return {"result": "not_sent", "error_code": "no_target"}
            msg = await channel.fetch_message(int(mid))
            await msg.edit(view=view)
            return {"result": "delivered", "message_id": mid}
        sent = await channel.send(view=view)      # create / notice
        return {"result": "delivered",
                "message_id": str(sent.id)}

    async def _maybe_thread(self, claim: dict, message_id: str) -> None:
        """Card companion thread — separated from the body send; a
        thread failure never resends the card itself (plan §3)."""
        spec = claim["spec"]
        name = (spec["parts"].get("thread_name")
                if spec["op"] in ("create", "notice") else None)
        if not name or (spec["delivery"].get("thread_id")):
            return
        scope_key = registry.scope_key(self.scope())
        cap = self._reg.capability(scope_key)
        if cap is not None and cap.get("ok") is False:
            return                                 # negative-cached
        thread = None
        try:
            channel = await self._channel(spec["delivery"]["channel_id"])
            sent_message = await channel.fetch_message(int(message_id))
            thread = await sent_message.create_thread(name=name)
            self._reg.put_capability(scope_key, True)
            env = envelopes.thread_receipt(
                spec["delivery_id"], message_id,
                thread_id=str(thread.id))
        except Exception as exc:
            if _is_definitive_reject(exc):
                self._reg.put_capability(scope_key, False)
            env = envelopes.thread_receipt(
                spec["delivery_id"], message_id,
                error_code=_err_code(exc))
        await asyncio.to_thread(
            envelopes.publish_command, self._dirs["cmd_int"], env)
        await self._thread_body(spec, thread)

    async def _thread_body(self, spec: dict, thread) -> None:
        """Full text lands inside the fresh companion thread — the card
        itself stays a summary surface. Best-effort by design: the
        thread (and its receipt) is already settled, so a chunk send
        failure only logs; it must not re-enter the delivery path."""
        if thread is None:
            return
        body = str(spec["parts"].get("thread_body") or "")
        if not body.strip():
            return
        for chunk in actions._split_body(body):
            if not chunk.strip():
                continue
            try:
                await thread.send(chunk)
            except Exception as exc:
                self._log("thread_body_failed",
                          error=type(exc).__name__)
                return

    # -- the per-claim step ----------------------------------------------

    async def _step_claim(self, claim: dict) -> None:
        spec = claim["spec"]
        now = time.time()
        if claim["phase"] == "begin_sent":
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
                # interactive_off is a transient denial — the queued
                # render stays legitimate, so no dead tombstone: the
                # flags check above already stops the re-claim churn,
                # and the spec must be claimable again once the kill
                # switch lifts
                await self._drop_claim(
                    claim, dead=error != "denied_interactive_off")
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
        if claim["phase"] == "granted":
            # fsync'd BEFORE the HTTP request — the single line that
            # separates provable-not-sent from honest-unknown on crash
            self._journal("started",
                          attempt_id=claim["attempt_id"],
                          delivery_id=spec["delivery_id"],
                          correlation=spec["delivery"]["correlation"])
            claim["phase"] = "started"
            try:
                outcome = await self._perform(claim)
            except asyncio.CancelledError:
                raise                            # unknown by omission
            except Exception as exc:
                if _is_definitive_reject(exc):
                    outcome = {"result": "not_sent",
                               "error_code": _err_code(exc)}
                else:
                    outcome = {"result": "unknown",
                               "error_code": _err_code(exc)}
            # Move past HTTP before any fallible journal/receipt I/O. A
            # retry settles this outcome; it must never call Discord again.
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
                await self._maybe_thread(claim, str(mid))
        if claim["phase"] == "settled":
            await self._drop_claim(claim)

    async def _drop_claim(self, claim: dict, dead: bool = True) -> None:
        spec = claim["spec"]
        try:
            await asyncio.to_thread(
                os.unlink, self._claim_path(claim["spec_path"]))
        except OSError:
            pass
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
                try:
                    os.replace(path, path + ".invalid")
                except OSError:
                    pass
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
        cards.validate(spec)

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
        claimable = flags.get("interactive") is not False \
            and flags.get("transport", "discord") == self.transport
        live_ids = set()
        # batch registry saves across the whole pass — one flush per
        # tick instead of ~3 full-file rewrites per claim (RC20: the
        # O(n^2) serialization was the delivery bottleneck at 1k cards)
        with self._reg.batch():
            self._reg.expire()
            for path, spec in scanned:
                delivery_id = spec["delivery_id"]
                live_ids.add(delivery_id)
                if self._reg.is_dead(delivery_id) \
                        and self._reg.claimed(delivery_id) is None:
                    continue          # dropped claims never re-claim
                claim = self._reg.claimed(delivery_id)
                if claim is None and not claimable:
                    continue
                if claim is None:
                    marker = self._claim_path(path)
                    if await asyncio.to_thread(os.path.exists, marker):
                        # the scope lock makes us the only live sender,
                        # so a marker without a registry claim is an
                        # orphan left by a worker that died mid-claim.
                        # Age it past CLAIM_STALE_S before reclaiming —
                        # a fresher one could still belong to a
                        # just-started peer whose lock acquisition we
                        # can't see here.
                        try:
                            st = await asyncio.to_thread(
                                os.stat, marker)
                        except OSError:
                            st = None
                        if st is None:
                            continue            # vanished — next tick
                        if now - st.st_mtime < CLAIM_STALE_S:
                            continue            # fresh claim in flight
                        try:
                            await asyncio.to_thread(os.unlink, marker)
                        except OSError:
                            continue            # vanished — next tick
                        self._log("claim_stale_reclaimed",
                                  delivery_id=delivery_id)
                    try:
                        await self._claim_spec(path, spec)
                    except OSError as e:
                        self._log("claim_failed",
                                  delivery_id=delivery_id,
                                  error=type(e).__name__)
                        continue
                    claim = self._reg.claimed(delivery_id)
                if claim is not None:
                    try:
                        await self._step_claim(claim)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._log("claim_step_error",
                                  delivery_id=delivery_id,
                                  error=type(e).__name__)
            # settle claims whose spec vanished mid-flight — the runner
            # may have cancelled the render; a granted attempt must not
            # hang as an unsettled row forever
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
                    continue                     # keep claim — retry next
                await self._drop_claim(claim)

    def _started(self, claim: dict) -> bool:
        """Conservative check — 'granted' phase means journal 'started'
        is either written or imminent; anything else is unknown."""
        records = journal.scan(self._dirs["state"])
        rows = records.get(claim["attempt_id"], [])
        return any(r.get("phase") == "started" for r in rows)

    def stop(self) -> None:
        self._stopping = True
