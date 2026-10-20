"""Cached reconciliation must equal the original exhaustive synthetic oracle."""
import copy
import random
from itertools import chain, combinations, product

import pytest

import semantic_facts as sf
import semantic_relations as sr


def original_classify(left: dict, right: dict, token_cache=None) -> dict | None:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    lf_id, rf_id = left.get("fact_id"), right.get("fact_id")
    if not lf_id or not rf_id or lf_id == rf_id:
        return None
    rel_type = None
    reason = None
    if sr._norm(left.get("statement")) == sr._norm(right.get("statement")) \
            and all(left.get(field) == right.get(field) for field in (
                "kind", "subject", "actor", "polarity", "epistemic",
                "workflow_status", "event_time", "valid_time", "quantity",
                "action")):
        rel_type, reason = "EXACT_DUPLICATE", "same_statement"
    else:
        shared = sr._entity_overlap(left, right, token_cache)
        if not shared:
            return None
        same_subject = left.get("subject") == right.get("subject")
        same_kind = left.get("kind") == right.get("kind")
        la = left.get("action", "unknown")
        ra = right.get("action", "unknown")
        action_pair = same_kind and (
            (la, ra) in sr._ACTION_SUPERSESSION
            or (ra, la) in sr._ACTION_SUPERSESSION)
        polarity_conflict = (
            {left.get("polarity"), right.get("polarity")}
            == {"affirmed", "negated"})
        if same_subject and action_pair:
            if sr._ordered_before(left, right):
                rel_type = "EXPLICIT_SUPERSESSION"
                reason = f"action_{la}_then_{ra}"
            elif sr._ordered_before(right, left):
                # Older fact wins the left slot deterministically.
                return sr._relation(right, left, "EXPLICIT_SUPERSESSION",
                                 f"action_{ra}_then_{la}")
            else:
                rel_type, reason = "CONTRADICTION", "unordered_action_pair"
        elif same_subject and polarity_conflict:
            if sr._ordered_before(left, right):
                rel_type, reason = "EXPLICIT_SUPERSESSION", "polarity_flip"
            elif sr._ordered_before(right, left):
                return sr._relation(right, left, "EXPLICIT_SUPERSESSION",
                                 "polarity_flip")
            else:
                rel_type, reason = "CONTRADICTION", "polarity_conflict"
        elif same_subject and same_kind \
                and left.get("kind") != "other_observation":
            lw = left.get("workflow_status")
            rw = right.get("workflow_status")
            if lw in sr.WORKFLOW_ORDER and rw in sr.WORKFLOW_ORDER \
                    and sr.WORKFLOW_ORDER.index(lw) < sr.WORKFLOW_ORDER.index(rw):
                rel_type, reason = "TRANSITION", "workflow_progression"
            elif sr._norm(left.get("statement")) != sr._norm(right.get("statement")):
                rel_type, reason = "COMPLEMENTS", "shared_entity"
            else:
                rel_type, reason = "UNRESOLVED", "clinical_attributes_differ"
        elif not same_subject:
            rel_type, reason = "COMPLEMENTS", "shared_entity_other_subject"
        else:
            rel_type, reason = "UNRESOLVED", "shared_entity_unclassified"
    return sr._relation(left, right, rel_type, reason)


def exhaustive(active, new):
    """Independent original pair enumeration, never candidate-index selection."""
    active = [f for f in (active or []) if isinstance(f, dict)]
    new = [f for f in (new or []) if isinstance(f, dict)]
    relations = {}
    for left, right in chain(product(active, new), combinations(new, 2)):
        relation = original_classify(left, right)
        if relation is not None:
            relations[relation['relation_id']] = relation
    out = sorted(relations.values(), key=lambda r: r['relation_id'])
    return dict(relations=out, fingerprint=sr.relation_set_fingerprint(out),
                counts={t: sum(r['type'] == t for r in out)
                        for t in sf.RELATION_TYPES},
                active_preserved=len(active), new_preserved=len(new),
                pairs_skipped=0)


def fact(index, statement, **kw):
    return dict(fact_id=f'fact_{index}', statement=statement,
                kind='medication_event', subject='patient:synthetic',
                actor='staff:synthetic', polarity='affirmed',
                epistemic='asserted', workflow_status='planned',
                event_time='unknown', valid_time='unknown',
                quantity='unknown', action='start', evidence_ids=[], **kw)


def sparse(n, prefix='entity'):
    return [fact(i, f'{prefix}{i:05d}') for i in range(n)]


@pytest.mark.parametrize('shape', ['sparse', 'dense', 'mixed', 'generic', 'no_token'])
def test_cache_matches_exhaustive_and_preserves_inputs(shape):
    active, new = sparse(32, 'active'), sparse(100)
    if shape == 'dense':
        new = [fact(i, f'共通薬剤 Drug{i:05d}') for i in range(100)]
    elif shape == 'mixed':
        new = [fact(i, f'Drug{i % 7:02d} {i:05d}') for i in range(100)]
    elif shape == 'generic':
        new = [fact(i, '投与') for i in range(100)]
    elif shape == 'no_token':
        new = [fact(i, '!') for i in range(100)]
    active += copy.deepcopy(new[:4])  # repeated IDs/overlapping active/new
    before = copy.deepcopy((active, new))
    assert sr.reconcile_facts(active, new) == exhaustive(active, new)
    assert (active, new) == before


def test_clinical_adversaries_keep_subject_negation_time_and_collision_order():
    new = sparse(100)
    variants = [
        {'subject': 'family:synthetic'}, {'kind': 'care_event'},
        {'polarity': 'negated'}, {'actor': 'staff:other'},
        {'epistemic': 'suspected'}, {'quantity': '10mg'},
        {'workflow_status': 'performed'}, {'action': 'stop'},
        {'event_time': '2026-09-01T01:00:00.100Z'},
        {'event_time': '2026-09-01'},
        {'valid_time': '2026-09-01T10:00:00+09:00'},
    ]
    new[:len(variants)] = [{**fact(i, '合成薬剤 SyntheticDrug'), **v}
                           for i, v in enumerate(variants)]
    # Equal normalized text with no useful entity, plus repeated fact IDs.
    new[20:25] = [fact(20, x) for x in ['!', '!', '投与', '投与', 'ＳＡＭＥ']]
    new[25] = fact(25, 'same')
    new += [None, 'bad', {}]
    active = [{**new[0], 'evidence_ids': ['ev_left']},
              {**new[0], 'evidence_ids': ['ev_later']}]
    assert sr.reconcile_facts(active, new) == exhaustive(active, new)


@pytest.mark.parametrize('seed', range(8))
def test_scenario_series_transformations_match_oracle(seed):
    rng = random.Random(seed)
    rows = sparse(100)
    for row in rows:
        row.update(statement=rng.choice(['DrugA開始', 'DrugA中止', '頭痛あり',
                                         '頭痛なし', '!', '投与', row['statement']]),
                   subject=rng.choice(['patient:1', 'family:1', 'unknown']),
                   polarity=rng.choice(['affirmed', 'negated']),
                   action=rng.choice(['start', 'stop', 'hold', 'restart']),
                   event_time=rng.choice(['unknown', '2026-09-01', '2026-09-02']))
    for transformed in (rows, list(reversed(rows)),
                        [{**r, 'fact_id': 'renamed_' + r['fact_id']} for r in rows]):
        assert sr.reconcile_facts([], transformed) == exhaustive([], transformed)


def test_unvalidated_nontext_without_id_keeps_original_fallback():
    rows = sparse(100) + [{'statement': ['invalid without id']}]
    assert sr.reconcile_facts([], rows) == exhaustive([], rows)


def test_normalization_is_reused_only_within_one_reconciliation(monkeypatch):
    rows = sparse(100)
    real = sf._normalize_name
    calls = []

    def normalize(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(sf, '_normalize_name', normalize)
    first = sr.reconcile_facts([], rows)
    # One entity token and one full statement normalization per unique text.
    assert len(calls) == 2 * len(rows)
    calls.clear()
    assert sr.reconcile_facts([], rows) == first
    assert len(calls) == 2 * len(rows)
    calls.clear()
    assert exhaustive([], rows) == first
    assert len(calls) > len(rows) * 50
