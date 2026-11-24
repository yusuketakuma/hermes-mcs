"""A local model change keeps cloud semantic artifacts and redoes local ones (fully synthetic)."""
import json
import re

import pytest

import semantic_policy
import semantic_runtime
import semantic_store
import semantic_transport
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _cfg, _ledger, _message, _patient

# bundle_fingerprint of _MEMBER at the shipped defaults, pinned before the
# local model left the fingerprint (hash compatibility for existing rows);
# re-pinned when the Jev model moved to jev-1.14.0 (a model bump invalidates
# prior results by design, INV-15).
GOLDEN_FINGERPRINT = "2291c22d8cbe159fb5fbf8462adaf952630ae7f050aba5e329db9044d5da3ae3"
_MEMBER = {"project_id": 1, "message_id": 1, "parent_id": None, "revision": "r1", "source_occurrence": 1,
           "posted_at": "2026-09-19T00:00:00+09:00", "occurred_at": None,
           "sender": {"id": 1, "type": "user", "profession": ""},
           "source_metadata": {"sender_name": "sender", "organization": ""},
           "body_original": "架空の連絡です。", "body_state": "full", "reply_count": 0,
           "attachments": [], "role": "root"}
CLOUD = {"providers": ["anthropic"], "complete": True}
LOCAL = {"providers": ["local"], "complete": True}


@pytest.fixture
def model(monkeypatch):
    value = ["synthetic-local-a"]
    monkeypatch.setattr(semantic_transport, "LLM_MODEL", "synthetic-local-a")

    def set_model(name):
        value[0] = name
        monkeypatch.setattr(semantic_transport, "LLM_MODEL", name)
    return set_model


@pytest.fixture
def store(tmp_path):
    ledger = _ledger(tmp_path)
    _patient(ledger)
    ledger.save_messages([_message(1)])
    yield ledger
    ledger.close()


def _add(store, kind, *, proof=None, model="synthetic-local-a", fp="fp", policy="policy", **meta):
    full = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy, **meta}
    if proof is not None:
        full["provider_provenance"] = {"version": 1, "stage": "summary", **proof}
    return store.artifact_add(kind, json.dumps({"claims": [{"text": kind}]}), project_id=1, message_id=1,
                              model=model, meta=full)


def test_bundle_fingerprint_ignores_configured_local_model(model):
    first = semantic_store.bundle_fingerprint([_MEMBER])
    model("synthetic-local-b")
    assert semantic_store.bundle_fingerprint([_MEMBER]) == first == GOLDEN_FINGERPRINT


def test_thread_bundle_is_the_same_under_another_local_model(store, model):
    first = semantic_store.thread_bundle(store.db, 1, 1)
    model("synthetic-local-b")
    assert semantic_store.thread_bundle(store.db, 1, 1) == first


@pytest.mark.parametrize("proof,row_model,expected", [
    (CLOUD, "old", True),
    ({"providers": ["openai"], "complete": True}, "old", True),
    ({"providers": ["anthropic", "openai"], "complete": True}, "old", True),
    ({"providers": ["anthropic"], "complete": False}, "old", False),
    ({"providers": ["anthropic"], "complete": False}, "now", True),
    ({"providers": ["anthropic", "local"], "complete": True}, "old", False),
    ({"providers": ["anthropic", "local"], "complete": True}, "now", True),
    (LOCAL, "old", False),
    (LOCAL, "now", True),
    ({"providers": [], "complete": False}, "old", False),
    (None, "old", False),
    (None, "now", True),
    ({"providers": "anthropic", "complete": True}, "old", False),
    ({"providers": ["unknown"], "complete": True}, "old", False),
    ({"providers": ["anthropic"], "complete": 1}, "old", False),
    (CLOUD, None, True),
    # a row that never recorded its model keeps its fingerprint-only identity
    (LOCAL, None, True),
    (LOCAL, "", True),
    (LOCAL, 7, False),
])
def test_local_producer_rule_matrix(proof, row_model, expected):
    import extraction_provenance
    assert extraction_provenance.local_producer_current(proof, row_model, "now") is expected


def test_cloud_artifacts_stay_current_and_local_ones_go_stale(store, model):
    import semantic_publication
    cloud = {}
    for kind in (semantic_policy.KIND_SUMMARY, semantic_policy.KIND_FACTS_V2,
                 semantic_policy.KIND_FACT_PROJ, semantic_publication.KIND_V4, semantic_policy.KIND_CANDIDATE):
        _add(store, kind, proof=CLOUD)
        cloud[kind] = semantic_store.current_artifact(store.db, kind, 1, "fp", "policy")
        assert cloud[kind] is not None
    model("synthetic-local-b")
    for kind, before in cloud.items():
        assert semantic_store.current_artifact(store.db, kind, 1, "fp", "policy") == before
    for kind in (semantic_policy.KIND_SUMMARY, semantic_policy.KIND_FACTS):
        _add(store, kind, proof=LOCAL if kind == semantic_policy.KIND_SUMMARY else None)
        # strict latest: the stale local row never falls through to the older cloud row
        assert semantic_store.current_artifact(store.db, kind, 1, "fp", "policy") is None
    model("synthetic-local-a")
    assert semantic_store.current_artifact(store.db, semantic_policy.KIND_SUMMARY, 1, "fp", "policy") is not None


def test_reservation_receipts_and_jev_results_are_not_model_pinned(store, model):
    kinds = (semantic_policy.KIND_REPAIR, semantic_policy.KIND_SUMMARY_FORMAT_REPAIR,
             semantic_policy.KIND_FACT_REPAIR, semantic_policy.KIND_AUDIT, semantic_policy.KIND_COVERAGE,
             semantic_policy.KIND_FACT_AUDIT, semantic_policy.KIND_BUNDLE)
    for kind in kinds:
        _add(store, kind)
    model("synthetic-local-b")
    for kind in kinds:
        # a spent repair is never refunded by a local model change
        assert semantic_store.current_artifact(store.db, kind, 1, "fp", "policy") is not None, kind


def _kind_constants():
    import semantic_conversion
    import semantic_extraction
    import semantic_loops
    import semantic_publication
    values = set()
    for module in (semantic_policy, semantic_publication, semantic_extraction, semantic_store,
                   semantic_conversion, semantic_loops):
        values |= {value for name, value in vars(module).items()
                   if re.fullmatch(r"KIND_[A-Z0-9_]+", name) and isinstance(value, str)}
    return values


def test_every_semantic_kind_is_classified_for_the_local_model_gate():
    gated, ungated = semantic_policy.LOCAL_MODEL_GATED_KINDS, semantic_policy.LOCAL_MODEL_UNGATED_KINDS
    assert not gated & ungated
    assert _kind_constants() == gated | ungated


def test_loop_completion_generation_is_model_free_but_requeue_generation_is_not(model):
    scfg = semantic_policy.runtime_config(_cfg("shadow"))[0]
    model("Qwen3.5-9B")
    legacy = semantic_runtime.execution_generation(scfg)
    stable = semantic_runtime.interpretation_generation(scfg)
    model("synthetic-local-b")
    assert semantic_runtime.interpretation_generation(scfg) == stable == legacy
    assert semantic_runtime.execution_generation(scfg) != legacy


def test_local_model_is_resolved_once_per_scope(store, monkeypatch):
    reads = []
    original = semantic_transport.llm_conf
    monkeypatch.setattr(semantic_transport, "llm_conf", lambda cfg=None: reads.append(1) or original({}))
    for _ in range(3):
        _add(store, semantic_policy.KIND_SUMMARY, proof=LOCAL, model="Qwen3.5-9B")
    with semantic_transport.local_model_scope():
        for _ in range(5):
            assert semantic_store.current_artifact(store.db, semantic_policy.KIND_SUMMARY, 1, "fp", "policy")
    assert len(reads) == 1
    reads.clear()
    with semantic_transport.local_model_scope("Qwen3.5-9B"):
        semantic_store.current_artifact(store.db, semantic_policy.KIND_SUMMARY, 1, "fp", "policy")
    assert reads == []


def _with_local(cfg, **local_llm):
    import copy
    cfg = copy.deepcopy(cfg)
    cfg["local_llm"] = local_llm
    return cfg


_OTHER_URL = "http://127.0.0.1:9090/v1/chat/completions"


def test_legacy_policy_ignores_a_configured_local_model_change():
    # The real path: config.json local_llm changes, not the module seam.
    base = _cfg("enforce", loop_mode="off")
    first, errors = semantic_policy.runtime_config(_with_local(base, model="synthetic-local-a"))
    assert not errors and first["fact_source"] == "legacy"
    for local_llm in ({"model": "synthetic-local-b"}, {"model": "synthetic-local-a", "url": _OTHER_URL}):
        other, errors = semantic_policy.runtime_config(_with_local(base, **local_llm))
        assert not errors and other == first
        assert semantic_policy.policy_fingerprint(other) == semantic_policy.policy_fingerprint(first)


@pytest.mark.xfail(strict=True, reason=(
    "open owner decision: for fact_source != legacy, policy_fingerprint includes candidate_fingerprint, "
    "which hashes local_llm.resolve(cfg) and the whole local_llm block "
    "(docs/specs/patient-context.md, docs/specs/semantic-facts-v2-rollout.md 'Local model change')"))
def test_shadow_policy_ignores_a_configured_local_model_change():
    base = _cfg("assist", fact_source="shadow")
    first = semantic_policy.runtime_config(_with_local(base, model="synthetic-local-a"))[0]
    other = semantic_policy.runtime_config(_with_local(base, model="synthetic-local-b"))[0]
    assert first["fact_source"] == other["fact_source"] == "shadow"
    assert semantic_policy.policy_fingerprint(other) == semantic_policy.policy_fingerprint(first)


def test_canonical_approval_is_bound_to_the_configured_local_model():
    # Current behaviour, pinned: canonical evidence was measured on one local
    # model, so another model is a config error (semantic off) until approved again.
    from candidate_testkit import approved_config
    approved = approved_config(_with_local(_cfg("enforce", loop_mode="off", fact_source="canonical",
                                                fact_source_gate="g6-v1:test"), model="synthetic-local-a"))
    scfg, errors = semantic_policy.runtime_config(approved)
    assert not errors and scfg["fact_source"] == "canonical"
    changed = _with_local(approved, model="synthetic-local-b")
    scfg, errors = semantic_policy.runtime_config(changed)
    assert errors == ["config: semantic_fact_source_evidence_mismatch"]
    assert scfg["fact_source"] == "legacy" and scfg["mode"] == "off"


def _artifact_writers():
    """(path, line, kind expression, model keyword) of every semantic artifact write."""
    import ast
    from pathlib import Path
    root = Path(semantic_policy.__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("artifact_add", "artifact_add_tx") and node.args):
                model = next((k.value for k in node.keywords if k.arg == "model"), None)
                yield path.name, node.lineno, node.args[0], model


def test_every_local_model_gated_writer_records_its_model():
    """A row without a model keeps its fingerprint-only identity, so a gated writer must name one."""
    import ast
    constants = {name: value for module in (semantic_policy,) + tuple(
        __import__(m) for m in ("semantic_publication", "semantic_extraction", "semantic_store",
                                "semantic_conversion", "semantic_loops"))
        for name, value in vars(module).items() if re.fullmatch(r"KIND_[A-Z0-9_]+", name)}
    gated_writers = 0
    for name, line, kind, model in _artifact_writers():
        if isinstance(kind, ast.Constant):
            value = kind.value
        elif isinstance(kind, ast.Name) and kind.id in constants:
            value = constants[kind.id]
        elif isinstance(kind, ast.Attribute) and kind.attr in constants:
            value = constants[kind.attr]
        else:
            value = None  # a computed kind may be any kind, gated ones included
        if value is not None and value not in semantic_policy.LOCAL_MODEL_GATED_KINDS:
            continue
        gated_writers += 1
        assert model is not None and not (isinstance(model, ast.Constant) and not model.value), (
            f"{name}:{line} writes {ast.unparse(kind)} without a model")
    assert gated_writers >= 8
