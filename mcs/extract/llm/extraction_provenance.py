"""Validate extraction provider provenance independently of scheduling and transport state."""
import re

import cloud_inference
import mcs_util
from mcs_util import strict_json


def provider_evidence(providers=(), complete=False):
    allowed = ("local", "anthropic", "openai")
    return {"providers": [name for name in allowed if name in providers],
            "complete": bool(complete)}


def provider_strict_dict(raw):
    try:
        value = strict_json(raw)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def provider_result_hash(content):
    value = provider_strict_dict(content)
    if not isinstance(value, dict):
        return None
    try:
        return mcs_util.payload_hash(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


def internal_proof_hash(proof):
    try:
        return mcs_util.payload_hash(proof)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        return None


def evidence_matches_route(evidence, model, contract):
    names = evidence.get("providers")
    if not isinstance(model, str) or not model or not isinstance(names, list):
        return False
    if not names:
        return evidence.get("complete") is False
    if len(names) != 1:
        return False
    if names == ["local"]:
        return contract is None
    name = names[0]
    return (name in ("anthropic", "openai") and isinstance(contract, dict)
            and set(contract) == {"backend", "model", "prompt"}
            and contract.get("backend") == name and contract.get("model") == model
            and cloud_inference.is_supported_cloud_contract(contract)
            and isinstance(contract.get("prompt"), str)
            and re.fullmatch(r"[0-9a-f]{16}", contract["prompt"]) is not None)


def prior_provider_evidence(prior, content, binding):
    # Only inherit evidence bound to the exact retained artifact and source.
    meta = provider_strict_dict(prior["meta"]) if prior else None
    proof = meta.get("provider_provenance") if isinstance(meta, dict) else None
    digest = provider_result_hash(content)
    if (not isinstance(proof, dict) or digest is None
            or prior["content"] != content or meta.get("source_binding") != binding
            or type(proof.get("version")) is not int or proof.get("version") != 1
            or proof.get("stage") != "extract"
            or proof.get("source_binding") != binding
            or proof.get("result_hash") != digest
            or type(proof.get("complete")) is not bool
            or not isinstance(proof.get("providers"), list)
            or any(name not in ("local", "anthropic", "openai") for name in proof["providers"])
            or len(set(proof["providers"])) != len(proof["providers"])
            or (proof["complete"] and not proof["providers"])):
        return provider_evidence()
    contract = ({key: meta.get(key) for key in ("backend", "model", "prompt")}
                if any(key in meta for key in ("backend", "model", "prompt")) else None)
    if (meta.get("actual_model") != prior["model"]
            or not evidence_matches_route(proof, prior["model"], contract)):
        return provider_evidence()
    return provider_evidence(proof["providers"], proof["complete"])


class EvidenceLeaf(dict):
    def __init__(self, value, evidence):
        super().__init__(value)
        self.provider_evidence = evidence
        self.quality_origin = None
        self.quality_origin_id = None
        self.quality_started_id = None
        self.quality_prior_kind = None
        self.repair_proof = None


class EvidenceSplit(int):
    def __new__(cls, value, repair_proof):
        item = super().__new__(cls, value)
        item.repair_proof = repair_proof
        return item


def leaf_evidence(value):
    proof = getattr(value, "provider_evidence", None)
    if not isinstance(proof, dict):
        return None
    names, complete = proof.get("providers"), proof.get("complete")
    if (not isinstance(names, list) or type(complete) is not bool
            or any(name not in ("local", "anthropic", "openai") for name in names)
            or len(set(names)) != len(names) or (complete and not names)):
        return None
    return provider_evidence(names, complete)
