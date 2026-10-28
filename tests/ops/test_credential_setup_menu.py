"""認証設定の選択と保存先の振り分けを合成環境で確かめる。"""
from types import SimpleNamespace

import credential_setup_menu as menu


def test_standalone_routes_each_selection_to_its_own_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(menu.api_key_setup, "is_terminal", lambda: True)
    choices = iter(["1", "2", "3", "4", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    seen = []
    monkeypatch.setattr(menu.api_key_setup, "run", lambda root: seen.append(("api", root)) or 0)
    monkeypatch.setattr(menu.subprocess, "run", lambda command, **kwargs: (
        seen.append((command, kwargs)) or SimpleNamespace(returncode=0)))
    assert menu.run(tmp_path, {"runtime_mode": "standalone"}) == 0
    assert seen[0] == ("api", tmp_path)
    assert [row[0][2] for row in seen[1:]] == ["mcs_standalone", "mcs_standalone", "lineworks_adapter"]
    assert seen[1][0][-2:] == ["--transport", "slack"]
    assert seen[2][0][-2:] == ["--transport", "discord"]
    assert all(str(tmp_path) in row[0] for row in seen[1:])


def test_hermes_uses_explicit_profile_and_reports_failure(monkeypatch, tmp_path):
    import hermes_credential_setup
    monkeypatch.setattr(menu.api_key_setup, "is_terminal", lambda: True)
    choices = iter(["2", "profile-a", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    monkeypatch.setattr(menu.mcs_util, "hermes_exe", lambda cfg: "/synthetic/hermes")
    seen = []
    monkeypatch.setattr(hermes_credential_setup, "run", lambda *args: seen.append(args) or 1)
    assert menu.run(tmp_path, {}) == 1
    assert seen == [("/synthetic/hermes", "profile-a", "slack")]


def test_noninteractive_never_enters_a_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(menu.api_key_setup, "is_terminal", lambda: False)
    monkeypatch.setattr(menu.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    assert menu.run(tmp_path, {}) == 2


def test_failed_selection_can_be_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(menu.api_key_setup, "is_terminal", lambda: True)
    choices = iter(["4", "4", "0"])
    results = iter([1, 0])
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    monkeypatch.setattr(menu.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=next(results)))
    assert menu.run(tmp_path, {}) == 0
