"""Japanese guided setup preserves stored values and validates every answer."""
import pytest

import mcs_setup


@pytest.mark.parametrize("raw, expected", [("はい", True), ("いいえ", False),
                                           ("y", True), ("n", False)])
def test_japanese_yes_no_keeps_boolean_storage(raw, expected):
    assert mcs_setup._parse_answer("bool", raw) == (True, expected)


@pytest.mark.parametrize("raw, expected", [("1", "hermes"), ("2", "standalone"),
                                           ("hermes", "hermes"), ("standalone", "standalone")])
def test_numbered_choices_keep_existing_config_values(raw, expected):
    assert mcs_setup._parse_answer("choice:hermes,standalone", raw) == (True, expected)


def test_invalid_choice_reprompts_and_enter_keeps_existing_value(monkeypatch, capsys):
    answers = iter(["3", ""])
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    assert mcs_setup._ask("runtime_mode", "choice:hermes,standalone", "hermes",
                          "どの方法で使いますか？", "standalone", present=True) == "standalone"
    assert len(prompts) == 2
    output = capsys.readouterr().out
    assert output.index("どの方法") < output.index("2. MCS だけで動かす")
    assert prompts[0].index("番号を選んでください") < prompts[0].index("runtime_mode")
    assert "現在: standalone" in prompts[0]
    assert "（現在の設定・Enter で保持）" in output


@pytest.mark.parametrize("cur, default, label, expected", [
    (None, False, "推奨: いいえ", False), (None, True, "推奨: はい", True),
    (False, True, "現在: いいえ", False), (True, False, "現在: はい", True),
])
def test_boolean_question_labels_keep_current_or_recommended_value(
        monkeypatch, cur, default, label, expected):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert mcs_setup._ask("synthetic", "bool", default, "使いますか？", cur) is expected
    assert label in prompts[0]


def test_unused_connectors_have_no_empty_steps(monkeypatch, capsys):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "local"}
    seen = set()

    def answer(prompt):
        key = prompt.split("設定項目: ", 1)[1].split()[0].rstrip(":")
        assert key not in seen, f"fixture answer was rejected: {key}"
        seen.add(key)
        return "いいえ" if key == "drug_map.bundled" else ""

    monkeypatch.setattr("builtins.input", answer)
    mcs_setup._wizard(cfg)
    output = capsys.readouterr().out
    assert "【手順 1】" in output
    assert "朝のまとめ通知" in output
    assert "Slackの接続先と操作できる人" not in output
    assert "Discordの接続先と操作できる人" not in output
    assert "LINE WORKSの接続先と操作できる人" not in output
    assert cfg["notify"]["interactive"] == "off"
    assert cfg["semantic"]["mode"] == "off"
    assert cfg["deep_history"] is True


def test_llm_url_question_contains_the_complete_post_endpoint():
    from local_llm import ENDPOINT

    question = next(desc for _, rows in mcs_setup.WIZARD
                    for key, _, _, desc, _ in rows if key == "local_llm.url")
    assert ENDPOINT in question
    assert "/v1/chat/completions まで入力" in question


def test_noninteractive_help_explains_hidden_password_environment(capsys):
    with pytest.raises(SystemExit) as stopped:
        mcs_setup.main(["init", "--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "read -r -s MCS_SETUP_PASSWORD" in output
    assert "export MCS_SETUP_PASSWORD" in output
    assert "unset MCS_SETUP_PASSWORD" in output
    assert "パスワードをコマンドへ直接書かず" in output


@pytest.mark.parametrize("answer,consented", [("はい", True), ("いいえ", False),
                                            ("", False), ("yes", True), ("n", False)])
def test_hermes_token_reuse_respects_japanese_consent(monkeypatch, tmp_path, capsys, answer, consented):
    from types import SimpleNamespace

    env = tmp_path / ".env"
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(env))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(mcs_setup, "env_value", lambda key, *, paths, check_env:
                        "synthetic-reuse-token" if paths != [str(env)] else None)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda _: "")
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", respond)
    result = mcs_setup._apply_standalone_credentials(
        SimpleNamespace(yes=False), {"discord"}, [("DISCORD_BOT_TOKEN", "DiscordのBotトークン")])
    assert result is consented
    assert "1=はい / 2=いいえ" in prompts[0]
    assert "推奨: いいえ" in prompts[0]
    assert env.exists() is consented
    if consented:
        assert env.read_text() == "DISCORD_BOT_TOKEN=synthetic-reuse-token\n"
    assert "synthetic-reuse-token" not in capsys.readouterr().out
