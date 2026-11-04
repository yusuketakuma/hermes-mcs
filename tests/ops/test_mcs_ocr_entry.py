"""Synthetic OCR entry checks keep model work outside the ingestion lock."""
from types import SimpleNamespace

import mcs_config
import mcs_ocr


def test_disabled_does_not_open_or_probe_the_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled OCR accessed runtime or database")
    monkeypatch.setattr(mcs_ocr, "Ledger", forbidden)
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", forbidden)
    assert mcs_ocr.run({}) == {"reason": "disabled"}


def test_entry_releases_the_ingestion_lock_before_ocr(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.db"
    database.touch()
    events = []
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"}}
    monkeypatch.setattr(mcs_ocr, "DB", str(database))
    monkeypatch.setattr(mcs_ocr, "DATA", str(tmp_path))
    monkeypatch.setattr(mcs_ocr.writer_lock, "updater_state", lambda: (False, "generation"))
    monkeypatch.setattr(mcs_ocr, "acquire_run_lock", lambda path: 123)
    monkeypatch.setattr(mcs_ocr.os, "close", lambda fd: events.append("unlock"))
    monkeypatch.setattr(mcs_ocr, "Ledger", lambda path: SimpleNamespace(close=lambda: events.append("close")))

    def engine(ledger, settings, *, deadline, limit):
        assert events == ["unlock"]
        assert settings is cfg and deadline > 0 and limit == 1
        events.append("ocr")
        return {"complete": 1}

    monkeypatch.setattr(mcs_ocr.attachment_ocr, "run_pending", engine)
    assert mcs_ocr.run(cfg) == {"complete": 1}
    assert events == ["unlock", "ocr", "close"]


def test_ocr_config_uses_the_shared_runtime_validation():
    rule = mcs_config.CONFIG_RULES["attachment_ocr"][1]
    assert rule({"enabled": False}) is None
    assert rule({"enabled": "yes"})
    assert rule({"enabled": True, "executable": "relative"})
    assert rule({"enabled": True, "executable": "/synthetic/yomitoku", "timeout_seconds": float("nan")})
