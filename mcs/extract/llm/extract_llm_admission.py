"""Observe extractor admission and call statistics without performing inference."""
import json
import sys
import semantic_conversion
import extract_quality


_COUNT_KEYS = ("calls", "first_calls", "repair_calls", "repair_wins",
               "length_stops", "length_resplits", "probe_runs",
               "format_downgrades", "checkpoint_reuses")


def _llm_call_stats(metas) -> dict | None:
    """Sum the fixed count keys, prompt_ms/predicted_ms/tokens, the
    prompt_n/cache_n pairs (first and repair calls; only metas reporting
    both) and the repair_issues histogram over per-row metas (a batch
    meta shared by several rows counts once); a field no meta reported
    is None, and all-None collapses to None. cache_hit_ratio =
    Σcache_n/(Σcache_n+Σprompt_n) over the metas that reported both,
    None otherwise."""
    llm_calls = dict.fromkeys(_COUNT_KEYS, 0)
    llm_calls.update({"prompt_ms": 0.0, "predicted_ms": 0.0, "tokens": 0,
                      "prompt_n": 0, "cache_n": 0,
                      "repair_prompt_n": 0, "repair_cache_n": 0,
                      "cache_hit_ratio": None, "repair_issues": None})
    have = set()
    seen_meta = set()

    def finite(value):
        return type(value) in (int, float) and 0 <= value <= sys.float_info.max

    for meta in metas:
        if not isinstance(meta, dict) or id(meta) in seen_meta:
            continue
        seen_meta.add(id(meta))
        for key in _COUNT_KEYS:
            if type(meta.get(key)) is int:
                llm_calls[key] += meta[key]
                have.add(key)
        timings = meta.get("timings")
        if isinstance(timings, dict):
            for key in ("prompt_ms", "predicted_ms"):
                if finite(timings.get(key)):
                    llm_calls[key] += timings[key]
                    have.add(key)
        for source, prefix in (("timings", ""), ("repair_timings", "repair_")):
            pair = meta.get(source)
            if isinstance(pair, dict) and finite(pair.get("prompt_n")) \
                    and finite(pair.get("cache_n")):
                llm_calls[prefix + "prompt_n"] += pair["prompt_n"]
                llm_calls[prefix + "cache_n"] += pair["cache_n"]
                have.add(prefix + "prompt_n")
        issues = meta.get("repair_issues")
        if isinstance(issues, dict):
            hist = llm_calls["repair_issues"] or dict.fromkeys(extract_quality._ISSUE_KEYS, 0)
            for key in extract_quality._ISSUE_KEYS:
                if type(issues.get(key)) is int:
                    hist[key] += issues[key]
            llm_calls["repair_issues"] = hist
        usage = meta.get("usage")
        if isinstance(usage, dict) \
                and type(usage.get("total_tokens")) is int:
            llm_calls["tokens"] += usage["total_tokens"]
            have.add("tokens")
    for key in (*_COUNT_KEYS, "prompt_ms", "predicted_ms", "tokens"):
        if key not in have or not finite(llm_calls[key]):
            llm_calls[key] = None
    for prefix in ("", "repair_"):
        if prefix + "prompt_n" not in have or not finite(llm_calls[prefix + "prompt_n"]) \
                or not finite(llm_calls[prefix + "cache_n"]):
            llm_calls[prefix + "prompt_n"] = llm_calls[prefix + "cache_n"] = None
    if llm_calls["prompt_n"] is not None:
        total = llm_calls["prompt_n"] + llm_calls["cache_n"]
        if 0 < total < sys.float_info.max:
            llm_calls["cache_hit_ratio"] = round(llm_calls["cache_n"] / total, 3)
    return llm_calls if any(
        v is not None for v in llm_calls.values()) else None


def legacy_admissions(ledger, cfg) -> set | None:
    """Limit canonical legacy inference to explicitly active conversion admissions.

    Shared by the tick and drainer: None is unrestricted for a noncanonical
    fact_source; canonical source uses active_legacy_admissions. A conversion
    cohort alone does not authorize legacy inference.
    A non-dict config, a semantic_config error or any failure fails
    CLOSED (empty set) — an errored canonical block must not fall back
    to legacy and reopen unrestricted v3 inference."""
    try:
        if not isinstance(cfg, dict):
            _admission_closed(["config_not_object"])
            return set()
        import semantic_policy
        policy, error = semantic_policy.runtime_config(cfg)
        if error:
            _admission_closed(error)
            return set()
        if policy.get("fact_source") != "canonical":
            return None
        return semantic_conversion.active_legacy_admissions(ledger)
    except Exception as e:
        _admission_closed([type(e).__name__])
        return set()


def cloud_backlog_admissions(ledger, cfg) -> set | None:
    """Admission set for the resident cloud backlog worker's cloud_only pass.

    A cloud_only pass performs no legacy inference, so the canonical legacy
    closure (conversion cohorts only) must not starve required cloud work:
    under a validated config the receipted human history scope, selected
    contract, holds and paid gates inside run_pending are the admission
    gates — unrestricted (None) is the safe canonical expression. Any config
    error keeps the fail-closed empty set, identical to legacy_admissions."""
    base = legacy_admissions(ledger, cfg)
    if base is None:
        return None
    try:
        if not isinstance(cfg, dict):
            return base
        import semantic_policy
        policy, error = semantic_policy.runtime_config(cfg)
        if error or policy.get("fact_source") != "canonical":
            return base
        return None
    except Exception:
        return base


_admission_closed_last = None


def _admission_closed(reasons) -> None:
    """One stderr event per distinct closure — a closed admission stops
    ALL legacy extraction, so it must never be silent (nor spam the
    resident drainer's loop)."""
    global _admission_closed_last
    reasons = sorted(str(r) for r in reasons)
    if reasons != _admission_closed_last:
        _admission_closed_last = reasons
        print(json.dumps({"event": "admission_closed", "reasons": reasons}),
              file=sys.stderr, flush=True)
