"""Japanese installer choices remain safe on invalid or closed input."""
import subprocess

import pytest

from test_install_sh import INSTALL


@pytest.mark.parametrize(('answer', 'selected'), [('\n', '1'), ('2\n', '2'), ('x\n2\n', '2')])
def test_guided_choice_retries_and_accepts_recommendation(answer, selected):
    text = INSTALL.read_text()
    function = text[text.index('choose() {'):text.index('\nMODE=install')]
    result = subprocess.run(['sh', '-c', 'set -e\n' + function + '\nchoose "選択: "\nprintf "picked=%s" "$_answer"'],
                            input=answer, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout.endswith('picked=' + selected)
    if answer.startswith('x'):
        assert '1 または 2 を入力してください' in result.stdout


@pytest.mark.parametrize('answer', ['', '0\n'])
def test_guided_choice_cancels_without_running_next_step(answer):
    text = INSTALL.read_text()
    function = text[text.index('choose() {'):text.index('\nMODE=install')]
    result = subprocess.run(['sh', '-c', 'set -e\n' + function + '\nchoose "選択: "\necho SHOULD_NOT_RUN'],
                            input=answer, capture_output=True, text=True, timeout=5)
    assert result.returncode == 130
    assert 'SHOULD_NOT_RUN' not in result.stdout
    assert '中止' in result.stdout


@pytest.mark.parametrize('answer', [b'0\n', b'2\n'])
def test_installer_confirmation_cancellation_is_not_success(tmp_path, answer):
    import os
    import selectors
    import shutil
    import sys
    import time

    from test_install_sh import _calls, _world

    if sys.platform != 'darwin' or not shutil.which('script'):
        pytest.skip('BSD script(1) gives the installer a terminal')
    home, _, stub_root, env = _world(tmp_path)
    with subprocess.Popen(
        ['script', '-q', '/dev/null', 'sh', str(INSTALL), '--mode', 'standalone',
         '--no-brew', '--no-llm'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env,
    ) as process, selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ)
        output = b''
        deadline = time.monotonic() + 10
        try:
            while '選択 [1・推奨 / 2 / 0・中止]: '.encode() not in output:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and ready.select(remaining), output
                chunk = os.read(process.stdout.fileno(), 4096)
                assert chunk, output
                output += chunk
            process.stdin.write(answer)
            process.stdin.flush()
            process.wait(timeout=10)
            tail, _ = process.communicate(timeout=10)
            output += tail
        finally:
            if process.poll() is None:
                process.kill()
        assert process.returncode == 130, output
    assert '中止しました'.encode() in output
    assert b'=== 1/6' not in output
    assert not (home / '.mcs').exists()
    assert not any(call.startswith(('brew install', 'uv pip', 'git clone')) for call in _calls(stub_root))
