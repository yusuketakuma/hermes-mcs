#!/usr/bin/env python3
"""Record a reviewed witness that keeps attachment results from the production baseline valid."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / 'resources' / 'inference-contracts' / 'attachment-refactor-20261105.json'
WITNESS = 'resources/inference-contracts/attachment-refactor-20261105.json'
WITNESS_KEY = 'attachment_refactor_witness'
CFG = {'local_llm': {'url': 'http://127.0.0.1:8012', 'model': 'synthetic-local'}}
PROGRAM = '''import sys, json
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'mcs'))
def audit(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'ctypes.dlopen'):
        raise RuntimeError('baseline_external_operation_forbidden')
sys.addaudithook(audit)
import _mcs_path
import extract_llm, extract_llm_source_contract, attachment_inference
code, sources = attachment_inference._CODE, dict(extract_llm._LOADED_SOURCE_DIGESTS)
versions = [extract_llm._INFERENCE_VERSION, extract_llm.EXTRACT_VERSION, extract_llm.PATIENT_CONTEXT_VERSION]
try:
    import attachment_inference_contract
    effective = attachment_inference_contract._compatible(code, sources, versions)
except ImportError:
    effective = None
if effective is not None:
    code, sources = effective
print(json.dumps({'code': code, 'versions': versions, 'sources': sources,
    'raw': dict(extract_llm._LOADED_SOURCE_DIGESTS),
    'hold': sorted(getattr(extract_llm_source_contract, '_HOLD_SOURCES', ())),
    'oracle': attachment_inference._contract(json.loads(sys.argv[2]))}))
'''


def inspect(repo):
    """Import one checkout under an empty HOME and deny network and subprocesses."""
    with tempfile.TemporaryDirectory(prefix='mcs-contract-witness-') as directory:
        private = Path(directory)
        home = private / 'home'
        home.mkdir()
        env = {'HOME': str(home), 'MCS_ROOT': str(private / 'data-root'), 'MCS_UPDATE_REPO': str(repo),
               'TMPDIR': str(private), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', PROGRAM, str(repo), json.dumps(CFG)],
                                cwd=private, env=env, capture_output=True, check=True, timeout=60)
        return json.loads(result.stdout)


def baseline(sha):
    """Return the contract inputs the baseline deploy actually hashed (its own witness applied)."""
    paths = ['mcs'] + subprocess.run(['git', 'ls-tree', '--name-only', sha, WITNESS], cwd=ROOT,
                                     capture_output=True, text=True, check=True).stdout.split()
    archived = subprocess.run(['git', 'archive', sha, *paths], cwd=ROOT, capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory(prefix='mcs-contract-baseline-') as directory:
        repo = Path(directory) / 'source'
        repo.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archived)) as archive:
            archive.extractall(repo, filter='data')
        return inspect(repo)


def build(sha, previous, current):
    """Map every source changed since the baseline back to it, refusing extraction-content changes."""
    if previous['versions'] != current['versions']:
        raise SystemExit('witness_refused: inference/extract/context version changed; results must be redone')
    now = {key: value for key, value in current['raw'].items() if key != WITNESS_KEY}
    removed = sorted(set(previous['sources']) - set(now) - {WITNESS_KEY})
    if removed:
        raise SystemExit(f'witness_refused: baseline sources were removed: {removed}')
    components = {key: value for key, value in now.items() if previous['sources'].get(key) != value}
    components['attachment_inference'] = now['attachment_inference']
    blocked = sorted(set(components) & set(current['hold']))
    if blocked:
        # These sources change what extraction produces (extract_llm_source_contract._HOLD_SOURCES).
        raise SystemExit(f'witness_refused: extraction-content sources changed: {blocked}')
    replacements = {key: previous['sources'][key] for key in components if key in previous['sources']}
    if WITNESS_KEY in previous['sources']:
        replacements[WITNESS_KEY] = previous['sources'][WITNESS_KEY]
    return {'schema': 'attachment-refactor-witness/v1', 'baseline': sha,
            'baseline_code_digest': previous['code'], 'versions': previous['versions'],
            'components': components, 'replacements': replacements,
            'additions': {key: value for key, value in components.items() if key not in previous['sources']}}


def render(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', help='full SHA of the deployed production commit whose results stay valid')
    parser.add_argument('--check', action='store_true', help='check the recorded witness without writing')
    args = parser.parse_args()
    # A witness records a human review that the diff since the baseline does not change
    # extraction output; it does not establish semantic equivalence by itself.
    if args.check:
        sha = json.loads(TARGET.read_text())['baseline']
    else:
        sha = args.baseline or parser.error('--baseline is required unless --check')
        if re.fullmatch(r'[0-9a-f]{40}', sha) is None:
            parser.error('--baseline must be a full 40-character commit SHA')
    previous = baseline(sha)
    value = render(build(sha, previous, inspect(ROOT)))
    if args.check:
        return 0 if TARGET.read_text() == value and inspect(ROOT)['oracle'] == previous['oracle'] else 1
    before = TARGET.read_text() if TARGET.is_file() else None
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(value)
    if inspect(ROOT)['oracle'] != previous['oracle']:
        if before is None:
            TARGET.unlink()
        else:
            TARGET.write_text(before)
        raise SystemExit('witness_refused: the current checkout does not reproduce the baseline token')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
