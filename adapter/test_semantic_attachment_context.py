"""Fixed bundles retain attachment identity without exposing signed URLs."""
import semantic
from test_mcs_semantic import _llm, _seeded


def test_attachment_revision_is_bound_and_unparsed_content_is_explicit(tmp_path):
    db = _seeded(tmp_path)
    try:
        before = semantic.thread_bundle(db, 1, 1)
        with db.db:
            db.db.execute("INSERT INTO attachments(message_id,file_id,name,url,local_path,sha256,state) VALUES(1,'file','scan.pdf','https://invalid.example/signed?token=synthetic','/private/file','old','done')")
        bundle = semantic.thread_bundle(db, 1, 1)
        assert bundle["source_fingerprint"] != before["source_fingerprint"]
        attachment = bundle["members"][0]["attachments"][0]
        assert attachment["sha256"] == "old"
        assert "url" not in attachment and "local_path" not in attachment
        facts, _, _ = semantic.extract_facts(_llm, bundle["members"][0])
        summary = semantic.summarize(_llm, bundle, 1, facts, {})
        assert any("添付内容は解析していません" in item for item in summary["limitations"])
        with db.db:
            db.db.execute("UPDATE attachments SET sha256='new'")
        assert semantic.thread_bundle(db, 1, 1)["source_fingerprint"] != bundle["source_fingerprint"]
        assert attachment["sha256"] == "old"
    finally:
        db.close()


def test_download_revision_requeues_only_enabled_changed_generation(tmp_path, monkeypatch):
    import json
    db = _seeded(tmp_path)
    try:
        with db.db:
            cursor = db.db.execute(
                "INSERT INTO attachments(message_id,file_id,name,state) VALUES(1,'file','scan.pdf','pending')")
            attachment_id = cursor.lastrowid
            db.db.execute("UPDATE fetch_jobs SET state='done' WHERE kind='semantic'")
        db.attachment_saved(attachment_id, '/synthetic/file', 10, 'first')
        assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 'done'
        before = db._semantic_source_generation(1, 1)
        db.attachment_saved(attachment_id, '/synthetic/file', 20, 'second', semantic=True)
        row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert row['state'] == 'pending'
        payload = json.loads(row['payload'])
        assert payload['targets'] == [1, 2]
        assert db._semantic_source_generation(1, 1) != before
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='done' WHERE kind='semantic'")
        db.attachment_saved(attachment_id, '/synthetic/new-path', 20, 'second', semantic=True)
        assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 'done'
        db.attachment_failed(attachment_id, 'synthetic', max_attempts=1, semantic=True)
        assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 'pending'
        assert db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind='semantic_notice'").fetchone()[0] == 0
        import pytest
        original = dict(db.db.execute("SELECT * FROM attachments WHERE attachment_id=?",
                                      (attachment_id,)).fetchone())
        def fail_seed(*args):
            raise RuntimeError("synthetic seed interruption")
        monkeypatch.setattr(db, "_semantic_seed_tx", fail_seed)
        with pytest.raises(RuntimeError, match="synthetic seed interruption"):
            db.attachment_saved(attachment_id, '/synthetic/new', 30, 'third', semantic=True)
        assert dict(db.db.execute("SELECT * FROM attachments WHERE attachment_id=?",
                                  (attachment_id,)).fetchone()) == original
    finally:
        db.close()


def test_attachment_only_post_remains_full_and_explicitly_unparsed(tmp_path):
    import ledger
    import mcs_adapter

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, *args, **kwargs):
            return {"messages": [{"id": 1, "comment": "",
                                  "created_at": "2026-09-19T00:00:00+09:00",
                                  "files": [{"name": "scan.pdf",
                                             "url": "https://example.invalid/files/1"}]}],
                    "paginate": {"has_next": False}}

    batch = Adapter().fetch_unread_messages(1, 123)
    assert batch.reached and batch.error is None
    path = str(tmp_path / "attachment-only.db")
    db = ledger.Ledger(path)
    patient = mcs_adapter.UnreadPatient(1, "medical", "synthetic", "", "", "",
                                        messages=batch.messages,
                                        fetch_state="complete")
    assert db.save_patient(patient, semantic=True) == [1]
    db.close()
    db = ledger.Ledger(path)
    try:
        bundle = semantic.thread_bundle(db, 1, 1)
        member = bundle["members"][0]
        assert bundle["content_quality"] == "full"
        assert member["body_original"] == ""
        assert member["attachments"][0]["name"] == "scan.pdf"
        summary = semantic.summarize(
            lambda _: '{"claims":[],"limitations":[]}', bundle, 1, [], {})
        assert summary["claims"] == []
        assert any("添付内容は解析していません" in text
                   for text in summary["limitations"])
        assert db.job_pending("semantic", 1, 1) is not None
    finally:
        db.close()
