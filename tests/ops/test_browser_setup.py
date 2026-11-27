"""Synthetic guided first sign-in: dedicated Chrome, the user's own login, one session check."""
import pytest

import browser_setup
import mcs_adapter
import mcs_cli
import mcs_setup
import setup_wizard


class _Adapter:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = []

    def open_browser_session(self, profile_dir, chrome_bin):
        self.calls.append((profile_dir, chrome_bin))
        return self.states.pop(0)


def _run(tmp_path, adapter, answers=(), **kwargs):
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    replies = list(answers)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return replies.pop(0)

    kwargs.setdefault("platform", "darwin")
    result = browser_setup.run(adapter=adapter, ask=ask, chrome_bin=str(chrome),
                               profile_dir=str(tmp_path / "profile"), **kwargs)
    return result, asked


def test_live_session_finishes_without_asking(tmp_path, capsys):
    adapter = _Adapter("ok")
    result, asked = _run(tmp_path, adapter)
    assert result == 0 and asked == []
    assert (tmp_path / "profile").stat().st_mode & 0o777 == 0o700
    out = capsys.readouterr().out
    assert "ログインした状態を確認しました" in out and "mcs doctor" in out


def test_waits_for_the_user_then_confirms(tmp_path):
    adapter = _Adapter("login_needed", "login_needed", "ok")
    result, asked = _run(tmp_path, adapter, ["", ""])
    assert result == 0 and len(asked) == 2 and len(adapter.calls) == 3


def test_quit_keeps_the_resume_command(tmp_path, capsys):
    result, _ = _run(tmp_path, _Adapter("login_needed"), ["q"])
    assert result == 130
    assert "mcs setup browser" in capsys.readouterr().out


@pytest.mark.parametrize("state,expected", [
    ("manual_required:multi_user_host", "管理担当者に相談"),
    ("failed", "Google Chrome"),
    ("unexpected", "Google Chrome"),
])
def test_stops_with_a_reason_and_an_action(tmp_path, capsys, state, expected):
    result, asked = _run(tmp_path, _Adapter(state))
    assert result == 1 and asked == []
    out = capsys.readouterr().out
    assert "停止:" in out and "対処:" in out and expected in out


def test_refuses_outside_macos_and_without_chrome(tmp_path, capsys):
    adapter = _Adapter()
    assert _run(tmp_path, adapter, platform="linux")[0] == 1
    assert browser_setup.run(adapter=adapter, ask=None, platform="darwin",
                             chrome_bin=str(tmp_path / "missing"),
                             profile_dir=str(tmp_path / "profile")) == 1
    assert adapter.calls == [] and not (tmp_path / "profile").exists()
    assert "Google Chromeが見つかりません" in capsys.readouterr().out


def test_public_route_reaches_the_browser_step(monkeypatch):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")
    seen = []
    monkeypatch.setattr(browser_setup, "run", lambda: seen.append(1) or 0)
    assert mcs_cli.main(["setup", "browser"]) == 0
    assert seen == [1]


def test_auto_login_shares_the_browser_session_step(monkeypatch):
    adapter = mcs_adapter.MCSAdapter()
    monkeypatch.setattr(mcs_adapter.MCSAdapter, "open_browser_session",
                        lambda self, *a: "manual_required:multi_user_host")
    assert adapter.auto_login("/synthetic/profile", "/synthetic/chrome") == "manual_required:multi_user_host"
    monkeypatch.setattr(mcs_adapter.MCSAdapter, "open_browser_session", lambda self, *a: "ok")
    assert adapter.auto_login("/synthetic/profile", "/synthetic/chrome") == "ok"


def test_offer_after_init_without_terminal_prints_the_command(monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup.sys.stdin, "isatty", lambda: False)
    mcs_setup._offer_browser_sign_in()
    assert "mcs setup browser" in capsys.readouterr().out


def test_recommended_course_keeps_the_installed_runtime(monkeypatch, capsys):
    asked = []

    def ask(key, kind, default, desc, cur, present=False, *, allow_back=False):
        asked.append(key)
        return "recommended" if key == "setup_detail" else (cur if cur is not None else default)

    monkeypatch.setattr(setup_wizard, "ask", ask)
    cfg = {"runtime_mode": "standalone", "mcs_login_id": "synthetic"}
    setup_wizard.wizard(cfg)
    assert "runtime_mode" not in asked and cfg["runtime_mode"] == "standalone"
    assert "導入時に選択済み" in capsys.readouterr().out
    asked.clear()
    setup_wizard.wizard({"mcs_login_id": "synthetic"})
    assert "runtime_mode" in asked
