"""Durable worker registry — one JSON document per delivery scope.

Holds what the snapshot cannot: deliveries claimed since the last
snapshot publish, token context carried by each rendered button, in-flight modal /
confirm flows, followup tokens (15-minute Discord ceiling), and the
per-scope capability cache. Rewritten atomically on every mutation;
a lost file is rebuilt from the snapshot — never invented.
"""
from __future__ import annotations

import json
import os
import secrets
import time

CONFIRM_TTL_S = 600          # pending_confirms: preview -> confirm
FOLLOWUP_TTL_S = 840         # Discord interaction tokens die ~15 min
CAPABILITY_NEG_S = 900       # negative thread-capability cache
CONTEXT_KEEP_S = 32 * 86400  # tokens/messages: covers the runner's
                             # 30-day view-token TTL plus margin


def new_worker_id() -> str:
    return secrets.token_hex(8)


def scope_key(scope: dict) -> str:
    import hashlib
    # Preserve the deployed lock namespace across upgrades.
    raw = "|".join(str(scope.get(k) or "-") for k in
                   ("profile", "application_id", "channel_id"))
    if scope.get("transport") == "slack":
        raw = f"slack|{scope['team_id']}|{raw}"
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
            "followups": {}, "capabilities": {}, "dead": {}}


class _RegistryBatch:
    """Context manager returned by Registry.batch — defers save() calls
    and flushes once on exit."""

    def __init__(self, reg: "Registry") -> None:
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
        self._batch_depth = 0
        self._dirty = False
        self.reload()

    def reload(self) -> None:
        """Read after acquiring the send lock, before accepting interactions."""
        try:
            with open(self._path, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError):
            data = None
        self._data = data if isinstance(data, dict) else _default()
        for key, default in _default().items():
            if not isinstance(self._data.get(key), type(default)):
                self._data[key] = default
        if data is None and self._scope is not None:
            self._restore_legacy_scope()

    def _restore_legacy_scope(self) -> None:
        """Keep the legacy file intact; import only provably owned state."""
        try:
            with open(os.path.join(os.path.dirname(self._path),
                                   "registry.json"), "rb") as handle:
                legacy = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(legacy, dict):
            return
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
                if isinstance(origin, dict) and scope_key(origin) == scope_key(self._scope):
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

    def save(self, *, immediate: bool = False) -> None:
        if self._batch_depth and not immediate:
            self._dirty = True
            return
        self._dirty = False
        raw = json.dumps(self._data, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        fd, temp = None, None
        import tempfile
        fd, temp = tempfile.mkstemp(prefix=".reg-", suffix=".tmp",
                                    dir=os.path.dirname(self._path))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)
            dfd = os.open(os.path.dirname(self._path),
                          os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    # -- claims ----------------------------------------------------

    def claim(self, delivery_id: str, record: dict) -> None:
        self._data["claims"][delivery_id] = record
        self.save()

    def claimed(self, delivery_id: str) -> dict | None:
        return self._data["claims"].get(delivery_id)

    def drop_claim(self, delivery_id: str) -> None:
        if self._data["claims"].pop(delivery_id, None) is not None:
            self.save()
        self.mark_dead(delivery_id)

    def release_claim(self, delivery_id: str) -> None:
        """Drop WITHOUT a dead tombstone — for transient denials where
        the spec stays legitimately claimable (e.g. interactive_off)."""
        if self._data["claims"].pop(delivery_id, None) is not None:
            self.save()

    # a dropped claim is final: every render mints a fresh delivery_id,
    # so re-claiming the same spec file can only churn denied begins
    def mark_dead(self, delivery_id: str) -> None:
        self._data["dead"][str(delivery_id)] = time.time()
        self.save()

    def is_dead(self, delivery_id: str) -> bool:
        return str(delivery_id) in self._data["dead"]

    def claims(self) -> dict:
        return self._data["claims"]

    # -- token context (spec action_rows capture) ------------------

    def put_tokens(self, token_map: dict) -> None:
        """Store each button's render context by token.
        Stamped 'at' so expired runner tokens don't accumulate — the
        runner still enforces its own TTL; this only bounds file size."""
        changed = False
        now = time.time()
        for token, ctx in token_map.items():
            old = self._data["tokens"].get(token)
            if old is None or {k: v for k, v in old.items()
                               if k != "at"} != ctx:
                self._data["tokens"][token] = {**ctx, "at": now}
                changed = True
        if changed:
            self.save()

    def token(self, token: str) -> dict | None:
        return self._data["tokens"].get(token)

    # -- pending modal / confirm flows -----------------------------

    def put_modal(self, modal_id: str, record: dict) -> None:
        self._data["pending_modals"][modal_id] = {
            **record, "expires": time.time() + CONFIRM_TTL_S}
        self.save(immediate=True)

    def modal(self, modal_id: str) -> dict | None:
        rec = self._data["pending_modals"].get(modal_id)
        if rec and rec.get("expires", 0) <= time.time():
            self._data["pending_modals"].pop(modal_id, None)
            self.save()
            return None
        return rec

    def drop_modal(self, modal_id: str) -> None:
        if self._data["pending_modals"].pop(modal_id, None) is not None:
            self.save(immediate=True)

    def put_confirm(self, confirm_id: str, record: dict) -> None:
        self._data["pending_confirms"][confirm_id] = {
            **record, "expires": time.time() + CONFIRM_TTL_S}
        self.save(immediate=True)

    def confirm(self, confirm_id: str) -> dict | None:
        rec = self._data["pending_confirms"].get(confirm_id)
        if rec and rec.get("expires", 0) <= time.time():
            self._data["pending_confirms"].pop(confirm_id, None)
            self.save()
            return None
        return rec

    def drop_confirm(self, confirm_id: str) -> None:
        if self._data["pending_confirms"].pop(confirm_id,
                                              None) is not None:
            self.save(immediate=True)

    # -- followups --------------------------------------------------

    def put_followup(self, command_id: str, record: dict) -> None:
        self._data["followups"][command_id] = {
            **record, "expires": time.time() + FOLLOWUP_TTL_S}
        self.save(immediate=True)

    def followup(self, command_id: str) -> dict | None:
        rec = self._data["followups"].get(command_id)
        if rec and rec.get("expires", 0) <= time.time():
            self._data["followups"].pop(command_id, None)
            self.save()
            return None
        return rec

    def drop_followup(self, command_id: str) -> None:
        if self._data["followups"].pop(command_id, None) is not None:
            self.save(immediate=True)

    def followups(self) -> dict:
        return dict(self._data["followups"])

    # -- capability cache ------------------------------------------

    def capability(self, scope_key: str) -> dict | None:
        rec = self._data["capabilities"].get(scope_key)
        if not rec:
            return None
        if rec.get("ok") is False \
                and rec.get("at", 0) + CAPABILITY_NEG_S <= time.time():
            return None                     # negative entries age out
        return rec

    def put_capability(self, scope_key: str, ok: bool) -> None:
        self._data["capabilities"][scope_key] = {
            "ok": ok, "at": time.time()}
        self.save()

    # -- sweep -------------------------------------------------------

    def expire(self) -> None:
        """Drop dead followups/modals/confirms — the 14-minute Discord
        ceiling means a followup older than that can never send."""
        now = time.time()
        changed = False
        stale = [k for k, v in self._data["dead"].items()
                 if v + DEAD_TTL_S <= now]
        for k in stale:
            del self._data["dead"][k]
            changed = True
        for table in ("pending_modals", "pending_confirms", "followups"):
            dead = [k for k, v in self._data[table].items()
                    if v.get("expires", 0) <= now]
            for k in dead:
                del self._data[table][k]
                changed = True
        for table in ("tokens", "messages"):
            dead = [k for k, v in self._data[table].items()
                    if v.get("at", 0) + CONTEXT_KEEP_S <= now]
            for k in dead:
                del self._data[table][k]
                changed = True
        if changed:
            self.save()
