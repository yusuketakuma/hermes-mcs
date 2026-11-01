"""Execute only the installer service-stage hook against synthetic private logs."""
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]


def stage(tmp_path, *, broken=False, no_services=False):
    home, checkout = tmp_path / 'home', tmp_path / 'checkout'
    data = home / '.mcs/data'
    data.mkdir(parents=True, mode=0o700)
    (home / '.mcs').chmod(0o700)
    home.chmod(0o700)
    for name in ('mcs/ops/run_log_install.py', 'mcs/core/run_log.py'):
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / name).read_bytes())
        target.chmod(0o644)
    source = data / 'run.log'
    if broken:
        outside = tmp_path / 'retained.log'
        outside.write_bytes(b'synthetic outside original\n')
        source.symlink_to(outside)
    else:
        source.write_bytes(b'synthetic installer prefix\n')
        source.chmod(0o600)
    setup = checkout / 'setup.py'
    setup.write_text('import os\nfrom pathlib import Path\n'
                     'Path(os.environ["TRACE"]).write_text("services")\n')
    script = (REPO / 'install.sh').read_text()
    start = script.index('# Install the independent helper before either llama')
    end = script.index('# --------------------------------- 6. update recovery', start)
    guard_start = script.index('    _root_alias=""')
    guard_end = script.index('    _safe_dir "$HOME" ||', guard_start)
    lock_start = script.index('    _lock_info() {')
    lock_end = script.index('    _held_info() {', lock_start)
    runtime_start = script.index('verify_split_runtime() {')
    runtime_end = script.index('\n_verify_root_alias ||', runtime_start)
    harness = tmp_path / 'service-stage.sh'
    harness.write_text('set -eu\nsay() { :; }\nskip() { :; }\nsum() { :; }\nok() { :; }\n'
                       'shell_quote() { printf "%s" "$1"; }\n'
                       'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
                       + script[lock_start:lock_end] + script[guard_start:guard_end]
                       + '_split_runtime_token=""\n' + script[runtime_start:runtime_end] + '\n'
                       + 'SETUP=' + shlex.quote(str(setup)) + '\n' + script[start:end])
    trace = tmp_path / 'services.trace'
    environment = {**os.environ, 'HOME': str(home), 'MCS_ROOT': str(home / '.mcs'),
                   'MCS_DATA': str(data), 'REPO': str(checkout), 'MCS_PY': sys.executable,
                   'RECOVERY_DIR': str(home / '.mcs-recovery'),
                   'SKIP_LLM': '1', 'RUNTIME_MODE': 'standalone',
                   'SKIP_SERVICES': '1' if no_services else '0', 'TRACE': str(trace)}
    result = subprocess.run(['/bin/sh', str(harness)], env=environment,
                            capture_output=True, text=True, timeout=15)
    return result, home, data, trace


@pytest.mark.parametrize('no_services', [False, True])
def test_installer_prepares_verified_sink_before_optional_services(tmp_path, no_services):
    result, home, data, trace = stage(tmp_path, no_services=no_services)
    assert result.returncode == 0, result.stderr
    assert trace.exists() is (not no_services)
    assert (data / 'mcs.log').read_bytes() == (data / 'run.log').read_bytes()
    assert len(list((home / '.mcs-recovery').glob('run_log-*.json'))) == 1
    assert result.stderr == ''
    assert result.stdout == ''  # The hook does not capture or replay interactive installer output.


def test_installer_holds_before_services_when_legacy_source_is_untrusted(tmp_path):
    result, _, data, trace = stage(tmp_path, broken=True)
    assert result.returncode != 0 and not trace.exists()
    assert 'ログの統合準備を確認できません' in result.stderr
    assert (data / 'run.log').is_symlink()
    assert (tmp_path / 'retained.log').read_bytes() == b'synthetic outside original\n'


@pytest.mark.parametrize('kind', ['llama', 'recovery'])
def test_installer_template_prefix_uses_verified_private_helper(tmp_path, kind):
    result, home, data, _ = stage(tmp_path)
    assert result.returncode == 0, result.stderr
    import run_log_install
    helper = run_log_install.helper_path(home / '.mcs', home / '.mcs-recovery',
                                         require_installed=True)
    checkout = tmp_path / 'checkout'
    relative = 'deployment/launchagents/' + ('ai.mcs.llamaserver.plist' if kind == 'llama'
                                            else 'org.mcs.recovery.plist')
    target = checkout / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((REPO / relative).read_bytes())
    text = (REPO / 'install.sh').read_text()
    if kind == 'llama':
        start = text.index('        sed -e "s|__LLAMA_BIN__|')
        end = text.index('        _changed=0', start)
    else:
        start = text.index('        sed -e "s|__RECOVERY__|')
        end = text.index('        if publish ', start)
    output = tmp_path / 'rendered.plist.tmp'
    command = 'xml_sed() { printf "%s" "$1"; }\n' + text[start:end]
    environment = {**os.environ, 'REPO': str(checkout), 'MCS_PY': sys.executable,
        'RECOVERY_PYTHON': sys.executable, 'RUN_LOG_HELPER': helper,
        'RECOVERY_DIR': str(home / '.mcs-recovery'), 'MCS_DATA': str(data),
        'RUNTIME_HOME': str(home / 'runtime'), 'LLAMA_BIN': '/synthetic/llama-server',
        'MODEL_FILE': '/synthetic/model.gguf', 'LLAMA_PLIST': str(output)[:-4],
        'WATCH_PLIST': str(output)[:-4], 'RECOVERY_TEMPLATE': str(target)}
    rendered = subprocess.run(['/bin/sh', '-eu', '-c', command], env=environment,
                              capture_output=True, text=True, timeout=10)
    assert rendered.returncode == 0, rendered.stderr
    raw = output.read_bytes()
    assert b'__' not in raw
    args = plistlib.loads(raw)['ProgramArguments']
    assert args[:7] == [sys.executable, '-I', helper, 'exec', '--data-dir', str(data), '--']
    assert args[7] == ('/synthetic/llama-server' if kind == 'llama' else sys.executable)
