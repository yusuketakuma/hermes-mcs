"""Committed progress queues updates without sending or breaking source ownership."""
import sqlite3
from types import SimpleNamespace

import extraction_refresh
import extract_llm
import notify_cards
import semantic_extraction
from ledger import Ledger


def test_refresh_uses_existing_queue_and_skips_uncommitted_work(monkeypatch):
    db = sqlite3.connect(":memory:")
    ledger = SimpleNamespace(db=db)
    calls = []
    cfg = {"notify": {"interactive": "slack"}}
    monkeypatch.setattr(notify_cards, "rerender_message_cards",
                        lambda ledger, cfg, pid, mid: calls.append((pid, mid)) or [7])
    assert extraction_refresh.refresh_extraction_cards(ledger, 1, 2, cfg=cfg) == [7]
    db.execute("BEGIN")
    assert extraction_refresh.refresh_extraction_cards(ledger, 1, 2, cfg=cfg) == []
    db.rollback()
    assert calls == [(1, 2)]
    assert extraction_refresh.refresh_extraction_cards(ledger, 1, 2, cfg={}) == []
    db.close()


def test_legacy_checkpoint_is_committed_before_refresh(tmp_path, monkeypatch):
    from test_extraction_review import _message
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.save_messages([_message()])
        row = dict(db.db.execute("SELECT * FROM messages").fetchone())
        calls = []

        def queued(ledger, pid, mid):
            assert not ledger.db.in_transaction
            assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1
            calls.append((pid, mid))

        monkeypatch.setattr(extract_llm, "_rerender_cards", queued)
        extract_llm._persist_chunks(db, row, {0: {}})
        assert calls == [(row["project_id"], row["message_id"])]
    finally:
        db.close()


def test_semantic_manifest_and_checkpoint_share_after_commit_refresh(tmp_path, monkeypatch):
    from test_extraction_review import _message
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        db.save_messages([_message()])
        calls = []

        def queued(ledger, pid, mid, **kwargs):
            assert not ledger.db.in_transaction
            calls.append((pid, mid))

        monkeypatch.setattr(extraction_refresh, "refresh_extraction_cards", queued)
        manifest = semantic_extraction.build_manifest("合成本文", "synthetic-source")
        semantic_extraction._persist_manifest(db, 1, 1, "synthetic-model", "synthetic-source", manifest, 4)
        semantic_extraction._persist_chunk(db, 1, 1, "synthetic-model", "synthetic-source",
                                          "a" * 64, "synthetic-revision",
                                          semantic_extraction._manifest_specs(manifest)[0], [], 0, 1)
        assert calls == [(1, 1), (1, 1)]
    finally:
        db.close()
