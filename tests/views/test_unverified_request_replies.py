"""The patient reply/previsit/handoff views keep unverified requests distinct."""
import pytest

import patient_context_view as pcv
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of

DAY = 86400
T0 = 1_790_000_000


@pytest.fixture
def store(tmp_path):
    db = _db(tmp_path, ())
    db.save_messages([_message(1, body="架空薬の残数確認をお願いします。")], project_id=1)
    db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=1", (T0,))
    db.db.commit()
    yield db
    db.close()


EVIDENCE = {"project_id": 1, "items": [], "as_of": None, "coverage": {}}


def test_unverified_request_is_not_listed_as_a_confirmed_unanswered_request(store):
    # The extraction marks the request unverified (e.g. negated/ungrounded);
    # structured_view shows it only as 依頼候補（未確認）. The patient views drop the flag.
    _artifact(store, "extract_llm", 1, {"requests": [{"to": "pharmacy", "action": "架空薬の残数確認",
                                                      "unverified": True}]},
              {"hash": _hash_of(store, 1)})
    item = pcv.replies(store.db, 1)["items"][0]
    unanswered = pcv.handoff(store.db, EVIDENCE)["unanswered"]
    assert item.get("unverified") is True or 1 not in [r["message_id"] for r in unanswered], item
    assert item["unverified"] is True and unanswered == []
    assert pcv.previsit(store.db, EVIDENCE, T0 + DAY)["unanswered"] == []
