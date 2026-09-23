"""A model support score cannot approve an unverified quantity conversion."""
import json
import time

import pytest

import semantic
from test_mcs_semantic import _FakeJev, _cfg, _llm, _seeded


@pytest.mark.parametrize('replacement', ['300g', '300μg', '300mg/mL', '500mg', '300mg/kg', '300mg/5mL', '-300mg', '1,300mg', '300mg/日'])
def test_summary_quantity_change_cannot_pass_on_model_support(tmp_path, replacement):
    db = _seeded(tmp_path)
    def llm(prompt):
        response = _llm(prompt)
        if response and '要約器' in prompt:
            return response.replace('300mg', replacement)
        return response
    try:
        semantic.run_due(db, _cfg('shadow'), {'errors': []},
                         time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=llm)
        audits = db.artifacts('semantic_audit')
        assert audits
        assert all(json.loads(row['meta'])['audit_status'] == 'NEEDS_REVIEW'
                   for row in audits)
        assert not db.db.execute(
            "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'").fetchone()
    finally:
        db.close()


@pytest.mark.parametrize('omit_quantity', [False, True])
def test_repaired_quantity_preserves_normal_summary_path(tmp_path, omit_quantity):
    db = _seeded(tmp_path)
    summaries = 0
    def llm(prompt):
        nonlocal summaries
        response = _llm(prompt)
        if response and '要約器' in prompt:
            summaries += 1
            if summaries == 1:
                return response.replace('300mg', '300g')
            if omit_quantity:
                value = json.loads(response)
                for claim in value['claims']:
                    claim['text'] = claim['text'].replace('300mg×3回', '新しい用量')
                return json.dumps(value)
        return response
    try:
        semantic.run_due(db, _cfg('shadow'), {'errors': []},
                         time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=llm)
        audits = [json.loads(row['meta']) for row in db.artifacts('semantic_audit')]
        expected = 'NEEDS_REVIEW' if omit_quantity else 'PASS'
        assert audits and all(row['audit_status'] == expected for row in audits)
        if not omit_quantity:
            assert sum(row['repair_count'] for row in audits) == 1
    finally:
        db.close()
