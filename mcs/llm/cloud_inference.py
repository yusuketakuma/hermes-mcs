"""Own the shared cloud-provider contract and guarded send lifecycle for LLM callers."""
from __future__ import annotations

import copy
import hashlib
import re
import sqlite3
import time

import anthropic_client
import openai_client
import bounded_http
import cloud_backlog
import cloud_client
import cloud_policy
import mcs_util
import writer_lock
from mcs_util import HOME, load_config

COMMIT_MARGIN = 5


def _history_read_error(phase, error):
    try:
        if type(phase) is not str or phase not in ("recover_response", "request_send_block_reason"):
            return
        category = next((name for cls, name in (
            (sqlite3.Error, "sqlite_error"), (ValueError, "value_error"),
            (TypeError, "type_error"), (KeyError, "key_error"),
            (cloud_client.CloudError, "cloud_error")) if isinstance(error, cls)), "read_error")
        values = {"phase": phase, "error_category": category}
        if isinstance(error, sqlite3.Error):
            code = cloud_backlog._exception_observation(error).get("sqlite_errorcode")
            values["sqlite_primary_code"] = code & 0xff if type(code) is int else "unknown"
        cloud_backlog._event("cloud_history_read_error", **values)
    except Exception:
        pass  # Diagnostics must never replace the existing fail-closed refusal.


class CloudFallback(Exception):
    """Discard this provider's unit and retry under the local contract."""


class CloudBusy(CloudFallback):
    """No shared cloud capacity was reserved or sent."""


class CloudObservationIncomplete(CloudFallback):
    """Defer adoption without discarding a received answer or starting a fallback."""


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


class CloudInferenceUnit:
    """Bind one unit's guarded HTTP attempts to the ledger's owning thread."""

    def __init__(self, ledger, cfg, *, route, project_id, unit_key, deadline,
                 current, dispatch, message_id=None, owner=None, discard_reason=None,
                 max_attempts=None, on_reserved_tx=None, reserve_dispatch=None, transport=None,
                 purpose=None, selection=None, cache_heads=(), input_binding=None, dispatch_current=None):
        purpose = purpose or ("patient_context" if route == "patient_context" else "backlog")
        raw_cfg = copy.deepcopy(cfg)
        resolved, errors = cloud_policy.resolve_profile(cfg, route=route, purpose=purpose, root=HOME)
        if errors or resolved is None or selection is not None and dict(selection) != dict(resolved):
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
        self._children = []
        self.message_id = message_id
        self.owner = owner
        self.input_binding = input_binding
        self._input_binding = lambda: (mcs_util.payload_hash([self.input_binding, scope])
                                      if self.input_binding is not None else None)
        self.route, self.project_id, self.unit_key = route, project_id, unit_key
        self._possibly_sent = False
        self._unresolved = False
        self.discard_reason = discard_reason
        self.ledger = ledger
        self.deadline = deadline
        self.discarded = False
        self.final_disposition = None

        reserved_caps = {}

        def caps(settings):
            out = {k: settings[k] for k in ("daily_request_budget", "daily_cost_limit_usd",
                                            "monthly_cost_limit_usd", "window_budget_usd")}
            # an earlier period start counts more spending, like a lower cap
            start = settings["window_start"]
            out["window_start"] = None if start is None else mcs_util.until_epoch(start)
            budgets = settings["provider_budgets"]
            provider_caps = budgets.get(settings["provider"], {})
            for key in ("daily_cost_limit_usd", "monthly_cost_limit_usd"):
                out["provider_" + key] = provider_caps.get(key, 0 if budgets else None)
            return out

        def tightened(now):
            # a cap set or lowered after this attempt's reservation stops the send it
            # has not made yet; the reservation itself stays counted at its maximum
            return any(reserved_caps.get(k, now[k]) is None and now[k] is not None
                       or None not in (reserved_caps.get(k), now[k]) and now[k] < reserved_caps[k]
                       for k in now)

        def guard(*, inference=True):
            # A denial observed after the response is sticky for this unit:
            # restoring the old source/scope cannot make its response current.
            if self.final_disposition is not None:
                raise CloudFallback("cloud_unit_changed")
            try:
                live = load_config()
                if not isinstance(live, dict) or not live:
                    raise CloudObservationIncomplete('cloud_config_unverified')
                if isinstance(live.get('cloud_llm'), dict) and live['cloud_llm'].get('mode') == 'off':
                    raise CloudFallback('cloud_unit_changed')
                now = resolve_cloud_settings(live)
                chosen, errors = cloud_policy.resolve_profile(live, route=route, purpose=purpose, root=HOME)
                if errors or chosen is None:
                    raise CloudObservationIncomplete('cloud_profile_unverified')
                live_key = cloud_policy.selected_key(HOME, {"cloud_llm": now})
                if not live_key:
                    raise CloudObservationIncomplete('cloud_key_unverified')
                # The run window gates each new POST (retries included); a result that
                # already arrived is still judged only by scope, not by the clock.
                denied = (dict(chosen) != dict(self.selection)
                          or writer_lock.updater_state() != updater
                          or updater[0] or live_key != key
                          or cloud_policy.scope_generation({"cloud_llm": now}) != scope
                          # A paid result is kept while its scope holds, even if the send window closes.
                          or not cloud_policy.eligible(now, route, project_id))
                if not denied and inference:
                    try:
                        waiting = (not cloud_backlog.owner_alive() or time.monotonic() >= deadline
                                   or tightened(caps(now)) or cloud_policy.setting_block_reason(now)
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
                raise CloudFallback("cloud_unit_changed")
            if inference and dispatch_current is not None:
                try:
                    allowed = dispatch_current() is True
                except Exception:
                    allowed = False
                if not allowed:
                    raise cloud_client.CloudNotSent("not_sent", "checkpoint_unverified")
        self.guard = lambda: dispatch(guard)
        def commit_guard():
            dispatch(lambda: guard(inference=False), result=True)
            for child in self._children:
                child.commit_guard()
        self.commit_guard = commit_guard
        def spawn_quality_repair():
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
            # every new attempt (retry, repair, next chunk) is judged against the
            # budget in force now, not the one the unit started under
            try:
                current_cfg = {"cloud_llm": _current_cloud_settings()}
                current_caps = caps(current_cfg["cloud_llm"])
            except CloudFallback:
                raise
            except Exception:
                raise CloudObservationIncomplete("cloud_config_unverified") from None
            reserved_caps.clear()
            reserved_caps.update(current_caps)
            hook = {"on_reserved_tx": on_reserved_tx} if on_reserved_tx is not None else {}
            return cloud_policy.usage_reserver(ledger, current_cfg, route=route, project_id=project_id,
                unit_key=unit_key, message_id=self.message_id, owner=self.owner,
                selection=self.selection, input_binding=self._input_binding(), **hook)(body, timeout)
        self.reserve = lambda body, timeout: (reserve_dispatch or dispatch)(lambda: reserve(body, timeout))
        def record(reservation, metadata, *, answer=None):
            # The callback also settles a reservation rejected before sending.
            # Capture the other cases before owner-thread dispatch: acquiring the
            # owner or saving accounting can fail after the HTTP attempt.
            not_sent = metadata.get("http_class") == "not_sent"
            if not not_sent:
                self.possibly_sent = True
            def write():
                try:
                    guard(inference=False)
                except CloudObservationIncomplete:
                    # Unknown readiness does not prove the paid response lost its scope.
                    outcome = dict(metadata)
                except CloudFallback:
                    if self.final_disposition is None:
                        self.final_disposition = discard_reason() if discard_reason else "discarded_scope"
                    outcome = {**metadata, "disposition": self.final_disposition}
                else:
                    outcome = dict(metadata)
                value = cloud_policy.record_result(ledger, reservation, outcome,
                    **({"answer": answer} if answer is not None else {}))
                if isinstance(metadata.get('http_class'), str) and re.fullmatch(r'http_[1-5][0-9]{2}', metadata['http_class']):
                    self._unresolved = False
                self.completed_reservations.append(reservation)
                return value
            return dispatch(write, result=True)
        self.result = record
        client_args = {} if max_attempts is None else {"max_attempts": max_attempts}
        if transport is not None:
            client_args["post_fn"] = lambda body, timeout: transport(
                self.client.request, "POST", body, timeout, deadline=self.deadline)
            client_args["wait_fn"] = lambda delay: transport(time.sleep, delay)
        if resolved["provider"] == "openai":
            client = openai_client.OpenAIClient
            client_args.update(model=self.model, effort=resolved["effort"])
        else:
            client = anthropic_client.AnthropicClient
        self.client = client(key, **client_args)

    def discard(self):
        """Append the unit's final disposition without mutating financial results."""
        for child in self._children:
            child.discard()
        if not self.completed_reservations or self.discarded:
            return
        def write():
            if self.ledger.db.in_transaction:
                raise RuntimeError("cloud_disposition_owner_transaction_active")
            with self.ledger.db:
                self.ledger.db.execute("BEGIN IMMEDIATE")
                reason = self.final_disposition or (self.discard_reason() if self.discard_reason else "discarded_scope")
                for reservation in self.completed_reservations:
                    cloud_policy.record_disposition_tx(self.ledger, reservation, reason)
            self.final_disposition = reason
            self.discarded = True
        return self.dispatch(write, result=True)

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

    def __call__(self, prompt, *, deadline, max_tokens, **unused):
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
                    return None
                self.commit_guard()
                try:
                    return cloud_policy.recover_response(self.ledger, route=self.route,
                        project_id=self.project_id, message_id=self.message_id, unit_key=self.unit_key,
                        input_binding=self._input_binding(),
                        request_fp=mcs_util.payload_hash(self.client.body(prompt, tokens, prefix)),
                        owner=self.owner, scope=self.scope)
                except (ValueError, TypeError, KeyError, sqlite3.Error, cloud_client.CloudError) as error:
                    _history_read_error("recover_response", error)
                    raise cloud_client.CloudNotSent("budget_exceeded", "cloud_unit_unresolved") from None
            saved = self.dispatch(recover, result=True)
            if saved is not None:
                evidence, answer = saved
                answer["reused_response"] = True
                self.possibly_sent = True
                self._unresolved = False
                if evidence['reservation'] not in self.completed_reservations:
                    self.completed_reservations.append(evidence['reservation'])
            else:
                def send_block_reason():
                    # An exact recovery miss is not proof of no prior send: old
                    # envelopes and incomplete receipts use the reservation guard.
                    self.commit_guard()
                    try:
                        return cloud_policy.request_send_block_reason(self.ledger,
                            {"cloud_llm": _current_cloud_settings()}, self.client.body(prompt, tokens, prefix),
                            route=self.route, project_id=self.project_id, message_id=self.message_id,
                            unit_key=self.unit_key, owner=self.owner, selection=self.selection,
                            input_binding=self._input_binding())
                    except (ValueError, TypeError, KeyError, sqlite3.Error, cloud_client.CloudError) as error:
                        _history_read_error("request_send_block_reason", error)
                        self.possibly_sent = True
                        raise cloud_client.CloudNotSent("budget_exceeded", "cloud_unit_unresolved") from None
                if self.dispatch(send_block_reason, result=True):
                    # Only a history-checked fresh request may use caller local fallback.
                    self.commit_guard()
                    raise CloudFallback("cloud_send_blocked")
                capacity = _current_cloud_settings()["max_concurrency"]
                with cloud_backlog.request_slot(HOME, capacity) as held:
                    if not held:
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
_CLOUD_CONTRACT = {"backend": "anthropic", "model": anthropic_client.MODEL,
                   "prompt": mcs_util.payload_hash({"system": anthropic_client.SYSTEM, "effort": "low"})[:16]}


def cloud_provider_contract(cfg=None, *, route="reextract", purpose="backlog", selection=None):
    if selection is None:
        cfg = load_config() if cfg is None else cfg
        selection, errors = cloud_policy.resolve_profile(cfg, route=route, purpose=purpose, root=HOME)
        if errors or selection is None:
            raise CloudFallback("cloud_profile_invalid")
    if selection["provider"] == "openai":
        if (selection["model"], selection["effort"]) not in openai_client.VALID_MODEL_EFFORTS:
            raise CloudFallback("cloud_profile_invalid")
        return {"backend": "openai", "model": selection["model"],
                "prompt": mcs_util.payload_hash({"system": anthropic_client.SYSTEM, "effort": selection["effort"],
                                               "service_tier": "default", "cache_ttl": "30m"})[:16]}
    if selection["provider"] != "anthropic" or selection["model"] != anthropic_client.MODEL:
        raise CloudFallback("cloud_profile_invalid")
    return dict(_CLOUD_CONTRACT)


def supported_cloud_contracts(model=None):
    """Enumerate the existing finite provider contracts, optionally for one model."""
    contracts = [dict(_CLOUD_CONTRACT)] if model in (None, _CLOUD_CONTRACT["model"]) else []
    contracts.extend(cloud_provider_contract(selection={"provider": "openai", "model": selected, "effort": effort})
                     for selected, effort in sorted(openai_client.VALID_MODEL_EFFORTS)
                     if model is None or selected == model)
    return tuple(contracts)


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
        if len(encoded) > bounded_http.MAX_RESPONSE_BYTES:
            raise ValueError("cloud_repair_evidence_invalid")
        evidence = cloud_policy.reservation_evidence(db, reservation_artifact_id)
        reservation, result = evidence["reservation"], evidence["result"]
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
                or selected["provider"] == "openai" and profile is not None
                and reservation["max_tokens"] != {"backlog": 8192, "patient_context": 16384,
                                                   "quality_repair": 32768}[purpose]
                or selected["provider"] == "anthropic" and profile is not None
                or result.get("answer_text_sha256_v1") != hashlib.sha256(encoded).hexdigest()
                or result.get("answer_finish_reason_v1") != finish_reason):
            raise ValueError("cloud_repair_evidence_invalid")
        return evidence
    except (KeyError, TypeError, UnicodeError, sqlite3.Error, CloudFallback) as error:
        raise ValueError("cloud_repair_evidence_invalid") from error
