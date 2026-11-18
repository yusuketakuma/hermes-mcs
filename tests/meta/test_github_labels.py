"""GitHub label configuration and offline shell regressions (no API calls)."""
import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/labeler.yml'


def test_safety_labels_are_add_only_and_area_labels_still_sync():
    text = WORKFLOW.read_text()
    # Both actions use the same reviewed pin and trusted base configuration.
    actions = re.findall(r'uses: actions/labeler@([a-f0-9]{40})\s*[^\n]*\n'
                         r'        with:\n'
                         r'          configuration-path: ([^\n]+)\n'
                         r'          sync-labels: (true|false)', text)
    assert len(actions) == 2
    assert {pin for pin, _, _ in actions} == {
        'bf12e9b00b37c5c0ca2b87b79b2daf7891dbda13'}
    assert [(path, sync) for _, path, sync in actions] == [
        ('.github/labeler-safety.yml', 'false'), ('.github/labeler.yml', 'true')]
    areas = (ROOT / '.github/labeler.yml').read_text()
    safety = (ROOT / '.github/labeler-safety.yml').read_text()
    assert '"安全ゲート":' not in areas
    assert re.findall(r'^"([^"]+)":', safety, re.M) == ['安全ゲート']
    assert 'ci/gates.py' in safety and 'ci/mine_gates.py' in safety
    assert 'actions/checkout@' not in text
    assert 'pull_request_target:' in text and 'permissions: {}' in text
    assert 'contents: write' not in text and 'secrets.' not in text


def test_base_changes_recalculate_labels_without_title_only_runs():
    text = WORKFLOW.read_text()
    assert 'ready_for_review, edited]' in text
    assert "if: github.event.action != 'edited' || github.event.changes.base" in text


def test_all_auto_and_form_labels_exist_and_are_unique():
    definitions = json.loads((ROOT / '.github/labels.json').read_text())['labels']
    names = [entry['name'] for entry in definitions]
    assert len(names) == len(set(names))
    assert all(re.fullmatch('[a-fA-F0-9]{6}', entry['color']) for entry in definitions)
    for config in ('.github/labeler.yml', '.github/labeler-safety.yml'):
        assert set(re.findall(r'^"([^"]+)":', (ROOT / config).read_text(), re.M)) <= set(names)
    assert {f'size: {size}' for size in ('XS', 'S', 'M', 'L', 'XL')} <= set(names)
    # Type labels are pre-existing in GitHub, not maintained by labels.json.
    existing = {'bug', 'enhancement', 'documentation', 'dependencies'}
    for form in (ROOT / '.github/ISSUE_TEMPLATE').glob('*.yml'):
        match = re.search(r'^labels: (\[.*\])$', form.read_text(), re.M)
        if match:
            assert set(json.loads(match[1])) <= set(names) | existing


@pytest.mark.parametrize('changed,size', [
    ('0+0', 'XS'), ('24+25', 'XS'), ('25+25', 'S'), ('100+99', 'S'),
    ('100+100', 'M'), ('250+249', 'M'), ('250+250', 'L'),
    ('500+499', 'L'), ('500+500', 'XL'), ('9999+1', 'XL'),
])
def test_size_boundaries_preserve_manual_labels(tmp_path, changed, size):
    text = WORKFLOW.read_text()
    script = textwrap.dedent(text.split('        run: |\n', 1)[1])
    # Shell function intercepts every gh call; PATH has no network client execution.
    stub = '''\n    gh() {\n      if [[ "$1 $2" == 'pr view' ]]; then\n        printf '%s\\n' '安全ゲート' '要オーナー対応' 'size: XL'\n      elif [[ "$1 $2" == 'pr edit' ]]; then\n        printf '%s\\n' "$4=$5" >> "$LOG"\n      else\n        return 99\n      fi\n    }\n    '''
    log = tmp_path / 'calls'
    result = subprocess.run(['bash', '-c', textwrap.dedent(stub) + script],
                            env={'PATH': '/usr/bin:/bin', 'CHANGED': changed,
                                 'PR': '82', 'LOG': str(log)},
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert calls == ([] if size == 'XL' else [
        '--remove-label=size: XL', f'--add-label=size: {size}'])
