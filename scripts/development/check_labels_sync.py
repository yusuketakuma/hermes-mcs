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
    if mode == 'api_failure' or (mode == 'inventory_failure' and '--slurp' in args):
        sys.exit(1)
    assert '--paginate' in args
    if '--slurp' in args:
        if mode == 'malformed':
            print('{}')
        else:
            print(json.dumps([state[:1], state[1:]]))
    else:
        print('\n'.join(x['name'] for x in state))
    sys.exit(0)
with open(os.environ['LOG'], 'a') as f:
    f.write(json.dumps(args) + '\n')
if mode == 'write_failure' and len(Path(os.environ['LOG']).read_text().splitlines()) == 3:
    sys.exit(1)
if args[:2] == ['label', 'edit']:
    label = next(x for x in state if x['name'] == args[2])
    if '--name' in args:
        new = args[args.index('--name') + 1]
        if mode == 'race':
            state.append(dict(name=new, color='ffffff', description='concurrent'))
        if any(x['name'] == new for x in state):
            p.write_text(json.dumps(state))
            sys.exit(1)
        label['name'] = new
    for flag, key in [('--color', 'color'), ('--description', 'description')]:
        if flag in args:
            label[key] = args[args.index(flag) + 1]
elif args[:2] == ['label', 'create']:
    assert '--force' not in args
    if any(x['name'] == args[2] for x in state):
        sys.exit(1)
    state.append(dict(name=args[2], color=args[args.index('--color') + 1],
                      description=args[args.index('--description') + 1]))
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
    desired = [{k: label[k] for k in ('name', 'color', 'description')} for label in labels]
    old = [dict(label, name=next((x['rename_from'] for x in renames
                                 if x['name'] == label['name']), label['name'])) for label in desired]
    unknown = dict(name='manual-owner-label', color='abcdef', description='keep')
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        stub = directory / 'gh'
        stub.write_text(STUB)
        stub.chmod(0o755)
        state = directory / 'state.json'
        log = directory / 'calls.jsonl'

        def run(mode, existing=None):
            if existing is not None:
                state.write_text(json.dumps(existing))
            log.write_text('')
            env = {'PATH': tmp + ':' + os.environ['PATH'], 'HOME': tmp,
                   'MODE': mode, 'STATE': str(state), 'LOG': str(log),
                   'GH_REPO': 'synthetic/example'}
            result = subprocess.run(['bash', '-c', script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=60)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            assert not any('delete' in call or '--force' in call for call in calls)
            return result, calls

        result, calls = run('same', desired + [unknown])
        assert result.returncode == 0 and calls == [], result.stderr
        assert json.loads(state.read_text()) == desired + [unknown]
        for field, value in [('color', 'ffffff'), ('description', 'changed')]:
            altered = [dict(x) for x in desired]
            altered[0][field] = value
            result, calls = run('changed', altered)
            assert result.returncode == 0, result.stderr
            assert calls == [['label', 'edit', desired[0]['name'], '--' + field, desired[0][field]]]
            assert json.loads(state.read_text()) == desired
        result, calls = run('new', desired[1:])
        assert result.returncode == 0 and len(calls) == 1, result.stderr
        assert calls[0][:3] == ['label', 'create', desired[0]['name']]
        result, calls = run('empty', [])
        assert result.returncode == 0 and len(calls) == len(desired), result.stderr
        assert all(call[:2] == ['label', 'create'] for call in calls)
        result, calls = run('same')
        assert result.returncode == 0 and not calls, result.stderr
        result, calls = run('rename', old + [unknown])
        assert result.returncode == 0 and len(calls) == len(renames), result.stderr
        assert all('--name' in call for call in calls)
        assert json.loads(state.read_text()) == desired + [unknown]
        collision = old + [next(x for x in desired if x['name'] == renames[0]['name'])]
        for mode, existing in [('collision', collision), ('api_failure', desired),
                               ('inventory_failure', desired), ('malformed', desired)]:
            result, calls = run(mode, existing)
            assert result.returncode != 0 and not calls, (mode, result.stderr)
            assert json.loads(state.read_text()) == existing
        result, calls = run('write_failure', old)
        assert result.returncode != 0 and len(calls) == 3
        result, calls = run('resume')
        assert result.returncode == 0 and len(calls) == len(renames) - 2, result.stderr
        assert json.loads(state.read_text()) == desired
        changed = [dict(x, description='outdated') for x in desired]
        result, calls = run('write_failure', changed)
        assert result.returncode != 0 and len(calls) == 3
        result, calls = run('resume')
        assert result.returncode == 0 and len(calls) == len(desired) - 2, result.stderr
        assert json.loads(state.read_text()) == desired
        null_description = [dict(x) for x in desired]
        null_description[0]['description'] = None
        result, calls = run('null_description', null_description)
        assert result.returncode == 0, result.stderr
        assert calls == [['label', 'edit', desired[0]['name'], '--description', desired[0]['description']]]
        result, calls = run('race', old)
        assert result.returncode != 0 and len(calls) == 1
        result, calls = run('collision')
        assert result.returncode != 0 and not calls
        uppercase = [dict(x, color=x['color'].upper()) for x in desired]
        result, calls = run('same', uppercase)
        assert result.returncode == 0 and not calls, result.stderr
    print('PASS: offline delta sync, pagination, rename, failure, resume and race checks')


if __name__ == '__main__':
    main()
