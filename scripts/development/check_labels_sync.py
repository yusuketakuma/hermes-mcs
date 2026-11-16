"""Offline regression check of the workflow's actual Bash with a stateful gh stub.

Run: python3 scripts/development/check_labels_sync.py (requires bash and jq).
No GitHub token or network is used; no application code is imported.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
p = Path(os.environ['STATE'])
state = json.loads(p.read_text())
mode = os.environ['MODE']
if args[0] == 'api':
    if mode == 'api_failure':
        sys.exit(1)
    print('\n'.join(state))
    sys.exit(0)
with open(os.environ['LOG'], 'a') as f:
    f.write(json.dumps(args) + '\n')
if args[:2] == ['label', 'edit']:
    old, new = args[2], args[4]
    count = int(os.environ['FAIL_AT'])
    calls = Path(os.environ['LOG']).read_text().splitlines()
    if mode == 'rename_failure' and len(calls) == count:
        sys.exit(1)
    if mode == 'race':
        state.append(new)  # another writer creates the destination after snapshot
    if old not in state or new in state:
        p.write_text(json.dumps(state))
        sys.exit(1)
    state[state.index(old)] = new
elif args[:2] == ['label', 'create']:
    if args[2] not in state:
        state.append(args[2])
else:
    sys.exit('unexpected gh operation')
p.write_text(json.dumps(state))
'''


def main():
    labels = json.loads((ROOT / '.github/labels.json').read_text())['labels']
    names = [label['name'] for label in labels]
    renames = [label for label in labels if 'rename_from' in label]
    assert len(names) == len(set(names))
    assert all(re.fullmatch('[0-9a-f]{6}', label['color']) for label in labels)
    assert not any(name.startswith('領域:') for name in names)
    assert len({label['rename_from'] for label in renames}) == len(renames)
    mapping = (ROOT / '.github/labeler.yml').read_text()
    assert set(re.findall(r'^"([^"]+)":', mapping, re.M)) <= set(names)
    workflow = (ROOT / '.github/workflows/labels-sync.yml').read_text()
    part = workflow.split('      - name: Sync labels\n', 1)[1]
    part = part.split('        run: |\n', 1)[1].split('      - name:', 1)[0]
    script = '\n'.join(line[10:] for line in part.splitlines())
    subprocess.run(['bash', '-n'], input=script, text=True, check=True)
    old = [label['rename_from'] for label in renames]
    new = [label['name'] for label in renames]
    assert len(old) >= 3, 'partial-migration fixture requires at least 3 renames'
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        stub = directory / 'gh'
        stub.write_text(STUB)
        stub.chmod(0o755)
        state = directory / 'state.json'
        log = directory / 'calls.jsonl'

        def run(mode, existing=None, fail_at=1):
            if existing is not None:
                state.write_text(json.dumps(existing))
            log.write_text('')
            # Deliberately exclude inherited tokens and credentials.
            env = {'PATH': tmp + ':' + os.environ['PATH'], 'HOME': tmp,
                   'MODE': mode, 'STATE': str(state), 'LOG': str(log),
                   'FAIL_AT': str(fail_at), 'GH_REPO': 'synthetic/example'}
            result = subprocess.run(['bash', '-c', script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=30)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            assert not any('delete' in call for call in calls)
            return result, calls

        def success(mode, existing, edits):
            result, calls = run(mode, existing)
            assert result.returncode == 0, result.stderr
            assert sum(call[:2] == ['label', 'edit'] for call in calls) == edits
            assert sum(call[:2] == ['label', 'create'] for call in calls) == len(names)
            assert set(json.loads(state.read_text())) == set(names)

        success('old', old, len(old))
        success('new', new, 0)
        success('empty', [], 0)
        for mode, existing in [('collision', old + new[:1]), ('api_failure', old)]:
            result, calls = run(mode, existing)
            assert result.returncode != 0 and not calls
            assert json.loads(state.read_text()) == existing
        result, calls = run('rename_failure', old)
        assert result.returncode != 0 and len(calls) == 1
        assert json.loads(state.read_text()) == old
        # Two successful renames, then interruption; retry consumes fresh inventory.
        result, calls = run('rename_failure', old, fail_at=3)
        assert result.returncode != 0 and len(calls) == 3
        partial = new[:2] + old[2:]
        assert json.loads(state.read_text()) == partial
        success('resume', partial, len(old) - 2)
        # Destination created after initial inventory: stop, never overwrite/delete.
        result, calls = run('race', old)
        assert result.returncode != 0 and len(calls) == 1
        assert json.loads(state.read_text()) == old + new[:1]
        result, calls = run('collision')
        assert result.returncode != 0 and not calls
    print('PASS: schema, Bash syntax, 8 offline migration scenarios (including resume/race)')


if __name__ == '__main__':
    main()
