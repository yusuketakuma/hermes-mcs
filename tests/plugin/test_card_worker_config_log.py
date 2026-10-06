"""An opted-in card worker with incomplete config must say so once in
the log instead of silently never delivering queued cards."""
import logging

from hermes_plugin import card_workers


class _Ctx:
    def __init__(self, config):
        self.config = config

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_incomplete_opt_in_logs_worker_disabled(caplog):
    caplog.set_level(logging.INFO, logger="hermes.plugin")
    assert card_workers.make_discord_factory(
        _Ctx({"interactive": True}))(None, None) is None
    assert card_workers.make_slack_factory(
        _Ctx({"slack_adapter_enabled": True}))(None, None) is None
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        'mcs_discord worker_disabled {"reason": "config_incomplete"}',
        'mcs_slack worker_disabled {"reason": "config_incomplete"}']


def test_opted_out_stays_silent(caplog):
    caplog.set_level(logging.INFO, logger="hermes.plugin")
    assert card_workers.make_discord_factory(_Ctx({}))(None, None) is None
    assert card_workers.make_slack_factory(_Ctx({}))(None, None) is None
    assert caplog.records == []
