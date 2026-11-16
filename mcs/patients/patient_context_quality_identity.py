"""Bind patient-context quality checks to the checking code instead of every loaded source.

A published PASS stays shown only while the code that asks the questions and
accepts the answers is unchanged. Unrelated releases no longer withdraw every
PASS. Code outside ``QUALITY_FUNCTIONS`` that records or checks a PASS (the
quality controller and receipts modules, semantic_usage_receipts) needs a
manual ``QA_VERSION`` bump when its acceptance rule changes.

This module is context-only: attachment extraction never runs it, so a
``QA_VERSION`` bump does not change the attachment extraction contract.
"""
from __future__ import annotations

import importlib
import sys

from mcs_util import payload_hash

# Bump when acceptance changes outside the hashed code: the PASS rule in the
# quality controller/receipts, a reinterpretation of an answer, or a Jev model
# behaviour change under the same model name.
QA_VERSION = 1

# (module, name). Names are resolved through module globals on every call, so a
# replaced function is seen. Helpers that only move bytes (mcs_util, the Jev
# transport) and source-key parsing are deliberately outside this set, as is
# semantic_jev._merge_partial_answers: it runs only when evaluate_once_more has
# no answer callback, and audit_patient_quality_page always passes one.
QUALITY_FUNCTIONS = (
    ("semantic_audit", "_probability"),
    ("semantic_audit", "_patient_quality_sources"),
    ("semantic_audit", "_patient_quality_facts"),
    ("semantic_audit", "patient_source_question_keys"),
    ("semantic_audit", "patient_source_quality_pages"),
    ("semantic_audit", "patient_source_quality_identity"),
    ("semantic_audit", "audit_patient_quality_page"),
    ("semantic_audit", "audit_patient_summary_chunk"),
    ("semantic_jev", "choice_question"),
    ("semantic_jev", "evaluate_once_more"),
    ("semantic_jev", "validate_answers"),
    ("semantic_jev", "_validate_usage"),
    ("semantic_jev", "_validate_answer"),
    ("semantic_jev", "finite_value"),
    ("semantic_jev", "is_answer_defect"),
    ("semantic_jev", "failure_class"),
    ("patient_context_quality_identity", "patient_quality_expected"),
)
QUALITY_CONSTANTS = (
    ("semantic_audit", "FACT_SUPPORT_OPTIONS"),
    ("semantic_audit", "COVERAGE_OPTIONS"),
    ("semantic_audit", "_PATIENT_SUMMARY_OMISSION_OPTIONS"),
    ("semantic_audit", "_PATIENT_SUMMARY_CHUNK_LIMIT"),
    ("semantic_audit", "_PATIENT_SOURCE_CONTEXT_FIELDS"),
    ("semantic_jev", "CLAIM_SUPPORT_OPTIONS"),
    ("semantic_jev", "JEV_MODEL"),
    ("semantic_jev", "DATA_NOTE"),
    ("patient_context", "CATEGORIES"),
)


def patient_quality_expected(identity, fid, selected):
    """Return the answer that passes one quality question."""
    if identity.get("purpose", "summary") == "source":
        return identity["passing_choices"][fid]
    return "supports" if fid in selected else "omission_ok"


# Marshalling the functions dominates the cost and runs per source and per read.
# Entries hold the hashed objects themselves, so a replaced function, code or
# defaults object (also one reusing a freed id) misses the cache.
_CODE_CACHE = {}


def _resolve(module, name):
    return getattr(importlib.import_module(module), name)


def _function_identity(function):
    import semantic_candidate

    parts = (function, getattr(function, "__code__", None), getattr(function, "__defaults__", None),
             getattr(function, "__kwdefaults__", None))
    cached = _CODE_CACHE.get(id(function))
    if cached is not None and all(a is b for a, b in zip(cached[0], parts)):
        return cached[1]
    digest = semantic_candidate.code_identity(function)
    if len(_CODE_CACHE) >= 4 * len(QUALITY_FUNCTIONS):
        _CODE_CACHE.clear()
    _CODE_CACHE[id(function)] = (parts, digest)
    return digest


def patient_quality_code():
    """Hash the quality-checking functions and constants, plus the bytecode-relevant interpreter."""
    import semantic_candidate

    return payload_hash({
        "v": 1,
        # Bytecode differs by Python minor version and -O level; name the cause.
        "python": [sys.version_info[0], sys.version_info[1], sys.flags.optimize],
        "functions": {f"{module}.{name}": _function_identity(_resolve(module, name))
                      for module, name in QUALITY_FUNCTIONS},
        "constants": {f"{module}.{name}": semantic_candidate.constant_identity(_resolve(module, name))
                      for module, name in QUALITY_CONSTANTS},
    })


def patient_quality_assessment_code():
    """Return the assessment code that every quality identity and PASS receipt carries."""
    return payload_hash(["patient-context-quality/1", QA_VERSION, patient_quality_code()])
