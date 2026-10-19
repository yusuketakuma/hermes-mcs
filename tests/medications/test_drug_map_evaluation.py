"""Frozen, fully fictional #11 corpus: general/brand/ambiguous/unknown P/R and forbid=0."""

import hashlib
import json

import drug_map

_SOURCE = {"name": "fictional evaluation dictionary", "url": "https://example.invalid/eval",
           "terms_checked_on": "2026-10-04", "approved_by": "fictional-eval"}
INGREDIENTS = {
    "schema": drug_map.SCHEMA, "dict_id": "fictional-eval-v1", "source": _SOURCE,
    "entries": [
        {"id": "f-mira", "kind": "ingredient", "display": "ミラプロフェン",
         "aliases": ["ミラノン", "ミラトール", "共通名ソラ"], "codes": {}, "forms": []},
        {"id": "f-miro", "kind": "ingredient", "display": "ミロプロフェン",
         "aliases": ["ミロノン", "共通名ソラ"], "codes": {}, "forms": []},
        {"id": "f-class", "kind": "class", "display": "架空鎮痛群",
         "aliases": ["架空鎮痛薬"], "codes": {}, "forms": []},
    ],
}
OFFICIAL = {
    "schema": drug_map.SOURCE_SCHEMA, "dict_id": "fictional-eval-v2", "source": _SOURCE,
    "entries": [
        {"id": "mhlw:medicine:900000101", "kind": "product", "display": "架空製品ハナ錠5mg",
         "aliases": [], "codes": {"medicine": "900000101"}, "forms": [],
         "source_codes": [{"medicine": "900000101", "drug_price": None,
                           "general_name": None}]},
        {"id": "mhlw:general:GEN000000101", "kind": "general_name",
         "display": "【般】架空ハナ5mg錠", "aliases": [],
         "codes": {"general_name": "GEN000000101"}, "forms": [],
         "source_codes": [{"medicine": "900000102", "drug_price": None,
                           "general_name": "GEN000000101"}]},
    ],
}
MIRA, MIRO = {"f-mira"}, {"f-miro"}
# (dictionary, category, surface, expected status, expected codes, forbidden codes)
CORPUS = [
    ("i", "general", "ミラプロフェン", "resolved", MIRA, MIRO),
    ("i", "general", "みらぷろふぇん", "resolved", MIRA, MIRO),
    ("i", "general", "ミラプロフェン錠60mg", "resolved", MIRA, MIRO),
    ("i", "general", "ミロプロフェン", "resolved", MIRO, MIRA),
    ("o", "general", "【般】架空ハナ5mg錠", "resolved", {"mhlw:general:GEN000000101"},
     {"mhlw:medicine:900000101"}),
    ("i", "brand", "ミラノン", "resolved", MIRA, MIRO),
    ("i", "brand", "ﾐﾗﾉﾝ錠60mg", "resolved", MIRA, MIRO),
    ("i", "brand", "ミラトールOD錠5mg「架空社」", "resolved", MIRA, MIRO),
    ("i", "brand", "ミロノン", "resolved", MIRO, MIRA),
    ("o", "brand", "架空製品ハナ錠5mg", "resolved", {"mhlw:medicine:900000101"},
     {"mhlw:general:GEN000000101"}),
    ("i", "ambiguous", "共通名ソラ", "ambiguous", MIRA | MIRO, set()),
    ("i", "ambiguous", "架空鎮痛薬", "generic", {"f-class"}, MIRA | MIRO),
    ("i", "ambiguous", "処方薬", "generic", set(), MIRA | MIRO | {"f-class"}),
    ("i", "unknown", "ミラノンン", "unresolved", set(), MIRA | MIRO),
    ("i", "unknown", "ミラ", "unresolved", set(), MIRA | MIRO),
    ("i", "unknown", "ミラノン架空", "unresolved", set(), MIRA | MIRO),
    ("i", "unknown", "未登録架空薬", "unresolved", set(), MIRA | MIRO),
    ("o", "unknown", "架空製品ハナ錠10mg", "unresolved", set(), {"mhlw:medicine:900000101"}),
    ("o", "unknown", "架空ハナ", "unresolved", set(),
     {"mhlw:medicine:900000101", "mhlw:general:GEN000000101"}),
]


def _load(tmp_path, document):
    raw = json.dumps(document, ensure_ascii=False).encode()
    path = tmp_path / f"{document['dict_id']}.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return drug_map.load(path, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_frozen_corpus_precision_recall_and_no_unsupported_merge(tmp_path):
    maps = {"i": _load(tmp_path, INGREDIENTS), "o": _load(tmp_path, OFFICIAL)}
    score, forbidden, mismatches = {}, [], []
    for key, category, surface, status, expected, forbid in CORPUS:
        ref = maps[key].resolve(surface)
        got = {c["code"] for c in ref["cands"]}
        assert all(c["candidate"] is True for c in ref["cands"])
        if ref["status"] != status or got != expected:
            mismatches.append((surface, ref["status"], sorted(got)))
        forbidden += [(surface, code) for code in got & forbid]
        tp, fp, fn = score.get(category, (0, 0, 0))
        score[category] = (tp + len(got & expected), fp + len(got - expected),
                           fn + len(expected - got))
    assert mismatches == []
    assert forbidden == []  # forbid=0: no merge into an identity without dictionary evidence
    # Fixed denominators (tp, fp, fn): precision and recall are 1.0 on this frozen corpus.
    assert score == {"general": (5, 0, 0), "brand": (5, 0, 0),
                     "ambiguous": (3, 0, 0), "unknown": (0, 0, 0)}
