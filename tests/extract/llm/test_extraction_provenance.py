"""The provider-provenance move preserves the pre-refactor functions and compatibility names."""
import ast
from pathlib import Path

import extraction_provenance as provenance
import extract_llm

NAMES = {'_provider_evidence': 'provider_evidence', '_provider_strict_dict': 'provider_strict_dict', '_provider_result_hash': 'provider_result_hash', '_internal_proof_hash': 'internal_proof_hash', '_evidence_matches_route': 'evidence_matches_route', '_prior_provider_evidence': 'prior_provider_evidence', '_EvidenceLeaf': 'EvidenceLeaf', '_EvidenceSplit': 'EvidenceSplit', '_leaf_evidence': 'leaf_evidence'}
BASELINE_SOURCE = {'_provider_evidence': 'def _provider_evidence(providers=(), complete=False):\n    allowed = ("local", "anthropic", "openai")\n    return {"providers": [name for name in allowed if name in providers],\n            "complete": bool(complete)}', '_provider_strict_dict': 'def _provider_strict_dict(raw):\n    try:\n        value = strict_json(raw)\n    except (TypeError, ValueError, OverflowError, RecursionError):\n        return None\n    return value if isinstance(value, dict) else None', '_provider_result_hash': 'def _provider_result_hash(content):\n    value = _provider_strict_dict(content)\n    if not isinstance(value, dict):\n        return None\n    try:\n        return mcs_util.payload_hash(value)\n    except (TypeError, ValueError, OverflowError, RecursionError):\n        return None', '_internal_proof_hash': 'def _internal_proof_hash(proof):\n    try:\n        return mcs_util.payload_hash(proof)\n    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):\n        return None', '_evidence_matches_route': 'def _evidence_matches_route(evidence, model, contract):\n    names = evidence.get("providers")\n    if not isinstance(model, str) or not model or not isinstance(names, list):\n        return False\n    if not names:\n        return evidence.get("complete") is False\n    if len(names) != 1:\n        return False\n    if names == ["local"]:\n        return contract is None\n    name = names[0]\n    return (name in ("anthropic", "openai") and isinstance(contract, dict)\n            and set(contract) == {"backend", "model", "prompt"}\n            and contract.get("backend") == name and contract.get("model") == model\n            and cloud_inference.is_supported_cloud_contract(contract)\n            and isinstance(contract.get("prompt"), str)\n            and re.fullmatch(r"[0-9a-f]{16}", contract["prompt"]) is not None)', '_prior_provider_evidence': 'def _prior_provider_evidence(prior, content, binding):\n    # Only inherit evidence bound to the exact retained artifact and source.\n    meta = _provider_strict_dict(prior["meta"]) if prior else None\n    proof = meta.get("provider_provenance") if isinstance(meta, dict) else None\n    digest = _provider_result_hash(content)\n    if (not isinstance(proof, dict) or digest is None\n            or prior["content"] != content or meta.get("source_binding") != binding\n            or type(proof.get("version")) is not int or proof.get("version") != 1\n            or proof.get("stage") != "extract"\n            or proof.get("source_binding") != binding\n            or proof.get("result_hash") != digest\n            or type(proof.get("complete")) is not bool\n            or not isinstance(proof.get("providers"), list)\n            or any(name not in ("local", "anthropic", "openai") for name in proof["providers"])\n            or len(set(proof["providers"])) != len(proof["providers"])\n            or (proof["complete"] and not proof["providers"])):\n        return _provider_evidence()\n    contract = ({key: meta.get(key) for key in ("backend", "model", "prompt")}\n                if any(key in meta for key in ("backend", "model", "prompt")) else None)\n    if (meta.get("actual_model") != prior["model"]\n            or not _evidence_matches_route(proof, prior["model"], contract)):\n        return _provider_evidence()\n    return _provider_evidence(proof["providers"], proof["complete"])', '_EvidenceLeaf': 'class _EvidenceLeaf(dict):\n    def __init__(self, value, evidence):\n        super().__init__(value)\n        self.provider_evidence = evidence\n        self.quality_origin = None\n        self.quality_origin_id = None\n        self.quality_started_id = None\n        self.quality_prior_kind = None\n        self.repair_proof = None', '_EvidenceSplit': 'class _EvidenceSplit(int):\n    def __new__(cls, value, repair_proof):\n        item = super().__new__(cls, value)\n        item.repair_proof = repair_proof\n        return item', '_leaf_evidence': 'def _leaf_evidence(value):\n    proof = getattr(value, "provider_evidence", None)\n    if not isinstance(proof, dict):\n        return None\n    names, complete = proof.get("providers"), proof.get("complete")\n    if (not isinstance(names, list) or type(complete) is not bool\n            or any(name not in ("local", "anthropic", "openai") for name in names)\n            or len(set(names)) != len(names) or (complete and not names)):\n        return None\n    return _provider_evidence(names, complete)'}


def test_provider_provenance_functions_are_identical_to_reviewed_baseline():
    aliases = {new: old for old, new in NAMES.items()}

    class OriginalNames(ast.NodeTransformer):
        def visit_Name(self, node):
            node.id = aliases.get(node.id, node.id)
            return node

        def visit_FunctionDef(self, node):
            node.name = aliases.get(node.name, node.name)
            return self.generic_visit(node)

        def visit_ClassDef(self, node):
            node.name = aliases.get(node.name, node.name)
            return self.generic_visit(node)

    tree = ast.parse(Path(provenance.__file__).read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in aliases:
            old = aliases[node.name]
            normalized = OriginalNames().visit(node)
            original = ast.parse(BASELINE_SOURCE[old]).body[0]
            assert ast.dump(normalized, include_attributes=False) == ast.dump(original, include_attributes=False)
            assert getattr(extract_llm, old) is getattr(provenance, NAMES[old])


def test_provenance_carries_quality_without_marking_unknown_complete():
    leaf = provenance.EvidenceLeaf({'symptoms': []}, provenance.provider_evidence(['local'], True))
    assert provenance.leaf_evidence(leaf) == {'providers': ['local'], 'complete': True}
    assert leaf.quality_origin is leaf.repair_proof is leaf.quality_started_id is None
    leaf.provider_evidence = {'providers': [], 'complete': True}
    assert provenance.leaf_evidence(leaf) is None
    assert provenance.provider_strict_dict('{"complete":true,"complete":false}') is None
