"""Config rewrites read config.json the way load_config does: a file that
load_config would read as {} (symlink, non-regular, oversized) is never
treated as an empty config and overwritten. Synthetic temp files only."""
import json
import os
import hashlib
from types import SimpleNamespace

import pytest

import mcs_setup
import mcs_util

TARGET = {"mcs_login_id": "synthetic-user", "notify": {"target": "slack:CSYNTHETIC"}}


def _unreadable_config(tmp_path, kind):
    conf = tmp_path / "c.json"
    if kind == "symlink":
        (tmp_path / "real.json").write_text(json.dumps(TARGET))
        conf.symlink_to(tmp_path / "real.json")
    elif kind == "oversized":
        conf.write_text(json.dumps({"pad": "x" * mcs_util.CONFIG_MAX_BYTES}))
    else:
        os.mkfifo(conf)
    return conf


def _snapshot(conf):
    if conf.is_symlink():
        return ("link", os.readlink(conf), (conf.parent / "real.json").read_bytes())
    if conf.is_fifo():
        return ("fifo",)
    return ("file", conf.read_bytes())


@pytest.mark.parametrize("kind", ["symlink", "oversized", "fifo"])
@pytest.mark.parametrize("command", ["init", "fact-source"])
def test_rewrite_refuses_config_that_load_config_reads_as_empty(
        monkeypatch, tmp_path, capsys, kind, command):
    conf = _unreadable_config(tmp_path, kind)
    before = _snapshot(conf)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    if command == "init":
        assert mcs_setup.cmd_init(SimpleNamespace(yes=False)) == 1
    else:
        assert mcs_setup.cmd_fact_source(
            SimpleNamespace(fact_source="shadow", gate_evidence=None)) == 1
    assert _snapshot(conf) == before
    assert "保存していません" in capsys.readouterr().out
    assert not list(tmp_path.glob("c.json.corrupt-*"))


def test_yes_moves_a_symlink_aside_without_touching_its_target(monkeypatch, tmp_path):
    conf = _unreadable_config(tmp_path, "symlink")
    real = tmp_path / "real.json"
    real.chmod(0o644)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    assert mcs_setup._guard_config(True) is True
    (moved,) = tmp_path.glob("c.json.corrupt-*")
    assert moved.is_symlink() and not conf.exists()
    assert json.loads(real.read_text()) == TARGET
    assert real.stat().st_mode & 0o777 == 0o644


def test_regular_config_within_the_limit_is_still_accepted(monkeypatch, tmp_path):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps(TARGET))
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    assert mcs_setup._config_problem() is None


def _synthetic_split(monkeypatch, root):
    import cloud_policy
    import provider_settings

    root.chmod(0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    directory = data / 'provider_settings'
    directory.mkdir(mode=0o700)
    common = {'mcs_login_id': 'synthetic', 'notify_target': 'local',
              'cloud_llm': {'mode': 'off', 'provider': 'openai'},
              'semantic': {'mode': 'off', 'fact_source': 'legacy'}, 'future_synthetic': {'keep': True}}
    providers = {'anthropic': {'model': cloud_policy.PROVIDERS['anthropic']},
                 'openai': {'model': cloud_policy.PROVIDERS['openai']},
                 'jev': {'daily_request_budget': 0, 'until': None}}
    def put(path, value):
        raw = (json.dumps(value, sort_keys=True) + '\n').encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()
    put(root / 'config.json', common)
    hashes = {name: put(directory / (name + '.json'), value) for name, value in providers.items()}
    put(data / '.provider-settings-authority.json', {'version': 2, 'state': 'committed', 'files': hashes,
        'common_policy_sha256': provider_settings.common_policy_digest(common)})
    monkeypatch.setattr(mcs_setup, 'CONF_PATH', str(root / 'config.json'))
    monkeypatch.setattr(mcs_setup, 'HOME', str(root))
    monkeypatch.setattr(mcs_util, 'HOME', str(root))
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *args, **kwargs: None)
    return common, providers


def _split_state(root):
    import cloud_policy
    import provider_settings
    import semantic_jev
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    return provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)


@pytest.mark.parametrize('command', ['features', 'fact-source'])
def test_split_common_commands_preserve_all_provider_values(tmp_path, monkeypatch, command):
    common, providers = _synthetic_split(monkeypatch, tmp_path)
    if command == 'features':
        monkeypatch.setattr(mcs_setup.sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard_optional_features',
                            lambda cfg, **kwargs: cfg.update(optional_features={'csdc': True}))
        monkeypatch.setattr('builtins.input', lambda *args: '1')
        assert mcs_setup.cmd_features(SimpleNamespace()) == 0
    else:
        assert mcs_setup.cmd_fact_source(SimpleNamespace(fact_source='shadow', gate_evidence=None)) == 0
    state = _split_state(tmp_path)
    assert state['providers'] == providers
    assert state['common']['future_synthetic'] == common['future_synthetic']
    if command == 'features':
        assert state['common']['optional_features'] == {'csdc': True}
        assert state['common']['semantic'] == common['semantic']
    else:
        assert state['common']['semantic']['fact_source'] == 'shadow'
    assert not (tmp_path / 'data' / '.provider-settings-pending.json').exists()


@pytest.mark.parametrize('kind', ['pending', 'provider'])
def test_split_features_rejects_change_during_confirmation(tmp_path, monkeypatch, kind):
    _, providers = _synthetic_split(monkeypatch, tmp_path)
    initial_common = (tmp_path / 'config.json').read_bytes()
    data = tmp_path / 'data'
    monkeypatch.setattr(mcs_setup.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard_optional_features',
                        lambda cfg, **kwargs: cfg.update(optional_features={'csdc': True}))
    path = (data / '.provider-settings-pending.json' if kind == 'pending'
            else data / 'provider_settings' / 'anthropic.json')
    raw = b'{"synthetic":"concurrent"}\n'
    def confirm(*args):
        path.write_bytes(raw)
        path.chmod(0o600)
        return '1'
    monkeypatch.setattr('builtins.input', confirm)
    assert mcs_setup.cmd_features(SimpleNamespace()) == 1
    assert path.read_bytes() == raw
    assert (tmp_path / 'config.json').read_bytes() == initial_common
    assert json.loads((data / 'provider_settings' / 'openai.json').read_text()) == providers['openai']


def test_split_features_declined_confirmation_preserves_every_file(tmp_path, monkeypatch):
    _synthetic_split(monkeypatch, tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    monkeypatch.setattr(mcs_setup.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard_optional_features',
                        lambda cfg, **kwargs: cfg.update(optional_features={'csdc': True}))
    monkeypatch.setattr('builtins.input', lambda *args: '2')
    assert mcs_setup.cmd_features(SimpleNamespace()) == 130
    assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before
