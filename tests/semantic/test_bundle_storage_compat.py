"""Synthetic bundle JSON compatibility; this does not test whole-runtime rollback."""
import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import mcs_backup
import notify_spool
import pattern_cards
import semantic_store
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_mcs_backup import KEY, world  # noqa: F401 - pytest fixture


# Exact function from semantic_store.py at 020a87b4558b2958780556218431d7e32592868f.
# Keeping the bounded excerpt makes this offline test work in shallow CI clones.
_LEGACY_FUNCTION = 'def current_artifact(db, kind: str, message_id: int, fp: str, policy=None):\n    """Latest artifact of `kind` for `message_id` whose meta fingerprint\n    still matches the live input — older generations stay recorded as\n    history but are never \'current\' (AT-035/056)."""\n    for r in db.execute(\n            "SELECT content,meta FROM artifacts WHERE kind=? "\n            "AND message_id=? ORDER BY artifact_id DESC",\n            (kind, message_id)):\n        meta = loads_dict(r["meta"])\n        if meta is None:\n            continue\n        if kind == KIND_ASSESS and meta.get("fact_id") is not None:\n            continue\n        # an invalidated row (projection expiry, or a summary/audit/coverage\n        # held after its canonical document was rebuilt) is never reused\n        if meta.get("invalidated"):\n            continue\n        if meta.get("fingerprint") == fp and (\n                policy is None or meta.get("policy_fingerprint") == policy):\n            content = loads_dict(r["content"])\n            if content is None:\n                return None\n            return {"content": content, "meta": meta}\n    return None\n'
_LEGACY_FUNCTION_SHA256 = "767726ac668e399211a25d151022e4365ee16a21a07a2c9e29e9ee33ed2b5ef7"


def _document():
    return {"bundle_id": "synthetic_bundle", "source_fingerprint": "synthetic-fingerprint",
            "members": [{"message_id": 1, "body_original": '本文 "合成  空白"',
                         "source_metadata": {"sender_name": "合成 職員", "organization": "合成 施設"}}]}


def _legacy_reader(tmp_path):
    assert hashlib.sha256(_LEGACY_FUNCTION.encode()).hexdigest() == _LEGACY_FUNCTION_SHA256
    path = tmp_path / "legacy_semantic_reader.py"
    path.write_text("from mcs_util import loads_dict\nKIND_ASSESS = 'semantic_assess'\n\n" + _LEGACY_FUNCTION)
    spec = importlib.util.spec_from_file_location("synthetic_legacy_semantic_reader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.current_artifact


def test_previous_semantic_reader_accepts_both_json_spellings(tmp_path):
    read_legacy = _legacy_reader(tmp_path)
    document = _document()
    meta = {"fingerprint": "synthetic-fingerprint", "policy_fingerprint": "synthetic-policy"}
    forms = (json.dumps(document, ensure_ascii=False),
             json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    assert forms[0] != forms[1]
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY, kind TEXT, message_id INTEGER, "
                   "content TEXT, meta TEXT)")
        for mid, raw in enumerate(forms, 1):
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)",
                       (mid, "semantic_bundle", mid, raw, json.dumps(meta)))
            found = read_legacy(db, "semantic_bundle", mid, "synthetic-fingerprint", "synthetic-policy")
            assert found == {"content": document, "meta": meta}
            assert read_legacy(db, "semantic_bundle", mid, "wrong", "synthetic-policy") is None
            assert read_legacy(db, "semantic_bundle", mid, "synthetic-fingerprint", "wrong") is None
        assert db.execute("SELECT artifact_id FROM artifacts ORDER BY artifact_id").fetchall()[0][0] == 1
    finally:
        db.close()


def test_compact_and_legacy_bundle_survive_authenticated_restore(world):  # noqa: F811
    snapshot, source, _, policy = world
    doc = _document()
    raw = (json.dumps(doc, ensure_ascii=False), json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    metas = [{VERSION_KEY: CURRENT_VERSION, "fingerprint": f"synthetic-fp-{i}",
              "policy_fingerprint": "synthetic-policy"} for i in (1, 2)]
    with closing(sqlite3.connect(snapshot)) as db:
        with db:
            for aid, text, meta in zip((501, 502), raw, metas, strict=True):
                db.execute("INSERT INTO artifacts(artifact_id,kind,project_id,message_id,content,model,meta,created_at) "
                           "VALUES(?,'semantic_bundle',1,1,?,'synthetic',?,0)", (aid, text, json.dumps(meta)))
    source_hash = mcs_backup.file_sha256(source)
    snapshot_hash = mcs_backup.file_sha256(snapshot)
    receipt = mcs_backup.offsite(str(snapshot), policy, lambda: KEY)
    target = Path(policy.scratch_dir) / "synthetic-restore"
    report = mcs_backup.restore(receipt["bundle"], receipt["sha256"], policy, lambda: KEY,
                                destination=str(target))
    assert report["restore_pending"] is True and notify_spool.restore_awaiting_consent(str(target))
    assert mcs_backup.file_sha256(source) == source_hash
    assert mcs_backup.file_sha256(target / "ledger.db") == snapshot_hash
    db = sqlite3.connect((target / "ledger.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='semantic_bundle' "
                          "ORDER BY artifact_id").fetchall()
        assert [r["artifact_id"] for r in rows] == [501, 502]
        assert [r["content"] for r in rows] == list(raw)
        assert [json.loads(r["content"]) for r in rows] == [doc, doc]
        for meta in metas:
            found = semantic_store.current_artifact(db, "semantic_bundle", 1,
                                                     meta["fingerprint"], "synthetic-policy")
            assert found == {"content": doc, "meta": meta}
        names = pattern_cards._generic_names(db, {"artifacts"})
        assert {"合成 職員", "合成 施設"} <= names
    finally:
        db.close()
