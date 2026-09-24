"""Structured display preserves typed exclusions and source attribution."""
import json

import pytest

import ledger
import structured_view


@pytest.fixture
def db(tmp_path):
    store = ledger.Ledger(str(tmp_path / "ledger.db"))
    with store.db:
        store.db.execute(
            "INSERT INTO messages(message_id,project_id,body_state,content_hash) "
            "VALUES(1,1,'full','synthetic-hash')")
    yield store
    store.close()


def _render(db, llm, rules=None):
    for kind, content in (("extract_llm", llm), ("extract_v1", rules or {})):
        db.artifact_add(kind, json.dumps(content), project_id=1, message_id=1,
                        meta={"hash": "synthetic-hash"})
    return "\n".join(structured_view.structured_lines(db.db, 1))


def test_filters_and_labels(db):
    joined = _render(db, {
        "meds": [{"name": "家族薬", "subject": "family"},
                 {"name": "予定薬", "status": "planned"},
                 {"name": "現行薬", "action": "none"}],
        "events": ["transfer", "fall", "family_contact"],
        "requests": [{"to": "医師", "from": "家族",
                      "action": "状態確認", "due": "2026-10-01"}],
    })
    assert "家族薬" not in joined
    assert "予定薬[予定]" in joined
    assert "転院/移動" in joined and "転倒" in joined and "家族連絡" in joined
    assert "家族→医師へ状態確認(期限:2026-10-01)" in joined


def test_resolved_cancels_rule_positive(db):
    joined = _render(db, {"symptoms": [{"text": "発熱", "status": "resolved"}]},
                     {"symptoms": ["発熱"]})
    assert "発熱" not in joined


def test_rule_fallback_skipped_when_llm_excluded_medication(db):
    joined = _render(db, {"meds": [{"name": "家族薬", "subject": "family"}]},
                     {"medications": [{"name": "家族薬"}]})
    assert "家族薬" not in joined
