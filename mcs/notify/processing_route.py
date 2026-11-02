"""Identify the route of the fact artifact selected for a visible chat post."""
from __future__ import annotations

import math
import sqlite3

import mcs_util
from mcs_queries import FACT_KINDS_SQL, current_fact_pred
from mcs_util import payload_hash, strict_json

PROVIDERS = ("anthropic", "openai", "jev", "local")
STATE_LABELS = {"done": "実行済み", "partial": "一部実行・一部省略", "skipped": "省略", "unused": "未使用",
                "pending": "処理待ち", "failed": "失敗", "unknown": "不明", "used": "使用あり"}


def visible_providers(root=None):
    """Return services whose private API keys are usable, plus Jev and local."""
    result = []
    for provider, key in (("anthropic", "MCS_ANTHROPIC_API_KEY"),
                          ("openai", "MCS_OPENAI_API_KEY")):
        try:
            if mcs_util.private_env_problem(key, root) is None:
                result.append(provider)
        except Exception:
            pass
    return (*result, "jev", "local")


def aggregate_provider_states(posts):
    if len(posts) == 1:
        return dict(posts[0])
    return {key: ("used" if any(post[key] in ("done", "partial") for post in posts)
                  else "unknown") for key in PROVIDERS}


def _extract_jev_state(db, project_id, message_id, source_id, source_hash, version, content):
    """Read QC execution only for the exact current extraction artifact."""
    import semantic_qc
    import extract_llm
    from inference_version import is_current

    if type(version) is not int or version != extract_llm.EXTRACT_VERSION:
        return "unknown"
    rows = db.execute("SELECT kind,content,meta FROM artifacts WHERE project_id=? AND message_id=? "
                      "AND kind IN ('extract_qc','extract_qc_skipped') ORDER BY artifact_id DESC LIMIT 1025",
                      (project_id, message_id)).fetchall()
    if len(rows) > 1024:
        return "unknown"
    questions, layout, _ = semantic_qc._qc_questions(content)
    for row in rows:
        meta, data = strict_json(row["meta"]), strict_json(row["content"])
        if not isinstance(meta, dict) or not isinstance(data, dict) or not is_current(meta):
            return "unknown"
        if type(meta.get("source_artifact_id")) is not int:
            return "unknown"
        if meta["source_artifact_id"] != source_id or meta.get("hash") != source_hash:
            continue
        if type(meta.get("extract_version")) is not int or meta["extract_version"] != version:
            return "unknown"
        if row["kind"] == "extract_qc":
            coverage, items = data.get("coverage"), data.get("items")
            if (meta.get("qc") != "done" or data.get("qc") != "done"
                    or type(meta.get("jev_requests")) is not int or meta["jev_requests"] <= 0
                    or not isinstance(coverage, dict) or not isinstance(items, list)
                    or type(coverage.get("checked")) is not int
                    or coverage["checked"] != len(items) + int("urgency" in data)
                    or coverage["checked"] != len(layout) or not layout):
                return "unknown"
            expected = {(section, index) for _, section, index in layout if section != "urgency"}
            actual = set()
            for item in items:
                if not isinstance(item, dict) or type(item.get("index")) not in (int, str):
                    return "unknown"
                key = (item.get("section"), item["index"])
                score = item.get("noul")
                if (key not in expected or key in actual or item.get("verdict") not in {"MATCH", "NO_MATCH", "UNDETERMINED"}
                        or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
                    return "unknown"
                actual.add(key)
            if actual != expected:
                return "unknown"
            if any(section == "urgency" for _, section, _ in layout):
                urgency = data.get("urgency")
                score = urgency.get("confidence") if isinstance(urgency, dict) else None
                if (not isinstance(urgency, dict) or urgency.get("jev") not in questions["urg"]["criteria"]
                        or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
                    return "unknown"
            return "done"
        if (data.get("status") != "SKIPPED" or data.get("reason") not in {
                "not_configured", "disabled", "daily_budget", "backlog_reserved", "circuit_open", "service_unavailable"}):
            return "unknown"
        partial = data.get("partial_answers", {})
        if not isinstance(partial, dict) or set(partial) - set(questions):
            return "unknown"
        for qid, answer in partial.items():
            if not isinstance(answer, dict) or answer.get("type") != questions[qid].get("type"):
                return "unknown"
            number = answer.get("noul") if answer["type"] == "noul" else answer.get("confidence")
            if (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1
                    or (answer["type"] == "choice" and answer.get("choice") not in questions[qid].get("criteria", {}))):
                return "unknown"
        return "partial" if partial else "skipped"
    return "unknown"


def provider_states(db, project_id, message_id, cfg=None, *, bundle_cache=None, sequence_out=None):
    """Return only states supported by current per-post execution evidence."""
    states = dict.fromkeys(PROVIDERS, "unknown")
    sequence = None

    def result():
        if sequence_out is not None:
            sequence_out["sequence"] = sequence
        return states

    try:
        row = db.execute(
            "SELECT a.artifact_id,a.kind,a.model,a.meta,a.content,m.content_hash FROM artifacts a JOIN messages m "
            "ON m.message_id=a.message_id AND m.project_id=a.project_id "
            "WHERE a.message_id=? AND m.project_id=? AND a.kind IN (" + FACT_KINDS_SQL + ") "
            "AND m.body_state IS NOT 'deleted' "
            "AND CASE WHEN json_valid(a.content) THEN json_type(a.content)='object' ELSE 0 END "
            + current_fact_pred('a', 'm') + " ORDER BY a.artifact_id DESC LIMIT 1",
            (message_id, project_id)).fetchone()
        if row is None:
            return result()
        meta, content = strict_json(row["meta"]), strict_json(row["content"])
        if not isinstance(meta, dict) or not isinstance(content, dict) or not content:
            return result()
        proof = meta.get("provider_provenance")
        from inference_dependency_loss import source_binding
        binding = source_binding(db, project_id, message_id)
        if (row["kind"] == "extract_llm" and isinstance(binding, str) and meta.get("source_binding") == binding
                and type(meta.get("extract_version")) is int):
            states["jev"] = _extract_jev_state(db, project_id, message_id, row["artifact_id"],
                                               row["content_hash"], meta["extract_version"], content)
        if (not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1
                or type(proof.get("complete")) is not bool
                or not isinstance(binding, str) or meta.get("source_binding") != binding
                or proof.get("source_binding") != binding or proof.get("result_hash") != payload_hash(content)):
            return result()
        if row["kind"] == "extract_llm":
            if proof.get("stage") != "extract":
                return result()
            if ("__mcs_internal_proof_v1" in content or "internal_proof_version" in meta
                    or "internal_proof_hash" in meta):
                import extract_llm
                message = db.execute("SELECT * FROM messages WHERE project_id=? AND message_id=?",
                                     (project_id, message_id)).fetchone()
                if (message is None or extract_llm.progress_for_message(
                        db, message, model=row["model"], expected_artifact_id=row["artifact_id"])["state"] != "complete"):
                    states = dict.fromkeys(PROVIDERS, "unknown")
                    return result()
        else:
            if proof.get("stage") != "projection" or not isinstance(cfg, dict):
                return result()
            import semantic_policy
            import semantic_store
            import semantic_transport
            target = db.execute("SELECT COALESCE(parent_id,message_id) FROM messages "
                                "WHERE project_id=? AND message_id=?", (project_id, message_id)).fetchone()
            scfg, errors = semantic_policy.semantic_config(cfg)
            if target is None or errors:
                return result()
            model = semantic_transport.llm_model(cfg)
            cache_key = (project_id, target[0], model)
            if bundle_cache is not None and cache_key in bundle_cache:
                bundle = bundle_cache[cache_key]
            else:
                bundle = semantic_store.thread_bundle(db, project_id, target[0], local_model=model)
                if bundle_cache is not None:
                    bundle_cache[cache_key] = bundle
            if bundle is None:
                return result()
            member = next((m for m in bundle["members"] if m["message_id"] == message_id), None)
            policy = semantic_policy.policy_fingerprint(scfg)
            if (member is None or proof.get("source_fingerprint") != bundle["source_fingerprint"]
                    or meta.get("fingerprint") != bundle["source_fingerprint"]
                    or proof.get("policy_fingerprint") != policy or meta.get("policy_fingerprint") != policy
                    or proof.get("target_revision") != member["revision"]):
                return result()
            # The projection's own metadata cannot prove its producer. Recheck
            # against the current source document and audit receipts instead.
            import semantic_publication
            source = db.execute("SELECT content FROM artifacts WHERE kind=? AND project_id=? "
                                "AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                                (semantic_policy.KIND_FACTS_V2, project_id, message_id)).fetchone()
            if source is None:
                return result()
            doc = strict_json(source["content"])
            projection_sequence = {}
            if not isinstance(doc, dict) or proof != semantic_publication._projection_provenance(
                    db, project_id, message_id, bundle["source_fingerprint"], policy, member, doc, content,
                    sequence_out=projection_sequence):
                return result()
            if proof.get("jev") in ("done", "partial", "skipped"):
                states["jev"] = proof["jev"]
        providers = proof.get("providers")
        if (not isinstance(providers, list)
                or any(not isinstance(p, str) or p not in ("local", "anthropic", "openai") for p in providers)
                or len(set(providers)) != len(providers) or (proof["complete"] and not providers)):
            if row["kind"] != "extract_llm":
                states = dict.fromkeys(PROVIDERS, "unknown")
            return result()
        if proof["complete"]:
            if row["kind"] == "extract_llm":
                # A proof's own provider field is not independent evidence:
                # the selected artifact's actual model/contract must agree.
                route = _route(row, meta, content)
                if (len(providers) != 1
                        or (providers[0] == "local" and route != "local")
                        or (providers[0] != "local" and
                            (route != "cloud" or meta.get("backend") != providers[0]))):
                    return result()
            for provider in ("anthropic", "openai", "local"):
                # The adopted result does not prove that another provider
                # was never attempted before a fallback or earlier stage.
                states[provider] = "done" if provider in providers else "unknown"
            if row["kind"] == "extract_llm" and states["jev"] in ("done", "partial"):
                # QC names this exact retained extraction, so its Jev calls
                # follow the adopted producer. Timestamps are not evidence.
                sequence = [providers[0], "jev"]
            elif row["kind"] != "extract_llm":
                sequence = projection_sequence.get("sequence")
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        states = dict.fromkeys(PROVIDERS, "unknown")
        sequence = None
    return result()


def provider_text(states, visible=None, sequence=None):
    """Readable fallback for transports without inline provider images."""
    allowed = PROVIDERS if visible is None else visible if isinstance(visible, (list, tuple)) else ()
    names = {"anthropic": "Anthropic", "openai": "OpenAI", "jev": "Jev", "local": "💻"}
    ordered = list(sequence) if sequence else []
    remaining = [key for key in PROVIDERS if key in allowed and key not in ordered]
    lines = [f"{names[key]} {STATE_LABELS[states[key]]}" for key in ordered]
    if ordered and remaining:
        lines.append("その他（順序不明）")
    lines.extend(f"{names[key]} {STATE_LABELS[states[key]]}" for key in remaining)
    return " · ".join(lines)


def _route(row, meta, content):
    if (not isinstance(meta, dict) or not isinstance(content, dict) or not content
            or not isinstance(row["model"], str) or not row["model"]):
        return None
    if row["kind"] != "extract_llm":
        # Derived rows can reuse mixed stages; their model column is not provenance.
        return None
    if meta.get("actual_model") != row["model"]:
        return None
    backend = meta.get("backend")
    import cloud_policy
    import cloud_inference
    if backend is None:
        try:
            cloud_policy._price_definition(row["model"])
        except cloud_policy.AnthropicError:
            return "local"
        return None
    contract = {key: meta.get(key) for key in ("backend", "model", "prompt")}
    return "cloud" if (backend in cloud_policy.PROVIDERS and contract["model"] == row["model"]
                       and cloud_inference.is_supported_cloud_contract(contract)) else None
