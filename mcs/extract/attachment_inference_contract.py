"""Preserve OCR artifact identity only for an exact reviewed responsibility refactor."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import local_llm
from mcs_util import payload_hash, strict_json
from safe_io import read_regular_nofollow

SCHEMA = 'attachment-inference/v1'
WITNESS_SCHEMA = 'attachment-refactor-witness/v1'
BASELINE = '132d87d2994e8d6b5c4d6fce6f1790ced77ef9db'
BASELINE_CODE = '45a81b77ca67c360ac90ccca5e6667434396ce8afb61e4374e3e821be19739b0'
WITNESS_KEY = 'attachment_refactor_witness'
_PATH = str(Path(__file__).resolve().parents[2] / 'resources' / 'inference-contracts' / 'attachment-refactor-20261105.json')
try:
    _RAW = read_regular_nofollow(_PATH, 65536, ValueError)
    _WITNESS = strict_json(_RAW) if len(_RAW) <= 65536 else None
    _WITNESS_DIGEST = hashlib.sha256(_RAW).hexdigest()
except (OSError, ValueError, TypeError, RecursionError):
    _WITNESS = None
    _WITNESS_DIGEST = None


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _compatible(code_digest, sources, versions):
    witness = _WITNESS
    if (not isinstance(witness, dict) or set(witness) != {
            'schema', 'baseline', 'baseline_code_digest', 'versions', 'components', 'replacements', 'additions'}
            or witness['schema'] != WITNESS_SCHEMA or witness['baseline'] != BASELINE
            or witness['baseline_code_digest'] != BASELINE_CODE or witness['versions'] != versions
            or not all(isinstance(witness[key], dict) for key in ('components', 'replacements', 'additions'))
            or sources.get(WITNESS_KEY) != _WITNESS_DIGEST):
        return None
    components, replacements, additions = (witness[key] for key in ('components', 'replacements', 'additions'))
    if (components.get('attachment_inference') != code_digest
            or set(components) != set(replacements) | set(additions)
            or set(replacements) & set(additions)
            or any(not _digest(value) or sources.get(key) != value for key, value in components.items())
            or any(not _digest(value) for value in replacements.values())
            or any(value != components.get(key) for key, value in additions.items())
            or WITNESS_KEY in components):
        return None
    normalized = dict(sources)
    normalized.update(replacements)
    for key in (*additions, WITNESS_KEY):
        normalized.pop(key, None)
    return witness['baseline_code_digest'], normalized


def contract(cfg, *, code_digest, source_digests, inference_version, extract_version, context_version):
    """Return the legacy artifact token only when every reviewed component matches exactly."""
    versions = [inference_version, extract_version, context_version]
    compatible = _compatible(code_digest, source_digests, versions)
    code, sources = compatible if compatible is not None else (code_digest, source_digests)
    upgrade = hashlib.sha256(json.dumps([*versions, sources], sort_keys=True).encode()).hexdigest()
    return payload_hash([SCHEMA, code, upgrade, local_llm.resolve(cfg)])
