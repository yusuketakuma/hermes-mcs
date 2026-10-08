"""Durable worker registry — one JSON document per delivery scope.

Holds what the snapshot cannot: deliveries claimed since the last
snapshot publish, token context carried by each rendered button, in-flight modal /
confirm flows, followup tokens (15-minute Discord ceiling), and the
per-scope capability cache. Rewritten atomically on every mutation;
a lost file is rebuilt from the snapshot — never invented.

Inside a batch, mutations that must be durable before the caller's next
step (posted-button tokens before their receipt, a LINE WORKS retirement
before HTTP) append one fsync'd row to ``<registry>.delta`` instead of
rewriting the whole file; the batch flush folds them in and ``reload``
replays rows newer than the file's ``delta_seq``. Each row names the
digest of the main file it extends: a main file rewritten by code that
never folded the delta (an older release after a rollback) makes those
rows stale, and they are ignored rather than replayed over newer state.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import os
import secrets
import threading
import time

from . import paths

CONFIRM_TTL_S = 600          # pending_confirms: preview -> confirm
FOLLOWUP_TTL_S = 840         # Discord interaction tokens die ~15 min
CAPABILITY_NEG_S = 900       # negative thread-capability cache
CONTEXT_KEEP_S = 32 * 86400  # tokens/messages: covers the runner's
                             # 30-day view-token TTL plus margin


def new_worker_id() -> str:
    return secrets.token_hex(8)


def scope_key(scope: dict) -> str:
    # Preserve the deployed lock namespace across upgrades.
    raw = "|".join(str(scope.get(k) or "-") for k in
                   ("profile", "application_id", "channel_id"))
    if scope.get("transport") in ("slack", "lineworks"):
        raw = f"{scope['transport']}|{scope['team_id']}|{raw}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def new_attempt_id() -> str:
    return secrets.token_hex(8)


def new_modal_id() -> str:
    return secrets.token_hex(8)


def new_confirm_id() -> str:
    return secrets.token_hex(8)


DEAD_TTL_S = 86400          # dropped delivery_ids — one-shot by design


def _default() -> dict:
    # Keep legacy message bindings readable/expirable; current workers use
    # the runner ledger as the binding authority and no longer duplicate them.
    return {"v": 1,
            "claims": {}, "messages": {}, "tokens": {},
            "pending_modals": {}, "pending_confirms": {},
            "followups": {}, "capabilities": {}, "dead": {},
            "parts": {}}


def _expired(value, *, ttl: float = 0, now: float | None = None) -> bool:
    """Malformed expiry never preserves an interaction authorization."""
    if type(value) not in (int, float):
        return True
    try:
        return (not math.isfinite(value) or value < 0
                or value + ttl <= (time.time() if now is None else now))
    except OverflowError:
        return True


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:32]


def _locked(method):
    """Serialize a Registry method on the instance lock — workers call
    mutators via asyncio.to_thread while save() serializes on the loop,
    and json.dumps over a dict another thread resizes raises."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class _RegistryBatch:
    """Context manager returned by Registry.batch — defers save() calls
    and flushes once on exit."""

    def __init__(self, reg: Registry) -> None:
        self._reg = reg

    def __enter__(self):
        self._reg._batch_depth += 1
        return self._reg

    def __exit__(self, *exc):
        self._reg._batch_depth -= 1
        if self._reg._batch_depth == 0 and self._reg._dirty:
            self._reg.save()
        return False


class Registry:
    def __init__(self, state_dir: str, *, scope: dict | None = None):
        self._scope = scope
        self._path = os.path.join(
            state_dir, f"registry-{scope_key(scope)}.json"
            if scope is not None else "registry.json")
        self._delta_path = self._path + ".delta"
        self._batch_depth = 0
        self._dirty = False
        self._lock = threading.RLock()     # re-entrant: mutators call save()
        self.reload()

    @_locked
    def reload(self) -> None:
        """Read after acquiring the send lock, before accepting interactions."""
        try:
            with open(self._path, "rb") as handle:
                raw = handle.read()
            self._base = _digest(raw)
            data = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            self._base = None
            data = None
        except (ValueError, RecursionError):
            raise ValueError("registry_corrupt") from None
        else:
            if not isinstance(data, dict):
                raise ValueError("registry_corrupt")
        self._data = data if data is not None else _default()
        for key, default in _default().items():
            if key not in self._data:
                self._data[key] = default
            elif type(self._data[key]) is not type(default):
                raise ValueError("registry_corrupt")
            if isinstance(default, dict) and key not in ("dead", "parts"):
                if not all(isinstance(row, dict) for row in self._data[key].values()):
                    raise ValueError("registry_corrupt")
        if data is None and self._scope is not None:
            self._restore_legacy_scope()
        self._seq = self._data.get("delta_seq", 0)
        if type(self._seq) is not int:
            raise ValueError("registry_corrupt")
        self._replay_delta()

    def _replay_delta(self) -> None:
        """Apply committed delta rows the main file has not folded yet.
        An unterminated tail is an append whose fsync never returned, so
        nothing after it (receipt, HTTP) happened — it is skipped. Any
        terminated row that fails to parse fails closed like the file."""
        try:
            with open(self._delta_path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return
        for line in raw.split(b"\n")[:-1]:
            try:
                row = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                raise ValueError("registry_corrupt") from None
            if not isinstance(row, dict) or type(row.get("seq")) is not int:
                raise ValueError("registry_corrupt")
            if row["seq"] <= self._seq or row.get("base") != self._base:
                continue    # folded, or extends a main file since rewritten
            if row.get("op") == "put" and isinstance(row.get("tokens"), dict) \
                    and all(isinstance(c, dict) for c in row["tokens"].values()):
                self._data["tokens"].update(row["tokens"])
            elif row.get("op") == "retire" and isinstance(row.get("card_key"), str):
                self._drop_card_tokens(row["card_key"])
            else:
                raise ValueError("registry_corrupt")
            self._seq = row["seq"]

    def _durable(self, row: dict) -> None:
        """Make one mutation durable now. In a batch: an fsync'd delta
        append (the flush folds it); otherwise the ordinary full save."""
        if not self._batch_depth:
            self.save()
            return
        self._seq += 1
        data = (json.dumps({**row, "seq": self._seq, "base": self._base},
                           ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
                + "\n").encode("utf-8")
        created = not os.path.exists(self._delta_path)
        fd = os.open(self._delta_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as handle:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                # drop a torn (never committed) tail so this row stays parsable
                os.ftruncate(fd, os.pread(fd, size, 0).rfind(b"\n") + 1)
            handle.write(data)
            handle.flush()
            os.fsync(fd)
        if created:
            paths.fsync_dir(os.path.dirname(self._delta_path))
        self._dirty = True

    def _restore_legacy_scope(self) -> None:
        """Keep the legacy file intact; import only provably owned state."""
        try:
            with open(os.path.join(os.path.dirname(self._path),
                                   "registry.json"), "rb") as handle:
                legacy = json.load(handle)
        except (OSError, ValueError, RecursionError):
            return
        if not isinstance(legacy, dict):
            return
        owner_key = scope_key(self._scope)
        for table in ("claims", "pending_modals", "pending_confirms"):
            records = legacy.get(table)
            if not isinstance(records, dict):
                continue
            for key, record in records.items():
                if not isinstance(record, dict):
                    continue
                spec = record.get("spec")
                origin = ((spec.get("delivery", {}) if isinstance(spec, dict) else {})
                          if table == "claims" else record.get("origin", {}))
                if not isinstance(origin, dict):
                    continue
                try:
                    origin_key = scope_key(origin)
                except (KeyError, ValueError):
                    continue  # malformed origin cannot prove ownership
                if origin_key == owner_key:
                    self._data[table][key] = record
        # Legacy token/followup rows lack profile provenance. Keep them in the
        # old file rather than lending another profile their interaction tokens.

    def batch(self):
        """Coalesce per-record saves into one flush — for the worker's
        hot loop where hundreds of claims/sends per tick would each
        rewrite the whole file. Durability does NOT depend on this
        index: the journal (fsync per line), claim markers and the
        runner ledger carry the evidence, so a crash mid-batch only
        replays work the transport layer already idempotents."""
        return _RegistryBatch(self)

    @_locked
    def save(self, *, immediate: bool = False) -> None:
        if self._batch_depth and not immediate:
            self._dirty = True
            return
        self._data["delta_seq"] = self._seq
        raw = json.dumps(self._data, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        paths.atomic_write(self._path, raw, tmp_prefix=".reg-",
                           mode=0o600)
        self._base = _digest(raw)
        self._dirty = False
        # delta_seq makes a surviving delta a no-op; this only bounds size
        with contextlib.suppress(OSError):
            os.unlink(self._delta_path)

    # -- claims ----------------------------------------------------

    @_locked
    def claim(self, delivery_id: str, record: dict) -> None:
        self._data["claims"][delivery_id] = record
        self.save()

    @_locked
    def claimed(self, delivery_id: str) -> dict | None:
        return self._data["claims"].get(delivery_id)

    @_locked
    def drop_claim(self, delivery_id: str) -> None:
        if self._data["claims"].pop(delivery_id, None) is not None:
            self.save()
        self.mark_dead(delivery_id)

    @_locked
    def release_claim(self, delivery_id: str) -> None:
        """Drop WITHOUT a dead tombstone — for transient denials where
        the spec stays legitimately claimable (e.g. interactive_off)."""
        if self._data["claims"].pop(delivery_id, None) is not None:
            self.save()

    # a dropped claim is final: every render mints a fresh delivery_id,
    # so re-claiming the same spec file can only churn denied begins
    @_locked
    def mark_dead(self, delivery_id: str) -> None:
        self._data["dead"][str(delivery_id)] = time.time()
        self.save()

    def is_dead(self, delivery_id: str) -> bool:
        return str(delivery_id) in self._data["dead"]

    @_locked
    def claims(self) -> dict:
        return dict(self._data["claims"])

    # -- token context (spec action_rows capture) ------------------

    @_locked
    def put_tokens(self, token_map: dict, *, durable: bool = False) -> None:
        """Store each button's render context by token.
        Stamped 'at' so expired runner tokens don't accumulate — the
        runner still enforces its own TTL; this only bounds file size.
        ``durable`` makes the whole map durable before returning (a
        posted card's pins must survive a crash before its receipt)."""
        changed = False
        now = time.time()
        for token, ctx in token_map.items():
            old = self._data["tokens"].get(token)
            if old is None or {k: v for k, v in old.items()
                               if k != "at"} != ctx:
                self._data["tokens"][token] = {**ctx, "at": now}
                changed = True
        if durable:
            self._durable({"op": "put", "tokens": {
                t: self._data["tokens"][t] for t in token_map}})
        elif changed:
            self.save()

    @_locked
    def prune_card_tokens(self, keep: dict) -> None:
        """Drop the button contexts an in-place update just replaced.

        ``keep`` is the delivered render's token map. A row goes only
        when it shares that render's card_key, channel and message pin
        (Slack stamps message_id; Discord rows carry none), has an
        action the render re-issued, and predates the render's own rows
        — so result-issued task_status tokens, other cards and any
        newer render's tokens stay. Only bounds file size: a pruned
        click falls to the refresh path."""
        tokens = self._data["tokens"]
        new = [tokens[t] for t in keep if t in tokens]
        if not new or not all(type(r.get("at")) in (int, float)
                              and isinstance(r.get("action"), str) for r in new):
            return
        ref = new[0]
        if not ref.get("card_key") or not ref.get("channel_id"):
            return
        actions = {r.get("action") for r in new}
        cutoff = min(r["at"] for r in new)
        stale = [t for t, r in tokens.items()
                 if t not in keep
                 and r.get("card_key") == ref["card_key"]
                 and r.get("channel_id") == ref["channel_id"]
                 and r.get("message_id") == ref.get("message_id")
                 and isinstance(r.get("action"), str)
                 and r["action"] in actions
                 and type(r.get("at")) in (int, float)
                 and r["at"] < cutoff]
        for t in stale:
            del tokens[t]
        if stale:
            self.save()

    def token(self, token: str) -> dict | None:
        return self._data["tokens"].get(token)

    @_locked
    def retire_card_tokens(self, card_key: str) -> None:
        """Invalidate every old card pin before a transport replaces an
        uneditable post — durable before returning, so before HTTP."""
        if self._drop_card_tokens(card_key):
            self._durable({"op": "retire", "card_key": card_key})

    def _drop_card_tokens(self, card_key: str) -> bool:
        stale = [token for token, context in self._data["tokens"].items()
                 if context.get("card_key") == card_key]
        for token in stale:
            del self._data["tokens"][token]
        return bool(stale)

    # -- pending modal / confirm flows -----------------------------

    def _unexpired(self, table: str, key: str) -> dict | None:
        """Lookup under the caller's lock, retiring malformed/expired records."""
        records = self._data[table]
        rec = records.get(key)
        if rec is not None and _expired(rec.get("expires")):
            records.pop(key, None)
            self.save()
            return None
        return rec

    @_locked
    def put_modal(self, modal_id: str, record: dict) -> None:
        self._data["pending_modals"][modal_id] = {
            **record, "expires": time.time() + CONFIRM_TTL_S}
        self.save(immediate=True)

    @_locked
    def modal(self, modal_id: str) -> dict | None:
        return self._unexpired("pending_modals", modal_id)

    @_locked
    def drop_modal(self, modal_id: str) -> None:
        if self._data["pending_modals"].pop(modal_id, None) is not None:
            self.save(immediate=True)

    @_locked
    def put_confirm(self, confirm_id: str, record: dict) -> None:
        self._data["pending_confirms"][confirm_id] = {
            **record, "expires": time.time() + CONFIRM_TTL_S}
        self.save(immediate=True)

    @_locked
    def confirm(self, confirm_id: str) -> dict | None:
        return self._unexpired("pending_confirms", confirm_id)

    @_locked
    def drop_confirm(self, confirm_id: str) -> None:
        if self._data["pending_confirms"].pop(confirm_id,
                                              None) is not None:
            self.save(immediate=True)

    @_locked
    def consume_confirm(self, confirm_id: str) -> None:
        """The command is durably queued: the confirm stays taken until
        its TTL, so a second 確定 from the preview that is still on screen
        reads as in progress (take_confirm -> busy) instead of expired —
        never a retry that queues the same task twice."""
        rec = self._data["pending_confirms"].get(confirm_id)
        if rec is not None:
            rec["in_flight"] = True
            rec["consumed"] = True
            self.save(immediate=True)

    # 確定 awaits the durable command write; a 取消 (or a second 確定)
    # landing inside that await must see the confirm as taken, never
    # report 取り消しました for a command that is being queued. First
    # caller wins; the marker is persisted so a crash mid-publish leaves
    # the confirm unusable (the command may already be queued) until TTL.
    # One synchronous decision (no await between check and mark) shared
    # by every transport: gone -> busy -> cancel -> denied -> taken.
    # ``allowed`` is the caller's late scope recheck; it gates only the
    # 確定 path, so an out-of-scope confirm can still be cancelled.
    @_locked
    def take_confirm(self, confirm_id: str, cancel: bool, *,
                     allowed: bool = True) -> str:
        rec = self.confirm(confirm_id)
        if rec is None:
            return "gone"
        if rec.get("in_flight"):
            return "busy"
        if cancel:
            self.drop_confirm(confirm_id)
            return "cancelled"
        if not allowed:
            return "denied"
        rec["in_flight"] = True
        self.save(immediate=True)
        return "taken"

    @_locked
    def end_confirm(self, confirm_id: str) -> None:
        """Publish failed before the command was queued — the user may retry."""
        rec = self._data["pending_confirms"].get(confirm_id)
        if rec is not None and rec.pop("in_flight", None) is not None:
            self.save(immediate=True)

    # -- followups --------------------------------------------------

    @_locked
    def put_followup(self, command_id: str, record: dict) -> None:
        self._data["followups"][command_id] = {
            **record, "expires": time.time() + FOLLOWUP_TTL_S}
        self.save(immediate=True)

    @_locked
    def followup(self, command_id: str) -> dict | None:
        return self._unexpired("followups", command_id)

    @_locked
    def drop_followup(self, command_id: str) -> bool:
        """True for the one caller that removed the record — it delivers
        the result; a concurrent sweep that lost the race gets False."""
        if self._data["followups"].pop(command_id, None) is not None:
            self.save(immediate=True)
            return True
        return False

    @_locked
    def followups(self) -> dict:
        return dict(self._data["followups"])

    # -- capability cache ------------------------------------------

    def capability(self, scope_key: str) -> dict | None:
        rec = self._data["capabilities"].get(scope_key)
        if not rec:
            return None
        if rec.get("ok") is False \
                and _expired(rec.get("at"), ttl=CAPABILITY_NEG_S):
            return None                     # negative entries age out
        return rec

    @_locked
    def put_capability(self, scope_key: str, ok: bool) -> None:
        self._data["capabilities"][scope_key] = {
            "ok": ok, "at": time.time()}
        self.save()

    # -- durable part progress -----------------------------------------

    def parts_done(self, delivery_id: str) -> bool:
        """Every manifest part of this delivery already carries journal
        evidence — resume scans can skip it entirely."""
        return self._data["parts"].get(str(delivery_id)) == "done"

    @_locked
    def put_parts_done(self, delivery_id: str) -> None:
        if self._data["parts"].get(str(delivery_id)) != "done":
            self._data["parts"][str(delivery_id)] = "done"
            self.save()

    @_locked
    def done_parts(self) -> set:
        return {k for k, v in self._data["parts"].items() if v == "done"}

    # -- sweep -------------------------------------------------------

    @_locked
    def expire(self, *, keep: set | frozenset = frozenset()) -> None:
        """Drop dead followups/modals/confirms — the 14-minute Discord
        ceiling means a followup older than that can never send.

        ``keep`` names delivery_ids whose spec file is still published:
        their dead tombstone outlives DEAD_TTL_S, or a lingering spec
        (e.g. an unknown render awaiting card_resolve) would be
        re-claimed daily. Part progress expires with its tombstone."""
        now = time.time()
        changed = False
        stale = [k for k, v in self._data["dead"].items()
                 if k not in keep
                 and _expired(v, ttl=DEAD_TTL_S, now=now)]
        for k in stale:
            del self._data["dead"][k]
            changed = True
        orphan = [k for k in self._data["parts"]
                  if k not in self._data["dead"]
                  and k not in self._data["claims"]]
        for k in orphan:
            del self._data["parts"][k]
            changed = True
        for table in ("pending_modals", "pending_confirms", "followups"):
            dead = [k for k, v in self._data[table].items()
                    if _expired(v.get("expires"), now=now)]
            for k in dead:
                del self._data[table][k]
                changed = True
        for table in ("tokens", "messages"):
            dead = [k for k, v in self._data[table].items()
                    if _expired(v.get("at"), ttl=CONTEXT_KEEP_S, now=now)]
            for k in dead:
                del self._data[table][k]
                changed = True
        if changed:
            self.save()
