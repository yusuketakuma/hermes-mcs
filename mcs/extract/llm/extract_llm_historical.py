"""Retired extraction-contract constants for stall recognition.

Recognition only: these finite sets prove a stalled checkpoint was written
under a known retired generation of the chunk-context binding. They never
resume the old attempt — the caller builds an independent current frame and
runs the current request locally while every old row stays byte-identical.

A generation may also carry ``"material": true``. Only such marked
generations let an otherwise current, leaf-sealed final extraction count as
patient-context material (``material_contracts``); the mark is added only
after owner approval, and the shipped contracts carry none.
"""
import json
from pathlib import Path

_CONTRACTS_PATH = Path(__file__).with_name("extract_llm_historical_contracts.json")
_KNOWN_OLD = None


def _load():
    doc = json.loads(_CONTRACTS_PATH.read_text(encoding="utf-8"))
    gens = doc.get("generations") if isinstance(doc, dict) else None
    if (not isinstance(doc, dict) or doc.get("contract") != "extract-llm-historical/v1"
            or not isinstance(gens, list) or not gens):
        raise ValueError("historical_contracts_invalid")
    out = []
    for gen in gens:
        if (not isinstance(gen, dict) or not isinstance(gen.get("id"), str) or not gen["id"]
                or not isinstance(gen.get("prompt_head"), str) or not gen["prompt_head"]
                or not isinstance(gen.get("schema"), dict)
                or type(gen.get("material", False)) is not bool):
            raise ValueError("historical_contracts_invalid")
        out.append((gen["id"], gen["prompt_head"], gen["schema"], gen.get("material", False)))
    return tuple(out)


def _loaded():
    global _KNOWN_OLD
    if _KNOWN_OLD is None:
        _KNOWN_OLD = _load()
    return _KNOWN_OLD


def known_old_contracts():
    """Finite (id, prompt_head, schema) sets retired before the live contract."""
    return tuple(gen[:3] for gen in _loaded())


def material_contracts():
    """Owner-approved retired (id, prompt_head, schema) sets usable as final material."""
    return tuple(gen[:3] for gen in _loaded() if gen[3])
