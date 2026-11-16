"""Read stored, already-validated extraction outputs for offline benchmark scoring.

Evaluation only: nothing here is imported by the periodic jobs, and nothing
here calls a model, opens a connection or re-validates an output.
"""
from __future__ import annotations

_GENERATION_KEYS = frozenset({"llm_extract", "rule", "patient_context"})
# What only a `run` report measures; null in a report means "not measured here".
_ORIGIN_KEYS = ("model", "actual_model", "patient_state_mode", "prompt_sha256",
                "call_stats", "performance")


def read(stored, case_ids: list) -> tuple[dict, dict]:
    """Read validated outputs and their provenance from either accepted input shape."""
    if not isinstance(stored, dict):
        raise ValueError("outputs_not_an_object")
    if isinstance(stored.get("cases"), list) and "outputs" not in stored:
        # a `run` report: llm_extract output is validated by construction
        if not isinstance(stored.get("prompt_sha256"), str):
            raise ValueError("outputs_not_a_run_report")
        rows = stored["cases"]
        if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows) \
                or len({row["id"] for row in rows}) != len(rows):
            raise ValueError("outputs_case_ids_invalid")
        if any(not row.get("error") and not isinstance(row.get("validated_output"), dict)
               for row in rows):
            # a row with neither an error nor an output was not measured: not a failure
            raise ValueError("outputs_shape_invalid")
        outputs = {row["id"]: None if row.get("error") else row["validated_output"]
                   for row in rows}
        origin = {key: stored.get(key) for key in _ORIGIN_KEYS}
    else:
        outputs = stored.get("outputs")
        if stored.get("validated") is not True:
            raise ValueError("outputs_must_be_validated")
        if not isinstance(outputs, dict):
            raise ValueError("outputs_missing")
        if any(out is not None and not isinstance(out, dict) for out in outputs.values()):
            raise ValueError("outputs_shape_invalid")
        origin = dict.fromkeys(_ORIGIN_KEYS)
    generations = stored.get("generations")
    if (not isinstance(generations, dict) or not generations
            or set(generations) - _GENERATION_KEYS
            or any(type(value) is not int for value in generations.values())):
        # an unnamed generation cannot be compared with a baseline
        raise ValueError("outputs_generations_required")
    origin["generations"] = generations
    if set(outputs) != set(case_ids):
        # a missing output is not a miss and an unknown id is not ignorable
        raise ValueError("outputs_case_ids_mismatch")
    scored = stored.get("scored_fields")
    if scored is not None and (not isinstance(scored, list) or not scored
                               or any(not isinstance(name, str) or "." in name for name in scored)):
        raise ValueError("outputs_scored_fields_invalid")
    origin["scored_fields"] = scored
    return outputs, origin


def limit_fields(score: dict, scored: list) -> list:
    """Drop counts for fields whose output shape the producer does not share; name them."""
    def kept(key):
        return key.split(".", 1)[0] in scored
    dropped = sorted({key.split(".", 1)[0] for source in ("fields", "forbid_counts")
                      for key in score[source] if not kept(key)})
    score["fields"] = {key: value for key, value in score["fields"].items() if kept(key)}
    score["forbid_counts"] = {key: value for key, value in score["forbid_counts"].items() if kept(key)}
    score["forbid_violations"] = [name for name in score["forbid_violations"]
                                  if kept(name.split(":", 1)[0])]
    return dropped
