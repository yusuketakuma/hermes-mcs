"""旧抽出と混在原文の主体を完全合成データで集約時に検証する。"""
import json
import re

import pytest

import clinical_values
import extract
from extract_testkit import _ledger, _message, _hash
import rollup


@pytest.fixture
def store(tmp_path):
    led = _ledger(tmp_path)
    led.ensure_patient(1)
    yield led
    led.close()


def seed(store, body, doc):
    store.save_messages([_message(body=body)])
    store.artifact_add("extract_llm", json.dumps(doc), project_id=1, message_id=1,
                       meta={"hash": _hash(store)})


def test_mislabeled_family_values_never_become_patient_current(store):
    seed(store, "母の状態：\n体温38.5℃。Cr 2.1 mg/dL。合成薬Aを開始。発熱あり。", {
        "vitals": {"bt": 38.5},
        "labs": [{"name": "Cr", "value": "2.1", "unit": "mg/dL", "subject": "patient",
                  "evidence": "Cr 2.1 mg/dL"}],
        "meds": [{"name": "合成薬A", "action": "start", "subject": "patient",
                  "status": "current", "evidence": "合成薬Aを開始"}],
        "symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
                      "evidence": "発熱あり"}]})
    out = rollup.build_rollup(store, 1)
    for key in ("latest_vitals", "recent_labs", "medications", "recent_symptoms"):
        assert not out.get(key), key


def test_mixed_source_keeps_patient_readings_medication_and_symptom(store):
    body = "母の状態：\n体温38.5℃。Cr 2.1 mg/dL。合成薬Aを開始。発熱あり。\n本人の状態：\n体温36.5℃。Cr 0.9 mg/dL。合成薬Bを開始。咳あり。"
    seed(store, body, {"vitals": {"bt": 36.5}, "labs": [
        {"name": "Cr", "value": "2.1", "subject": "patient", "evidence": "Cr 2.1 mg/dL"},
        {"name": "Cr", "value": "0.9", "subject": "patient", "evidence": "Cr 0.9 mg/dL"}],
        "meds": [{"name": f"合成薬{c}", "action": "start", "subject": "patient", "status": "current",
                  "evidence": f"合成薬{c}を開始"} for c in "AB"],
        "symptoms": [{"text": s, "subject": "patient", "status": "ongoing", "evidence": s + "あり"}
                     for s in ("発熱", "咳")]})
    out = rollup.build_rollup(store, 1)
    assert out["latest_vitals"]["bt"] == 36.5
    assert [x["value"] for x in out["recent_labs"]] == ["0.9"]
    assert [x["name"] for x in out["medications"]] == ["合成薬B"]
    assert [x["symptom"] for x in out["recent_symptoms"]] == ["咳"]


def test_cached_clinical_fields_are_rechecked_without_erasing_profile(store):
    seed(store, "母の状態：\n体温38.5℃。合成薬Aを開始。", {"vitals": {"bt": 38.5},
        "meds": [{"name": "合成薬A", "subject": "patient", "status": "current",
                  "evidence": "合成薬Aを開始"}]})
    cached = {"latest_vitals": {"at": "old", "bt": 38.5},
              "medications": [{"name": "合成薬A", "last": "old"}],
              "patient_context": {"memo": {"living": ["合成プロフィール"]}}}
    store.db.execute("PRAGMA query_only=ON")
    out = rollup.current_cached_refs(store.db, 1, cached, {})
    assert not out.get("latest_vitals") and not out.get("medications")
    assert out["patient_context"] == cached["patient_context"]


def test_full_sentence_evidence_inherits_subject_at_item_surface(store):
    seed(store, "母は合成薬Aを開始し発熱あり。本人は合成薬Bを開始し咳あり。", {
        "meds": [{"name": "合成薬A", "subject": "patient", "status": "current",
                  "evidence": "母は合成薬Aを開始し発熱あり。"},
                 {"name": "合成薬B", "subject": "patient", "status": "current",
                  "evidence": "本人は合成薬Bを開始し咳あり。"}],
        "symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
                      "evidence": "母は合成薬Aを開始し発熱あり。"},
                     {"text": "咳", "subject": "patient", "status": "ongoing",
                      "evidence": "本人は合成薬Bを開始し咳あり。"}]})
    out = rollup.build_rollup(store, 1)
    assert [x["name"] for x in out["medications"]] == ["合成薬B"]
    assert [x["symptom"] for x in out["recent_symptoms"]] == ["咳"]


def test_older_unscoped_trusted_output_contract_is_preserved(store):
    seed(store, "合成本文", {"vitals": {"bt": 36.5},
                            "meds": [{"name": "合成薬A", "subject": "patient", "status": "current"}]})
    out = rollup.build_rollup(store, 1)
    assert out["latest_vitals"]["bt"] == 36.5
    assert out["medications"][0]["name"] == "合成薬A"


def test_recent_family_stop_does_not_suppress_older_patient_medication(store):
    seed(store, "本人は合成薬Aを開始。", {"meds": [{"name": "合成薬A", "subject": "patient",
        "status": "current", "action": "start", "evidence": "本人は合成薬Aを開始。"}]})
    store.save_messages([_message(mid=2, body="母の状態：\n以前、合成薬Aを中止。",
                                   posted_at="2026-09-20T00:00:00+09:00")])
    store.artifact_add("extract_llm", json.dumps({"meds": [{"name": "合成薬A", "subject": "patient",
        "status": "past", "action": "stop", "evidence": "以前、合成薬Aを中止。"}]}),
        project_id=1, message_id=2, meta={"hash": _hash(store, 2)})
    assert rollup.build_rollup(store, 1)["medications"][0]["name"] == "合成薬A"


# Same scope output without repeated whole-body scans or per-read rebuilds.
SCOPE_BODY = ("母の状態：\n体温38.5℃。\n本人の状態：\n体温36.5℃、血圧120/80。以前は便秘あり。"
        "看護師が訪問しました。山田さんは咳あり。予定：来週受診。もし発熱なら連絡。")


class _CountingPattern:
    def __init__(self, pattern):
        self.pattern, self.scans = pattern, 0

    def finditer(self, *args, **kwargs):
        self.scans += 1
        return self.pattern.finditer(*args, **kwargs)


class _WindowPattern:
    """Record how many characters each search/finditer call may scan."""

    def __init__(self, pattern):
        self.pattern, self.windows = pattern, []

    def _record(self, string, pos=0, endpos=None, match=None):
        stop = match.end() if match else min(len(string), len(string) if endpos is None else endpos)
        self.windows.append(stop - pos)

    def search(self, *args):
        match = self.pattern.search(*args)  # stops at its first match
        self._record(*args, match=match)
        return match

    def finditer(self, *args):
        self._record(*args)
        return self.pattern.finditer(*args)


@pytest.fixture
def heading_scans(monkeypatch):
    def clear():
        for name in ("_scope_events", "_scope_puncts"):
            getattr(getattr(extract, name, None), "cache_clear", lambda: None)()

    clear()
    counter = _CountingPattern(extract._SCOPE_HEADING)
    monkeypatch.setattr(extract, "_SCOPE_HEADING", counter)
    yield counter
    clear()


@pytest.mark.parametrize("provider_fields", [False, True])
def test_per_span_scope_output_is_pinned(provider_fields):
    ends = [m.start() for m in re.finditer(r"[。！？!?;；、,\n]|$", SCOPE_BODY) if m.start() > 0]
    for name, expected in ((None, "fffppppouuuu"), ("山田", "fffppppoppcc")):
        got = "".join(extract.patient_source_scope(SCOPE_BODY, end - 1, end, patient_name=name,
                                                   provider_fields=provider_fields)[0] for end in ends)
        assert got == expected
    assert clinical_values.patient_item_scope({}, SCOPE_BODY) == "unknown"
    assert clinical_values.patient_item_scope({}, "体温36.5℃。咳あり。") == "patient"
    assert clinical_values.patient_item_scope({}, "") == "unknown"
    # A break word straddling the span start and an owner before a trailing newline.
    body = "以前は便秘けれども発熱あり。"
    assert extract.patient_source_scope(body, body.index("も"), body.index("も") + 3) == "patient"
    body = "山田さんの\n体温38.5℃。"
    assert extract.patient_source_scope(body, 6, 10) == "unknown"
    assert extract.patient_source_scope(body, 6, 10, patient_name="山田") == "patient"


def test_unlocated_item_scans_a_long_body_once(heading_scans):
    body = "体温36.5℃、咳あり。" * 100
    assert clinical_values.patient_item_scope({}, body) == "patient"
    assert heading_scans.scans == 1  # was one full-body scan per clause end
    extract.extract_message(body, "2026-09-01T09:00:00")
    assert heading_scans.scans <= 3  # at most one more per provider_fields variant


def test_scope_prefix_scans_stay_within_the_clause(heading_scans, monkeypatch):
    owner, breaks = _WindowPattern(extract._DIRECT_OWNER), _WindowPattern(extract._SCOPE_BREAK)
    monkeypatch.setattr(extract, "_DIRECT_OWNER", owner)
    monkeypatch.setattr(extract, "_SCOPE_BREAK", breaks)
    body = "体温36.5℃、咳あり。" * 200
    assert clinical_values.patient_item_scope({}, body) == "patient"
    assert owner.windows and breaks.windows
    # was the whole prefix body[:start] per clause end, i.e. quadratic in the body length
    assert max(owner.windows + breaks.windows) <= len("体温36.5℃、")


def test_unlocated_item_stops_at_first_non_patient_clause(monkeypatch):
    original, calls = extract.patient_source_scope, []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(extract, "patient_source_scope", counted)
    assert clinical_values.patient_item_scope({}, "母の状態：\n" + "体温36.5℃。" * 50) == "unknown"
    assert calls == [1]


def test_current_cache_with_mixed_scope_source_is_not_rebuilt_on_read(store, monkeypatch):
    store.save_messages([_message(body="母の状態：\n体温38.5℃。\n本人の状態：\n体温36.5℃。合成薬Bを開始。")])
    store.artifact_add("extract_llm", json.dumps({
        "vitals": {"bt": 36.5},
        "meds": [{"name": "合成薬B", "action": "start", "subject": "patient", "status": "current",
                  "evidence": "合成薬Bを開始"}]}), project_id=1, message_id=1, meta={"hash": _hash(store)})
    rollup.rebuild(store, 1)
    row = store.db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_rollup' "
                           "AND project_id=1").fetchone()
    cached, meta = json.loads(row["content"]), row["meta"]
    assert json.loads(meta)["period_check_version"] == rollup.PERIOD_CHECK_VERSION
    assert rollup.dirty_projects(store) == []
    assert cached["latest_vitals"]["bt"] == 36.5
    # The legacy (unstamped) path still recomputes; its output is the reference.
    original, calls = rollup.build_rollup, []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(rollup, "build_rollup", counted)
    expected = rollup.current_cached_refs(store.db, 1, cached, {})
    assert calls == [1]
    stale = rollup.current_cached_refs(store.db, 1, cached, {"period_check_version": rollup.PERIOD_CHECK_VERSION - 1})
    assert calls == [1, 1]
    monkeypatch.setattr(rollup, "build_rollup", lambda *_a, **_k: pytest.fail("current cache rebuilt"))
    got = rollup.current_cached_refs(store.db, 1, cached, meta)
    assert json.dumps(got, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    assert stale == expected


class _InPlacePattern:
    """Record whether each match call reads the body in place rather than a copied tail."""

    def __init__(self, pattern, body):
        self.pattern, self.body, self.in_place = pattern, body, []

    def match(self, string, *args):
        self.in_place.append(string is self.body)
        return self.pattern.match(string, *args)


def test_person_tail_checks_read_the_body_in_place(heading_scans, monkeypatch):
    body = "息子さんに対し看護師が合成薬Aを処方。娘さんより報告あり、" * 100 + "血圧120/80"
    reporting = _InPlacePattern(extract._REPORTING, body)
    actor = _InPlacePattern(extract._CLINICAL_ACTOR, body)
    monkeypatch.setattr(extract, "_REPORTING", reporting)
    monkeypatch.setattr(extract, "_CLINICAL_ACTOR", actor)
    end = len(body)
    extract.patient_source_scope(body, end - 10, end)
    assert reporting.in_place and actor.in_place
    # was body[match.start() + len(person):] per person mention, O(persons * body)
    assert all(reporting.in_place + actor.in_place)
