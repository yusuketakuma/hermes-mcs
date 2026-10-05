"""apply_card_resolve without cfg must read MCS_ROOT's config, not ~/.mcs."""
from __future__ import annotations

import pytest

import mcs_util
import notify_transport


class _Stop(Exception):
    pass


def test_card_resolve_default_cfg_uses_conf_path(tmp_path, monkeypatch):
    seen = []
    conf = str(tmp_path / "root" / "config.json")
    monkeypatch.setattr(mcs_util, "CONF_PATH", conf)
    monkeypatch.setattr(mcs_util, "load_config",
                        lambda path=None: seen.append(path) or {})
    monkeypatch.setattr(notify_transport.cards, "_db", lambda ledger: None)

    def stop(_req):
        raise _Stop

    monkeypatch.setattr(notify_transport, "payload_hash", stop)
    with pytest.raises(_Stop):
        notify_transport.apply_card_resolve(object(), {"command_id": "c"})
    assert seen == [conf]
