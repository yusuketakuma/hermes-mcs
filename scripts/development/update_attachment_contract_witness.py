#!/usr/bin/env python3
"""Record exact source hashes for the reviewed 2026-11-05 OCR refactor only."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASELINE = '132d87d2994e8d6b5c4d6fce6f1790ced77ef9db'
TARGET = ROOT / 'resources' / 'inference-contracts' / 'attachment-refactor-20261105.json'
COMPONENTS = {
    'attachment_inference': 'mcs/extract/attachment_inference.py',
    'attachment_extraction': 'mcs/extract/attachment_extraction.py',
    'attachment_inference_checkpoints': 'mcs/extract/attachment_inference_checkpoints.py',
    'attachment_inference_contract': 'mcs/extract/attachment_inference_contract.py',
    'extraction_provenance': 'mcs/extract/llm/extraction_provenance.py',
    'extract_llm': 'mcs/extract/llm/extract_llm.py',
    'mcs_queries': 'mcs/core/mcs_queries.py',
    'patient_context': 'mcs/patients/patient_context.py',
    'semantic_candidate': 'mcs/semantic/semantic_candidate.py',
}


def baseline():
    """Import a source-only baseline under an empty HOME and deny network and subprocesses."""
    archived = subprocess.run(['git', 'archive', BASELINE, 'mcs'], cwd=ROOT,
                              capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory(prefix='mcs-contract-baseline-') as directory:
        private = Path(directory)
        repo, home = private / 'source', private / 'home'
        repo.mkdir()
        home.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archived)) as archive:
            archive.extractall(repo, filter='data')
        program = '''import sys, json
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'mcs'))
def audit(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'ctypes.dlopen'):
        raise RuntimeError('baseline_external_operation_forbidden')
sys.addaudithook(audit)
import _mcs_path
import extract_llm, attachment_inference
print(json.dumps({'code': attachment_inference._CODE,
    'versions': [extract_llm._INFERENCE_VERSION, extract_llm.EXTRACT_VERSION, extract_llm.PATIENT_CONTEXT_VERSION],
    'sources': extract_llm._LOADED_SOURCE_DIGESTS,
    'oracle': attachment_inference._contract({'local_llm': {'url': 'http://127.0.0.1:8012', 'model': 'synthetic-local'}})}))
'''
        env = {'HOME': str(home), 'MCS_ROOT': str(private / 'data-root'), 'MCS_UPDATE_REPO': str(repo),
               'TMPDIR': str(private), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(repo)], cwd=private,
                                env=env, capture_output=True, check=True, timeout=60)
        return json.loads(result.stdout)


def build(previous):
    components = {key: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                  for key, path in COMPONENTS.items()}
    return {'schema': 'attachment-refactor-witness/v1', 'baseline': BASELINE,
            'baseline_code_digest': previous['code'], 'versions': previous['versions'],
            'components': components,
            'replacements': {key: previous['sources'][key] for key in components if key in previous['sources']},
            'additions': {key: value for key, value in components.items() if key not in previous['sources']}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check the fixed reviewed witness without writing')
    args = parser.parse_args()
    # This records a reviewed refactor; it does not establish semantic equivalence.
    value = json.dumps(build(baseline()), ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if args.check:
        return 0 if TARGET.is_file() and TARGET.read_text() == value else 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(value)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
