#!/usr/bin/env python3
"""Record and check the code a deployed extraction generation is frozen on.

``record`` writes a digest of every source and version that invalidates a derived
artifact when edited (classes A-I of the Blk-X plan); ``check`` compares the
running code with that record and names the artifacts a difference would
invalidate. A record that cannot be compared is a difference, never "no change".
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'inference-freeze/v1'
CLASSES = {
    'A': ('attachment pages', ['attachment_extract_v1', 'attachment_inference_checkpoint_v1'],
          'local LLM or cloud inference for every attachment page'),
    'B': ('held extractions', ['extract_upgrade_v1'], 'local LLM retry of every held post'),
    'C': ('patient context scan', ['patient_context_v1'], 'cloud scan of every patient (once)'),
    'D': ('patient context summary', ['patient_context_v1'], 'cloud summary of every patient'),
    'E': ('promotion evidence', ['candidate_fingerprint'], 'canonical re-evaluation (Jev) and human re-approval'),
    'F': ('semantic bundles', ['semantic bundle', 'jev verdict'], 'local LLM summaries and Jev verdicts for every bundle'),
    'G': ('extraction chunks', ['extract_llm_chunk'], 'discarded and rebuilt on read'),
    'H': ('projections', ['canonical_projection', 'semantic_facts_v4'], 'local CPU reprojection'),
    'I': ('QC questions (reference)', ['extract_qc'], 'none: old QC stays current; frozen after the F gate'),
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _runtime():
    """The running modules, imported on demand (importing them is not free)."""
    sys.path.insert(0, str(ROOT / 'mcs'))
    import importlib
    importlib.import_module('_mcs_path')  # registers the flat import roots
    names = ('attachment_extraction', 'extract', 'extract_llm', 'extract_llm_source_contract',
             'inference_version', 'patient_context_refresh_state', 'semantic_jev', 'semantic_llm',
             'semantic_projection', 'semantic_store', 'clinical_chunking')
    return {name: importlib.import_module(name) for name in names}


def snapshot():
    """Digests of the running code, by class; the manifest ``record`` writes."""
    m = _runtime()
    ext, state, jev = m['extract_llm'], m['patient_context_refresh_state'], m['semantic_jev']
    loaded = dict(ext._LOADED_SOURCE_DIGESTS)
    context_only = m['attachment_extraction']._CONTEXT_ONLY_SOURCES
    versions = {'inference_version': m['inference_version'].CURRENT_VERSION,
                'extract_version': ext.EXTRACT_VERSION,
                'patient_context_version': ext.PATIENT_CONTEXT_VERSION}
    a = {name: digest for name, digest in loaded.items() if name not in context_only}
    a.update({f'version:{key}': _text(value) for key, value in versions.items()})
    b = {name: loaded[name] for name in sorted(m['extract_llm_source_contract']._HOLD_SOURCES)}
    b['hold_epoch'] = _text(m['extract_llm_source_contract'].HOLD_EPOCH)
    c = {'scan_code': state._SCAN_CODE, 'mcs_queries': loaded['mcs_queries'],
         'contract': _text(json.dumps(state._contract(), sort_keys=True)),
         'chunk_plan_version': _text(m['clinical_chunking'].PLAN_VERSION)}
    d = {'summary_code': state._SUMMARY_CODE,
         'summary_contract': _text(json.dumps(state._summary_contract(), sort_keys=True))}
    e = dict(loaded)
    e.update({f'version:{key}': _text(value) for key, value in versions.items()})
    e['rule_version'] = _text(m['extract'].RULE_VERSION)
    llm = m['semantic_llm']
    f = {'prompts': _text(json.dumps([llm._FACT_PROMPT, llm._SUMMARY_PROMPT, llm._REPAIR_SUFFIX,
                                      llm._FACT_V2_PROMPT, llm._FACT_V2_REPAIR_SUFFIX])),
         'registry_version': _text(jev.REGISTRY_VERSION), 'jev_model': _text(jev.JEV_MODEL),
         'schema_version': _text(m['semantic_store'].SCHEMA_VERSION),
         'policy_version': _text(m['semantic_store'].POLICY_VERSION),
         'chunk_plan_version': _text(m['clinical_chunking'].PLAN_VERSION)}
    g = {name: loaded[name] for name in ('inference_version', 'extract_llm_prompts', 'extract_schema')}
    h = {'projection_version': _text(m['semantic_projection'].PROJECTION_VERSION)}
    i = {'semantic_qc': _sha(ROOT / 'mcs' / 'semantic' / 'semantic_qc.py')}
    return {'schema': SCHEMA,
            'classes': {'A': a, 'B': b, 'C': c, 'D': d, 'E': e, 'F': f, 'G': g, 'H': h, 'I': i}}


def record(out):
    """Write the current snapshot to ``out``."""
    Path(out).write_text(json.dumps(snapshot(), indent=2, sort_keys=True) + '\n', encoding='utf-8')


def check(manifest):
    """Differences between ``manifest`` and the running code, one entry per class."""
    if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA \
            or not isinstance(manifest.get('classes'), dict):
        raise ValueError('freeze_manifest_unrecognized')
    now = snapshot()['classes']
    report = []
    for key, (label, kinds, cost) in CLASSES.items():
        old = manifest['classes'].get(key)
        if not isinstance(old, dict):
            old = {}
        # A class or entry missing on either side differs; it is never read as unchanged.
        files = sorted(name for name in old.keys() | now[key].keys() if old.get(name) != now[key].get(name))
        if files:
            report.append({'class': key, 'label': label, 'files': files, 'kinds': kinds, 'cost': cost})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('record').add_argument('path')
    sub.add_parser('check').add_argument('path')
    args = parser.parse_args(argv)
    if args.command == 'record':
        record(args.path)
        return 0
    try:
        manifest = json.loads(Path(args.path).read_text(encoding='utf-8'))
        report = check(manifest)
    except (OSError, ValueError) as error:
        print(f'freeze check not possible: {error}', file=sys.stderr)
        return 2
    for entry in report:
        print(f"{entry['class']} {entry['label']}: {', '.join(entry['files'])}\n"
              f"  invalidates: {', '.join(entry['kinds'])}\n  cost: {entry['cost']}")
    return 1 if report else 0


if __name__ == '__main__':
    sys.exit(main())
