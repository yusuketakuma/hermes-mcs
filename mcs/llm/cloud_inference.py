"""Own the shared cloud-provider contract and guarded send lifecycle for LLM callers."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

import anthropic_client
import openai_client
import provider_registry
import cloud_backlog
import cloud_client
import cloud_policy
import cloud_send_admission
from cloud_send_admission import CloudBusy, CloudFallback, CloudObservationIncomplete  # noqa: F401  (public names)
import mcs_util
import writer_lock
from mcs_util import HOME, load_config

COMMIT_MARGIN = 5
_HISTORY_ERROR_REASONS = frozenset({
    "cloud_result_missing", "cloud_result_discarded", "cloud_result_unsettled",
    "cloud_receipt_invalid", "cloud_answer_invalid", "cloud_answer_unresolved", "cloud_result_superseded",
})
_HISTORY_CLOUD_KINDS = frozenset({"budget_exceeded", "pricing_unverified", "contract_error"})
_HISTORY_CLOUD_REASONS = frozenset({
    "cloud_unit_unresolved", "cloud_profile_invalid", "model_not_allowed",
    "openai_price_contract_invalid", "input_binding_invalid", "window_tally_invalid",
    "pricing_unverified", "usage_invalid", "duplicate_reservation", "result_invalid",
})
_HISTORY_COMPARISONS = frozenset({'same', 'different', 'stored_absent', 'current_absent', 'unknown'})
_HISTORY_OBSERVATIONS = {
    **{key + '_cmp': _HISTORY_COMPARISONS for key in (
        'unit_key', 'binding', 'request_fp', 'model', 'effort', 'profile', 'max_tokens')},
    'guard_match': frozenset({'logical_equal', 'legacy_low_verified', 'legacy_unproven'}),
    'primary_reason': frozenset({'recovery_not_requested', 'unit_key_mismatch', 'stored_binding_missing',
        'input_binding_mismatch', 'request_envelope_mismatch', 'exact_keys_same_unexplained', 'observation_unknown'}),
    'recovery_path': frozenset({'skipped_binding', 'no_exact_rows', 'confirmed_rejection_only',
                                'settled_failure_only', 'unknown'}),
}


def _history_read_error(phase, error, *, observation=None):
    try:
        if type(phase) is not str or phase not in ("recover_response", "request_send_block_reason"):
            return
        category = next((name for cls, name in (
            (sqlite3.Error, "sqlite_error"), (ValueError, "value_error"),
            (TypeError, "type_error"), (KeyError, "key_error"),
            (cloud_client.CloudError, "cloud_error")) if isinstance(error, cls)), "read_error")
        values = {"phase": phase, "error_category": category}
        if isinstance(error, cloud_client.CloudError):
            for field, value, allowed in (
                    ("error_kind", error.kind, _HISTORY_CLOUD_KINDS),
                    ("error_reason", error.detail, _HISTORY_CLOUD_REASONS)):
                values[field] = value if type(value) is str and value in allowed else "unknown"
            refusal = getattr(error, "history_refusal", None)
            if refusal is not None:
                values["history_refusal"] = (refusal if type(refusal) is str
                                             and refusal in cloud_policy.HISTORY_REFUSALS else "unknown")
            compared = getattr(error, 'history_observation', None)
            if phase == 'request_send_block_reason' and type(compared) is dict:
                for key, allowed in _HISTORY_OBSERVATIONS.items():
                    source = observation if key == 'recovery_path' and type(observation) is dict else compared
                    value = source.get(key)
                    values[key] = (value if type(value) is str and value in allowed else
                                   'observation_unknown' if key == 'primary_reason' else 'unknown')
        if isinstance(error, ValueError):
            reason = error.args[0] if type(error) is ValueError and len(error.args) == 1 else None
            values["error_reason"] = (reason if type(reason) is str
                                      and reason in _HISTORY_ERROR_REASONS else "ValueError")
        if isinstance(error, sqlite3.Error):
            code = cloud_backlog._exception_observation(error).get("sqlite_errorcode")
            values["sqlite_primary_code"] = code & 0xff if type(code) is int else "unknown"
        print(json.dumps({"event": "cloud_history_read_error", "pid": os.getpid(),
                          "ts": time.time(), **values}), file=sys.stderr, flush=True)
    except Exception:
        pass  # Diagnostics must never replace the existing fail-closed refusal.


def _send_blocked(reason):
    """Name the policy code that refused a fresh send; the row itself defers."""
    try:
        print(json.dumps({"event": "cloud_send_blocked", "pid": os.getpid(), "ts": time.time(),
                          "reason": reason if type(reason) is str and reason in cloud_send_admission.PRE_SEND_VERDICTS else "unknown"}),
              file=sys.stderr, flush=True)
    except Exception:
        pass


def _current_cloud_settings():
    try:
        cfg = load_config()
        if not isinstance(cfg, dict) or not cfg:
            raise CloudObservationIncomplete("cloud_config_unverified")
        return resolve_cloud_settings(cfg)
    except CloudFallback:
        raise
    except Exception:
        raise CloudObservationIncomplete("cloud_config_unverified") from None


_RESULT_SAVE_TRIES = 5


def _after_send_code(metadata):
    """The classifier's after-send code for one attempt's result metadata; None for a clean answer."""
    http_class, kind = metadata.get("http_class"), metadata.get("error_kind")
    if kind == cloud_client.RESPONSE_TOO_LARGE:
        return cloud_client.RESPONSE_TOO_LARGE
    if not isinstance(http_class, str):
        return "unknown"
    if http_class == "http_200":
        # A 200 whose usage could not be priced (the client's pricing_unverified) or is
        # absent is an unknown result, charged at its reserved maximum.
        if kind == "pricing_unverified" or kind is None and metadata.get("usage") is None:
            return "usage_unknown"
        if kind is not None:
            return kind if type(kind) is str else "unknown"
        return None
    if http_class in ("http_429", "http_529"):
        return http_class
    if re.fullmatch(r"http_5[0-9]{2}", http_class):
        return "http_5xx"
    if re.fullmatch(r"http_4[0-9]{2}", http_class):
        return "http_4xx"
    if http_class == "transport":
        return kind if kind in ("transport", "timeout") else "transport"
    return "unknown"


def _save_result(save):
    """Wait out a busy writer: until this write lands, the answer already paid
    for exists only in memory, and giving up leaves the unit unresolved for good.
    Each try already waits the connection's busy timeout; saving is idempotent."""
    for attempt in range(_RESULT_SAVE_TRIES):
        try:
            return save()
        except sqlite3.OperationalError as error:
            busy = getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            if not busy or attempt == _RESULT_SAVE_TRIES - 1:
                raise


class CloudInferenceUnit:
    """Bind one unit's guarded HTTP attempts to the ledger's owning thread."""

    def __init__(self, ledger, cfg, *, route, project_id, unit_key, deadline,
                 current, dispatch, message_id=None, owner=None, discard_reason=None,
                 max_attempts=None, on_reserved_tx=None, reserve_dispatch=None, transport=None,
                 purpose=None, selection=None, cache_heads=(), input_binding=None, dispatch_current=None,
                 changed=None):
        purpose = purpose or ("patient_context" if route == "patient_context" else "backlog")
        raw_cfg = copy.deepcopy(cfg)
        if selection is not None:
            resolved = selection
        else:
            resolved, _ = cloud_policy.resolve_profile(
                cfg, route=route, purpose=purpose, root=HOME, ledger=ledger, ordered=True)
            if resolved is None:
                raise CloudFallback("cloud_profile_invalid")
        self.selection = resolved
        self.purpose = purpose
        self._profile_cfg = raw_cfg
        self._cache_heads = cache_heads
        cfg = {**cfg, "cloud_llm": resolve_cloud_settings(cfg)}
        self.model = resolved["model"]
        self.contract = cloud_provider_contract(cfg, route=route, purpose=purpose, selection=resolved)
        generation = cloud_policy.config_generation(cfg)
        scope = cloud_policy.scope_generation(cfg)
        updater = writer_lock.updater_state()
        key = cloud_policy.selected_key(HOME, cfg)
        self.dispatch = dispatch
        self.generation = generation
        self.scope = scope
        self.completed_reservations = []
        self.answered_reservations = []
        self._children = []
        self.message_id = message_id
        self.owner = owner
        self.input_binding = input_binding
        self._input_binding = lambda: (mcs_util.payload_hash([self.input_binding, scope])
                                      if self.input_binding is not None else None)
        self.route, self.project_id, self.unit_key = route, project_id, unit_key
        self._possibly_sent = False
        self._unresolved = False
        # Monotonic in-memory observations for a same-pass replacement: a verified
        # repeat refusal, and whether any effectful step (recovered answer used,
        # request slot / reservation / HTTP entered) ever began. Never reset.
        self._verified_repeat = False
        self._effect_started = False
        self.discard_reason = discard_reason
        self.ledger = ledger
        self.deadline = deadline
        self.discarded = False
        self.final_disposition = None
        # Why the latest call stopped, as (stage, code) for cloud_send_admission.classify_refusal;
        # None for a clean answer or a stop with no classified reason. Every call starts from
        # None. A guard denial also names its stop when commit_guard() runs outside a call, and
        # a paid answer discarded after sending (mode off mid-flight) keeps stage pre_send with
        # possibly_sent true; its verdict is the same.
        self.refusal = None
        # ``changed`` proves the unit's input changed; without it every denial closes the unit.
        self._changed = changed
        self._denial_proven = False
        self._unbilled = []

        reserved_caps = {}

        def refuse(stage, code):
            if stage == "after_send" and code is None:
                self.refusal = None   # a clean answer
                return
            # A sent attempt carries the outcome of its call; a later pre-send stop in the same
            # call (a retry refused at reservation) does not replace it.
            if stage != "after_send" and self.refusal is not None and self.refusal[0] == "after_send":
                return
            self.refusal = (stage, code if code in cloud_send_admission.REFUSAL_CODES else "unknown")
        self._refuse = refuse

        def guard(*, inference=True):
            # A denial observed after the response is sticky for this unit:
            # restoring the old source/scope cannot make its response current.
            if self.final_disposition is not None:
                raise CloudFallback("cloud_unit_changed")
            try:
                live = load_config()
                if not isinstance(live, dict) or not live:
                    raise CloudObservationIncomplete('cloud_config_unverified')
                if live.get('provider_unavailable'):
                    # A provider-file mismatch is not the owner's stop: hold, keep any response.
                    raise CloudObservationIncomplete('cloud_provider_unverified')
                if isinstance(live.get('cloud_llm'), dict) and live['cloud_llm'].get('mode') == 'off':
                    refuse('pre_send', 'mode_off')
                    self._denial_proven = True
                    raise CloudFallback('cloud_unit_changed')
                now = resolve_cloud_settings(live)
                chosen, errors = cloud_policy.resolve_profile(
                    live, route=route, purpose=purpose, root=HOME, ledger=ledger, ordered=True)
                if chosen is None:
                    chosen, errors = cloud_policy.resolve_profile(
                        live, route=route, purpose=purpose, root=HOME)
                    if errors or chosen is None:
                        raise CloudObservationIncomplete('cloud_profile_unverified')
                live_key = cloud_policy.selected_key(HOME, {"cloud_llm": now})
                if not live_key:
                    raise CloudObservationIncomplete('cloud_key_unverified')
                # The run window gates each new POST (retries included); a result that
                # already arrived is still judged only by scope, not by the clock.
                # Provider/model is the executor, not the generation: a resolved
                # fallback provider is adopted below rather than denied.
                changed_scope = (live_key != key
                                 or cloud_policy.scope_generation({"cloud_llm": now}) != scope
                                 # A paid result is kept while its scope holds, even if the send window closes.
                                 or not cloud_policy.in_scope(now, route, project_id))
                denied = (changed_scope or writer_lock.updater_state() != updater
                          or updater[0])
                if denied:
                    try:
                        if not cloud_policy.in_scope(now, route, project_id):
                            refuse('pre_send', self._blocked_code('scope', live))
                    except Exception:
                        pass   # naming the stop never changes the refusal
                if not denied and dict(chosen) != dict(self.selection):
                    self.selection = chosen
                    self.model = chosen["model"]
                    self.contract = cloud_provider_contract(selection=chosen)
                if not denied and inference:
                    try:
                        waiting = (not cloud_backlog.owner_alive() or time.monotonic() >= deadline
                                   or cloud_send_admission.tightened(reserved_caps, cloud_send_admission.caps(now)) or cloud_policy.setting_block_reason(now)
                                   or cloud_policy.block_reason(ledger, now, route, project_id, model=self.model))
                    except Exception:
                        raise CloudObservationIncomplete("cloud_admission_unverified") from None
                    if waiting:
                        raise CloudObservationIncomplete("cloud_admission_unavailable")
                denied = denied or not current()
            except CloudObservationIncomplete:
                raise
            except CloudFallback:
                raise
            except Exception:
                raise CloudObservationIncomplete("cloud_guard_unverified") from None
            if denied:
                self._denial_proven = changed_scope
                raise CloudFallback("cloud_unit_changed")
            if inference and dispatch_current is not None:
                try:
                    allowed = dispatch_current() is True
                except Exception:
                    allowed = False
                if not allowed:
                    refuse('pre_send', 'checkpoint_unverified')
                    raise cloud_client.CloudNotSent("not_sent", "checkpoint_unverified")
        self.guard = lambda: dispatch(guard)
        def commit_guard():
            dispatch(lambda: guard(inference=False), result=True)
            for child in self._children:
                child.commit_guard()
        self.commit_guard = commit_guard
        def spawn_quality_repair():
            self._effect_started = True   # a started repair is work, even before its child sends
            if route != "reextract" or purpose != "backlog":
                raise CloudFallback("cloud_repair_invalid")
            child = CloudInferenceUnit(ledger, raw_cfg, route=route, project_id=project_id,
                unit_key=unit_key, deadline=deadline, current=current, dispatch=dispatch,
                message_id=message_id, owner=owner, discard_reason=discard_reason,
                max_attempts=max_attempts, reserve_dispatch=reserve_dispatch, transport=transport,
                purpose="quality_repair", cache_heads=self._cache_heads, input_binding=self.input_binding,
                dispatch_current=dispatch_current)
            self._children.append(child)
            return child
        self.spawn_quality_repair = spawn_quality_repair
        def reserve(body, timeout):
            self._effect_started = True   # before anything can commit and then fail to return
            # every new attempt (retry, repair, next chunk) is judged against the
            # budget in force now, not the one the unit started under
            try:
                current_cfg = {"cloud_llm": _current_cloud_settings()}
                current_caps = cloud_send_admission.caps(current_cfg["cloud_llm"])
            except CloudFallback:
                raise
            except Exception:
                raise CloudObservationIncomplete("cloud_config_unverified") from None
            reserved_caps.clear()
            reserved_caps.update(current_caps)
            hook = {"on_reserved_tx": on_reserved_tx} if on_reserved_tx is not None else {}
            try:
                return cloud_policy.usage_reserver(ledger, current_cfg, route=route, project_id=project_id,
                    unit_key=unit_key, message_id=self.message_id, owner=self.owner,
                    selection=self.selection, input_binding=self._input_binding(), **hook)(body, timeout)
            except cloud_client.CloudError as error:
                history = getattr(error, "history_refusal", None)
                if history is not None:
                    refuse("history", history if type(history) is str else "unknown")
                else:
                    # A kind shares names with block codes but not their meaning: only a
                    # declared detail, or an unverified price, names the stop.
                    detail = error.detail if type(error.detail) is str else None
                    code = (detail if detail in cloud_send_admission.PRE_SEND_VERDICTS
                            else error.kind if error.kind == "pricing_unverified" else "unknown")
                    refuse("pre_send", self._blocked_code(code, settings=current_cfg["cloud_llm"]))
                raise
        self.reserve = lambda body, timeout: (reserve_dispatch or dispatch)(lambda: reserve(body, timeout))
        def record(reservation, metadata, *, answer=None):
            # The callback also settles a reservation rejected before sending.
            # Capture the other cases before owner-thread dispatch: acquiring the
            # owner or saving accounting can fail after the HTTP attempt.
            not_sent = metadata.get("http_class") == "not_sent"
            if not not_sent:
                self.possibly_sent = True
                # Named before the write: saving a 200 without usage raises.
                refuse("after_send", _after_send_code(metadata))
            unbilled = cloud_policy._confirmed_rejection(metadata, True, self.model)
            def write():
                try:
                    guard(inference=False)
                except CloudObservationIncomplete:
                    # Unknown readiness does not prove the paid response lost its scope.
                    outcome = dict(metadata)
                except CloudFallback:
                    if unbilled or not self._proven_change():
                        # Nothing billed to close, or no proof the answer went stale:
                        # keep it recoverable instead of closing the unit for good.
                        outcome = dict(metadata)
                    else:
                        if self.final_disposition is None:
                            self.final_disposition = discard_reason() if discard_reason else "discarded_scope"
                        outcome = {**metadata, "disposition": self.final_disposition}
                else:
                    outcome = dict(metadata)
                value = _save_result(lambda: cloud_policy.record_result(ledger, reservation, outcome,
                    **({"answer": answer} if answer is not None else {})))
                if (isinstance(metadata.get('http_class'), str)
                        and re.fullmatch(r'http_[1-5][0-9]{2}', metadata['http_class'])
                        # A 200 without usage is charged at the maximum and unsettled in the
                        # receipts: the unit stays held like a send with no response.
                        and not (metadata['http_class'] == 'http_200' and metadata.get('usage') is None)):
                    self._unresolved = False
                if unbilled:
                    self._unbilled.append(reservation)
                self.completed_reservations.append(reservation)
                if answer is not None:
                    self.answered_reservations.append(reservation)
                return value
            return dispatch(write, result=True)
        self.result = record
        client_args = {} if max_attempts is None else {"max_attempts": max_attempts}
        if transport is not None:
            client_args["post_fn"] = lambda body, timeout: transport(
                self.client.request, "POST", body, timeout, deadline=self.deadline,
                max_bytes=cloud_client.response_limit(body.get("max_tokens", body.get("max_output_tokens"))))
            client_args["wait_fn"] = lambda delay: transport(time.sleep, delay)
        client = provider_registry.PROVIDER_MODULES[resolved["provider"]].client_class()
        if resolved["provider"] == "openai" or resolved["effort"] is not None:
            # The original Haiku contract keeps its unchanged constructor call.
            client_args.update(model=self.model, effort=resolved["effort"])
        self.client = client(key, **client_args)

    def _blocked_code(self, reason, cfg=None, *, settings=None):
        """A send-block code named for the classifier; ``cfg`` is the raw configuration the stop
        was observed under and ``settings`` the resolved settings it was judged under. Only the
        codes cloud_policy refines read them, loading the configuration when ``cfg`` is absent."""
        if type(reason) is not str:
            return "unknown"
        if reason not in cloud_policy.REFINED_CODES:
            return reason
        try:
            return cloud_policy.refusal_code(load_config() if cfg is None else cfg, reason,
                                             model=self.model, root=HOME, settings=settings,
                                             route=self.route, project_id=self.project_id)
        except Exception:
            return {"window_history_unverified": "window_history_pruned", "scope": "unknown"}.get(reason, reason)

    def discard(self):
        """Append the unit's final disposition without mutating financial results."""
        for child in self._children:
            child.discard()
        # A not-sent or confirmed-rejected reservation billed nothing and stays retryable.
        reservations = [r for r in self.completed_reservations if r not in self._unbilled]
        if not reservations or self.discarded:
            return
        def write():
            if not self._proven_change():
                return
            if self.ledger.db.in_transaction:
                raise RuntimeError("cloud_disposition_owner_transaction_active")
            with self.ledger.db:
                self.ledger.db.execute("BEGIN IMMEDIATE")
                reason = self.final_disposition or (self.discard_reason() if self.discard_reason else "discarded_scope")
                for reservation in reservations:
                    cloud_policy.record_disposition_tx(self.ledger, reservation, reason)
            self.final_disposition = reason
            self.discarded = True
        return self.dispatch(write, result=True)

    def _proven_change(self):
        """Only a proven scope or input change closes a paid answer; an
        unverifiable currentness or an update fence leaves it recoverable."""
        if self._denial_proven or self._changed is None:
            return True
        try:
            return self._changed() is True
        except Exception:
            return False

    @property
    def possibly_sent(self):
        return self._possibly_sent or any(child.possibly_sent for child in self._children)

    @possibly_sent.setter
    def possibly_sent(self, value):
        self._possibly_sent = value
        self._unresolved = bool(value)

    @property
    def unresolved(self):
        """Keep ambiguous sends closed across local fallback and child repair calls."""
        return self._unresolved or any(child.unresolved for child in self._children)

    @property
    def effect_started(self):
        return self._effect_started or any(child.effect_started for child in self._children)

    @property
    def replacement_eligible(self):
        """This whole unit (every chunk and repair child) only met the guard's verified
        refusal of an already settled same request and never began new work, so a
        same-pass runner may give its slot to the next candidate. Grants no resend."""
        return self._verified_repeat and not self.effect_started

    def __call__(self, prompt, *, deadline, max_tokens, **unused):
        observation = ({'recovery_path': 'unknown'}
                       if self.route == 'reextract' and self.message_id is not None else None)
        self.refusal = None
        try:
            http_deadline = min(self.deadline, deadline) - COMMIT_MARGIN
            if time.monotonic() >= http_deadline:
                raise CloudObservationIncomplete("cloud_unit_deadline")
            # the fixed extraction heads are long enough to cache; the small repair head is not
            prefix = next((head for head in self._cache_heads if prompt.startswith(head)), None)
            selected_tokens = self.selection["max_tokens"]
            tokens = max_tokens if selected_tokens is None else selected_tokens
            def recover():
                if self.input_binding is None:
                    cloud_policy._observe_recovery(observation, 'skipped_binding')
                    return None
                self.commit_guard()
                try:
                    return cloud_policy.recover_response(self.ledger, route=self.route,
                        project_id=self.project_id, message_id=self.message_id, unit_key=self.unit_key,
                        input_binding=self._input_binding(),
                        request_fp=mcs_util.payload_hash(self.client.body(prompt, tokens, prefix)),
                        owner=self.owner, scope=self.scope,
                        **({'observation': observation} if observation is not None else {}))
                except cloud_client.CloudNotSent:
                    raise
                except (ValueError, TypeError, KeyError, sqlite3.Error, cloud_client.CloudError) as error:
                    _history_read_error("recover_response", error)
                    # Only a reason recover_response proves names the stop; a read error
                    # (a locked database, say) may be gone by the next pass.
                    reason = error.args[0] if type(error) is ValueError and len(error.args) == 1 else None
                    self._refuse("history", reason if type(reason) is str and reason in _HISTORY_ERROR_REASONS
                                 else "unknown")
                    # An unreadable recovery history cannot prove no prior
                    # paid send: hold the unit like a blocked send check.
                    self.possibly_sent = True
                    raise cloud_client.CloudNotSent("budget_exceeded", "cloud_unit_unresolved") from None
            saved = self.dispatch(recover, result=True)
            if saved is not None:
                self._effect_started = True   # a recovered paid answer is used
                evidence, answer = saved
                answer["reused_response"] = True
                self.possibly_sent = True
                self._unresolved = False
                self.refusal = None
                if evidence['reservation'] not in self.completed_reservations:
                    self.completed_reservations.append(evidence['reservation'])
                if evidence['reservation'] not in self.answered_reservations:
                    self.answered_reservations.append(evidence['reservation'])
            else:
                judged = {}
                def send_block_reason():
                    # An exact recovery miss is not proof of no prior send: old
                    # envelopes and incomplete receipts use the reservation guard.
                    self.commit_guard()
                    try:
                        judged["cloud_llm"] = _current_cloud_settings()
                        return cloud_policy.request_send_block_reason(self.ledger,
                            {"cloud_llm": judged["cloud_llm"]}, self.client.body(prompt, tokens, prefix),
                            route=self.route, project_id=self.project_id, message_id=self.message_id,
                            unit_key=self.unit_key, owner=self.owner, selection=self.selection,
                            input_binding=self._input_binding())
                    except (ValueError, TypeError, KeyError, sqlite3.Error, cloud_client.CloudError) as error:
                        _history_read_error("request_send_block_reason", error, observation=observation)
                        # Only the guard's own classification names the stop; any other
                        # error (a locked database, say) may be gone by the next pass.
                        refusal = getattr(error, "history_refusal", None)
                        self._refuse("history", refusal if type(refusal) is str else "unknown")
                        self.possibly_sent = True
                        if getattr(error, "verified_repeat", False) is True:
                            self._verified_repeat = True
                        raise cloud_client.CloudNotSent("budget_exceeded", "cloud_unit_unresolved") from None
                blocked = self.dispatch(send_block_reason, result=True)
                if blocked:
                    self._refuse("pre_send", self._blocked_code(blocked, settings=judged.get("cloud_llm")))
                    # Only a history-checked fresh request may use caller local fallback.
                    self.commit_guard()
                    _send_blocked(blocked)
                    raise CloudFallback("cloud_send_blocked")
                capacity = _current_cloud_settings()["max_concurrency"]
                self._effect_started = True   # admission to new request work
                with cloud_backlog.request_slot(HOME, capacity) as held:
                    if not held:
                        self._refuse("pre_send", "cloud_capacity")
                        raise CloudBusy("cloud_capacity")
                    answer = self.client.complete(prompt, deadline=http_deadline, max_tokens=tokens,
                        before_attempt=self.guard, reserve_fn=self.reserve, after_result=self.result,
                        cache_prefix=prefix)
            self.commit_guard()
            return answer
        except CloudObservationIncomplete:
            raise
        except CloudFallback as error:
            if str(error) == "cloud_unit_changed":
                self.discard()
            raise
        except writer_lock.RunLockLost:
            raise
        except cloud_client.CloudNotSent:
            raise
        except Exception as exc:
            # Client errors contain fixed categories; no remote body enters local errors.
            raise CloudFallback("cloud_unit_failed") from exc



def _client_class(selection):
    return provider_registry.PROVIDER_MODULES[selection['provider']].client_class()


def _context_local_request(ledger, cfg, prompt, *, project_id, message_id, unit_key, owner):
    """The wire body the selected profile's client would send for this patient-context prompt, built on demand."""
    def request():
        from types import SimpleNamespace
        settings = resolve_cloud_settings(cfg)
        selection, errors = cloud_policy.resolve_profile(
            cfg, route='patient_context', purpose='patient_context', root=HOME,
            ledger=ledger, ordered=True)
        if selection is None:
            raise cloud_client.CloudError('budget_exceeded', 'cloud_unit_unresolved')
        tokens = selection['max_tokens'] if selection['max_tokens'] is not None else 1600
        body = _client_class(selection).body(
            SimpleNamespace(model=selection['model'], effort=selection['effort']), prompt, tokens, None)
        candidate = cloud_policy._request_identity(settings, body, route='patient_context',
            project_id=project_id, message_id=message_id, unit_key=unit_key,
            owner=owner, selection=selection, input_binding=None)
        return candidate, body
    return request


def guard_context_local(ledger, cfg, prompt, *, project_id, message_id, unit_key, owner,
                        allow_supersede=False):
    """Check patient-context paid history against the wire body the selected profile's client would send.

    ``allow_supersede`` also passes what :func:`supersede_context_local` would record as
    replaced, and returns those reservation ids; the check itself writes nothing."""
    return cloud_policy.guard_local_fallback(ledger, route='patient_context', project_id=project_id,
        message_id=message_id, unit_key=unit_key, allow_supersede=allow_supersede,
        request_factory=_context_local_request(ledger, cfg, prompt, project_id=project_id,
            message_id=message_id, unit_key=unit_key, owner=owner))


def supersede_context_local(ledger, cfg, prompt, *, project_id, message_id, unit_key, owner):
    """Record this unit's paid requests of the same prompt as replaced by local work.

    Runs inside the caller's transaction, beside the local result it commits with; returns
    the newly written disposition rows (see :func:`cloud_policy.supersede_local_tx`)."""
    return cloud_policy.supersede_local_tx(ledger, route='patient_context', project_id=project_id,
        message_id=message_id, unit_key=unit_key,
        request_factory=_context_local_request(ledger, cfg, prompt, project_id=project_id,
            message_id=message_id, unit_key=unit_key, owner=owner))


def guard_reextract_local(ledger, cfg, prompt, *, project_id, message_id, unit_key,
                          owner, input_binding):
    """guard_local_fallback for the superseded local migration (reextract).

    The common path keeps its accounting snapshot, verified history, tally
    and repeated-request checks; this entry's stricter rule lives in the
    request factory: every retained request must carry a valid
    logical_request_fp that differs from this request's. A same, absent or
    malformed fingerprint always holds, even where the generic guard would
    allow a confirmed rejection. Empty verified history never builds a
    candidate, matching guard_local_fallback."""
    def request():
        rows = cloud_policy._verified_request_history(
            ledger, route='reextract', project_id=project_id,
            message_id=message_id, unit_key=unit_key)
        settings = resolve_cloud_settings(cfg)
        selection, errors = cloud_policy.resolve_profile(
            cfg, route='reextract', purpose='backlog', root=HOME, ledger=ledger, ordered=True)
        if selection is None:
            raise cloud_client.CloudError('budget_exceeded', 'cloud_unit_unresolved')
        tokens = selection['max_tokens'] if selection['max_tokens'] is not None else 1600
        from types import SimpleNamespace
        body = _client_class(selection).body(
            SimpleNamespace(model=selection['model'], effort=selection['effort']), prompt, tokens, None)
        candidate = cloud_policy._request_identity(
            settings, body, route='reextract', project_id=project_id,
            message_id=message_id, unit_key=unit_key, owner=owner,
            selection=selection, input_binding=input_binding)
        new_fp = candidate.get('logical_request_fp')
        for row in rows:
            meta = cloud_policy._receipt_meta(row['meta'])
            fp = meta.get('logical_request_fp') if isinstance(meta, dict) else None
            if (not isinstance(fp, str) or re.fullmatch(r'[0-9a-f]{64}', fp) is None
                    or fp == new_fp):
                raise cloud_client.CloudError('budget_exceeded', 'cloud_unit_unresolved')
        return candidate, body
    cloud_policy.guard_local_fallback(ledger, route='reextract', project_id=project_id,
        message_id=message_id, unit_key=unit_key, request_factory=request)


def resolve_cloud_settings(cfg):
    if not cfg:
        # an unreadable config (load_config's {}) never auto-enables the paid
        # route: a real config always carries mcs_login_id, so {} means unread
        return cloud_policy.cloud_config({})[0]
    return cloud_policy.resolve_settings(cfg, HOME)[0]


# Reuse key: the provider and its fixed request shape. The inference rule
# versions live in each caller's own contract (EXTRACT_VERSION, the context
# CONTRACT/plan/rule), so a rule change rebuilds every patient while code
# releases that leave the rules alone do not re-send or hide results.
_CLOUD_CONTRACT = anthropic_client.provider_contract(*anthropic_client.ORIGINAL_CONTRACT)


def cloud_provider_contract(cfg=None, *, route="reextract", purpose="backlog", selection=None):
    if selection is None:
        cfg = load_config() if cfg is None else cfg
        selection, errors = cloud_policy.resolve_profile(cfg, route=route, purpose=purpose, root=HOME)
        if errors or selection is None:
            raise CloudFallback("cloud_profile_invalid")
    module = provider_registry.provider_module(selection["provider"])
    if module is None or (selection["model"], selection.get("effort")) not in module.VALID_MODEL_EFFORTS:
        raise CloudFallback("cloud_profile_invalid")
    return module.provider_contract(selection["model"], selection.get("effort"))


def supported_cloud_contracts(model=None):
    """Enumerate the existing finite provider contracts, optionally for one model."""
    return tuple(module.provider_contract(selected, effort)
                 for module in provider_registry.PROVIDER_MODULES.values()
                 for selected, effort in sorted(module.VALID_MODEL_EFFORTS, key=lambda pair: (pair[0], pair[1] or ""))
                 if model is None or selected == model)


def is_supported_cloud_contract(contract):
    return isinstance(contract, dict) and contract in supported_cloud_contracts(contract.get("model"))


def verify_cloud_repair_evidence(db, *, reservation_artifact_id, reservation_id, route,
                                  project_id, message_id, unit_key, owner, prompt, answer_text, contract,
                                  purpose="quality_repair", finish_reason="stop"):
    """Validate one repaired answer against its settled request and exact response."""
    if (type(reservation_artifact_id) is not int or not isinstance(reservation_id, str)
            or not isinstance(unit_key, str) or not unit_key or not isinstance(owner, dict)
            or not isinstance(prompt, str) or not isinstance(answer_text, str)
            or purpose not in ("backlog", "patient_context", "quality_repair")
            or finish_reason not in ("stop", "length")
            or not is_supported_cloud_contract(contract)):
        raise ValueError("cloud_repair_evidence_invalid")
    try:
        encoded = answer_text.encode("utf-8")
        evidence = cloud_policy.reservation_evidence(db, reservation_artifact_id)
        reservation, result = evidence["reservation"], evidence["result"]
        if len(encoded) > cloud_client.response_limit(reservation["max_tokens"]):
            raise ValueError("cloud_repair_evidence_invalid")
        selected = {"provider": contract["backend"], "model": reservation["model"],
                    "effort": reservation["effort"]}
        profile = reservation["profile"]
        expected_request = cloud_policy._logical_request_fp({"instructions": cloud_client.SYSTEM, "input": prompt})
        if (reservation["reservation_id"] != reservation_id or reservation["route"] != route
                or reservation["project_id"] != project_id or reservation["message_id"] != message_id
                or reservation["unit_key"] != unit_key or reservation["owner"] != owner
                or reservation["logical_request_fp"] != expected_request
                or cloud_provider_contract(selection=selected) != contract
                or selected["provider"] == "openai" and profile is not None
                and openai_client.PROFILE_DEFINITIONS.get(profile) != (selected["model"], selected["effort"])
                or selected["provider"] == "anthropic" and not cloud_policy.profile_contract_valid(
                    "anthropic", selected["model"], selected["effort"], profile, reservation["max_tokens"])
                or profile is not None
                and reservation["max_tokens"] != {"backlog": 8192, "patient_context": 16384,
                                                   "quality_repair": 32768}[purpose]
                or result.get("answer_text_sha256_v1") != hashlib.sha256(encoded).hexdigest()
                or result.get("answer_finish_reason_v1") != finish_reason):
            raise ValueError("cloud_repair_evidence_invalid")
        return evidence
    except (KeyError, TypeError, UnicodeError, sqlite3.Error, CloudFallback) as error:
        raise ValueError("cloud_repair_evidence_invalid") from error
