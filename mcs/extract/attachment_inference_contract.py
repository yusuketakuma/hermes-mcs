"""Preserve OCR artifact identity only across an exact reviewed deploy witness."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from mcs_util import payload_hash, strict_json
from safe_io import read_regular_nofollow

SCHEMA = 'attachment-inference/v1'
WITNESS_SCHEMA = 'attachment-refactor-witness/v1'
WITNESS_KEY = 'attachment_refactor_witness'
# Hash-compatibility constant of the token recipe, not a local default: the
# token once named the configured local endpoint and model, and every stored
# result was keyed by the shipped pair. A local result is now pinned to its
# own row model (attachment_extraction.stored), so the token names no backend.
_LEGACY_LOCAL_IDENTITY = ('http://127.0.0.1:8080/v1/chat/completions', 'Qwen3.5-9B')
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
            or witness['schema'] != WITNESS_SCHEMA or not isinstance(witness['baseline'], str)
            or re.fullmatch(r'[0-9a-f]{40}', witness['baseline']) is None
            or not _digest(witness['baseline_code_digest']) or witness['versions'] != versions
            or not all(isinstance(witness[key], dict) for key in ('components', 'replacements', 'additions'))
            or sources.get(WITNESS_KEY) != _WITNESS_DIGEST):
        return None
    components, replacements, additions = (witness[key] for key in ('components', 'replacements', 'additions'))
    # The baseline's own witness digest is restored, never reviewed as a runtime component.
    reviewed = set(replacements) - {WITNESS_KEY}
    if (components.get('attachment_inference') != code_digest
            or set(components) != reviewed | set(additions)
            or reviewed & set(additions)
            or any(not _digest(value) or sources.get(key) != value for key, value in components.items())
            or any(not _digest(value) for value in replacements.values())
            or any(value != components.get(key) for key, value in additions.items())
            or WITNESS_KEY in components or WITNESS_KEY in additions):
        return None
    normalized = dict(sources)
    normalized.update(replacements)
    for key in additions:
        normalized.pop(key, None)
    if WITNESS_KEY not in replacements:
        normalized.pop(WITNESS_KEY, None)
    return witness['baseline_code_digest'], normalized


def contract(*, code_digest, source_digests, inference_version, extract_version, context_version):
    """Return the legacy artifact token only when every reviewed component matches exactly.

    The token is the extraction input identity; it never names the local backend."""
    versions = [inference_version, extract_version, context_version]
    compatible = _compatible(code_digest, source_digests, versions)
    code, sources = compatible if compatible is not None else (code_digest, source_digests)
    upgrade = hashlib.sha256(json.dumps([*versions, sources], sort_keys=True).encode()).hexdigest()
    return payload_hash([SCHEMA, code, upgrade, _LEGACY_LOCAL_IDENTITY])
