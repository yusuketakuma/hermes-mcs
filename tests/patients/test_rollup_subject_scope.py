"""旧抽出と混在原文の主体を完全合成データで集約時に検証する。"""
import clinical_scope
import json
import re

import pytest

import clinical_values
import extract
import extract_llm
import extract_validation
import inference_dependency_loss
from extract_testkit import _message
from common_testkit import make_ledger
import rollup


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **_kwargs: True)
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *args, **kwargs: None)
    ledger = make_ledger(tmp_path)
    ledger.ensure_patient(1)
    ledger.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                      "WHERE project_id=1")
    ledger.db.commit()
    yield ledger
    ledger.close()


def seed(store, body, doc, *, mid=1, posted_at="2026-09-19T00:00:00+09:00"):
    store.save_messages([_message(mid=mid, body=body, posted_at=posted_at)])
    row = dict(store.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, mid)
    row["_target"] = ("", extract_llm.resolved_model({}))
    integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                 "completeness_chunks": 1,
                 "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(store, row, json.dumps(doc, ensure_ascii=False), integrity=integrity)


def test_mislabeled_family_values_never_become_patient_current(store):
    seed(store, "母の状態：\n母の2026-09-19測定で体温38.5℃。母の2026-09-19採血でCr 2.1 mg/dL。"
                "母は合成薬Aを開始し服用しています。母は発熱があります。", {
        "vitals": {"bt": 38.5},
        "labs": [{"name": "Cr", "value": "2.1", "unit": "mg/dL", "subject": "patient",
                  "measured_on": "2026-09-19", "evidence": "母の2026-09-19採血でCr 2.1 mg/dL。"}],
        "meds": [{"name": "合成薬A", "action": "start", "subject": "patient",
                  "status": "current", "evidence": "母は合成薬Aを開始し服用しています。"}],
        "symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
                      "evidence": "母は発熱があります。"}]})
    out = rollup.build_rollup(store.db, 1, cfg={})
    for key in ("latest_vitals", "recent_labs", "medications", "recent_symptoms"):
        assert not out.get(key), key


def test_mixed_source_keeps_patient_readings_medication_and_symptom(store):
    body = ("母の体温38.5℃。母のCr 2.1 mg/dL。母は合成薬Aを開始。母は発熱あり。"
            "本人は2026-09-19測定で体温36.5℃。本人は2026-09-19採血でCr 0.9 mg/dL。"
            "本人は合成薬Bを開始し服用しています。本人は咳があります。")
    doc = {"vitals": {"bt": 36.5}, "labs": [
        {"name": "Cr", "value": "2.1", "subject": "family", "evidence": "Cr 2.1 mg/dL"},
        {"name": "Cr", "value": "0.9", "unit": "mg/dL", "measured_on": "2026-09-19",
         "subject": "patient", "evidence": "本人は2026-09-19採血でCr 0.9 mg/dL。"}],
        "meds": [{"name": f"合成薬{c}", "action": "start", "subject": "family" if c == "A" else "patient",
                  "status": "current", "negated": False,
                  "evidence": ("合成薬Aを開始" if c == "A" else "本人は合成薬Bを開始し服用しています。")}
                 for c in "AB"],
        "symptoms": [{"text": "発熱", "subject": "family", "status": "ongoing", "negated": False,
                      "evidence": "発熱あり"},
                     {"text": "咳", "subject": "patient", "status": "ongoing", "negated": False,
                      "evidence": "本人は咳があります。"}]}
    validated = extract_validation.validate(doc, body)
    assert validated is not None and not validated.get("_items_dropped") and not validated.get("_evidence_dropped")
    seed(store, body, doc)
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["measurement_coverage"]["complete"] is True
    assert out["latest_vitals"]["bt"] == 36.5
    assert [x["value"] for x in out["recent_labs"]] == ["0.9"]
    assert [x["name"] for x in out["medications"]] == ["合成薬B"]
    assert [x["symptom"] for x in out["recent_symptoms"]] == ["咳"]


def test_family_topic_keeps_later_bare_patient_lab_unconfirmed(store):
    body = ("母の状態：\n体温38.5℃。Cr 2.1 mg/dL。合成薬Aを開始。発熱あり。\n本人の状態：\n"
            "本人の2026-09-19測定で体温36.5℃。本人の2026-09-19採血でCr 0.9 mg/dL。"
            "本人は合成薬Bを開始し服用しています。本人は咳があります。")
    seed(store, body, {"labs": [{"name": "Cr", "value": "0.9", "unit": "mg/dL",
                                "measured_on": "2026-09-19", "subject": "patient",
                                "evidence": "本人の2026-09-19採血でCr 0.9 mg/dL。"}]})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["measurement_coverage"]["complete"] is True
    assert out["measurement_coverage"]["unverified_lab_reports"] == 1
    assert not out.get("recent_labs")


def test_cached_clinical_fields_are_rechecked_without_erasing_profile(store):
    seed(store, "母の状態：\n体温38.5℃。合成薬Aを開始。", {"vitals": {"bt": 38.5},
        "meds": [{"name": "合成薬A", "subject": "patient", "status": "current",
                  "evidence": "合成薬Aを開始"}]})
    store.db.execute("UPDATE patients SET karte_id=100 WHERE project_id=1")
    store.db.commit()
    store.karte_summary_store(1, 100, {"comment": "生活環境：正本の合成プロフィール。"})
    cached = {"latest_vitals": {"at": "old", "bt": 38.5},
              "medications": [{"name": "合成薬A", "last": "old"}],
              "patient_context": {"memo": {"living": ["古い合成プロフィール"]}}}
    store.db.execute("PRAGMA query_only=ON")
    before = store.db.total_changes
    out = rollup.current_cached_refs(store.db, 1, cached, {}, cfg={})
    assert not out.get("latest_vitals") and not out.get("medications")
    assert out["patient_context"]["memo"]["living"][0]["text"] == "正本の合成プロフィール。"
    assert "古い合成プロフィール" not in json.dumps(out, ensure_ascii=False)
    assert store.db.total_changes == before


def test_full_sentence_evidence_inherits_subject_at_item_surface(store):
    seed(store, "母は合成薬Aを開始し発熱あり。本人は合成薬Bを開始し服用しています。本人は咳があります。", {
        "meds": [{"name": "合成薬A", "subject": "patient", "status": "current",
                  "evidence": "母は合成薬Aを開始し発熱あり。"},
                 {"name": "合成薬B", "subject": "patient", "status": "current",
                  "evidence": "本人は合成薬Bを開始し服用しています。"}],
        "symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
                      "evidence": "母は合成薬Aを開始し発熱あり。"},
                     {"text": "咳", "subject": "patient", "status": "ongoing",
                      "evidence": "本人は咳があります。"}]})
    out = rollup.build_rollup(store.db, 1)
    assert [x["name"] for x in out["medications"]] == ["合成薬B"]
    assert [x["symptom"] for x in out["recent_symptoms"]] == ["咳"]


def test_older_unscoped_trusted_output_contract_is_preserved(store):
    seed(store, "合成本文。本人の2026-09-19測定で体温36.5℃。本人は合成薬Aを服用しています。",
         {"vitals": {"bt": 36.5},
                            "meds": [{"name": "合成薬A", "subject": "patient", "status": "current"}]})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["latest_vitals"]["bt"] == 36.5
    assert out["medications"][0]["name"] == "合成薬A"


def test_recent_family_stop_does_not_suppress_older_patient_medication(store):
    seed(store, "本人は合成薬Aを開始し服用しています。", {"meds": [{"name": "合成薬A", "subject": "patient",
        "status": "current", "action": "start", "evidence": "本人は合成薬Aを開始し服用しています。"}]})
    seed(store, "母の状態：\n以前、合成薬Aを中止。", {"meds": [{"name": "合成薬A", "subject": "patient",
        "status": "past", "action": "stop", "evidence": "以前、合成薬Aを中止。"}]},
        mid=2, posted_at="2026-09-20T00:00:00+09:00")
    assert rollup.build_rollup(store.db, 1)["medications"][0]["name"] == "合成薬A"


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
    counter = _CountingPattern(clinical_scope._SCOPE_HEADING)
    monkeypatch.setattr(clinical_scope, '_SCOPE_HEADING', counter)
    yield counter
    clear()


@pytest.mark.parametrize("provider_fields", [False, True])
def test_per_span_scope_output_is_pinned(provider_fields):
    ends = [m.start() for m in re.finditer(r"[。！？!?;；、,\n]|$", SCOPE_BODY) if m.start() > 0]
    for name, expected in ((None, "fffppppouuuu"), ("山田", "fffppppoppcc")):
        got = "".join(clinical_scope.patient_source_scope(SCOPE_BODY, end - 1, end, patient_name=name,
                                                   provider_fields=provider_fields)[0] for end in ends)
        assert got == expected
    assert clinical_values.patient_item_scope({}, SCOPE_BODY) == "unknown"
    assert clinical_values.patient_item_scope({}, "体温36.5℃。咳あり。") == "patient"
    assert clinical_values.patient_item_scope({}, "") == "unknown"
    # A break word straddling the span start and an owner before a trailing newline.
    body = "以前は便秘けれども発熱あり。"
    assert clinical_scope.patient_source_scope(body, body.index("も"), body.index("も") + 3) == "patient"
    body = "山田さんの\n体温38.5℃。"
    assert clinical_scope.patient_source_scope(body, 6, 10) == "unknown"
    assert clinical_scope.patient_source_scope(body, 6, 10, patient_name="山田") == "patient"


def test_unlocated_item_scans_a_long_body_once(heading_scans):
    body = "体温36.5℃、咳あり。" * 100
    assert clinical_values.patient_item_scope({}, body) == "patient"
    assert heading_scans.scans == 1  # was one full-body scan per clause end
    extract.extract_message(body, "2026-09-01T09:00:00")
    assert heading_scans.scans <= 3  # at most one more per provider_fields variant


def test_scope_prefix_scans_stay_within_the_sentence(heading_scans, monkeypatch):
    owner, breaks = _WindowPattern(clinical_scope._DIRECT_OWNER), _WindowPattern(clinical_scope._SCOPE_BREAK)
    monkeypatch.setattr(clinical_scope, '_DIRECT_OWNER', owner)
    monkeypatch.setattr(clinical_scope, '_SCOPE_BREAK', breaks)
    body = "体温36.5℃、咳あり。" * 200
    assert clinical_values.patient_item_scope({}, body) == "patient"
    assert owner.windows and breaks.windows
    # Conditional phrases can cross a comma, but scans must not cross sentences.
    assert max(owner.windows + breaks.windows) <= len("体温36.5℃、咳あり。")
    assert sum(owner.windows + breaks.windows) <= 6 * len(body)
    conditional = "もし本人に発熱があれば、本人は受診します。"
    start = conditional.index("受診")
    assert clinical_scope.source_axes(conditional, start, start + len("受診"))[1] == "conditional"


def test_unlocated_item_stops_at_first_non_patient_clause(monkeypatch):
    original, calls = clinical_scope.patient_source_scope, []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(clinical_scope, 'patient_source_scope', counted)
    assert clinical_values.patient_item_scope({}, "母の状態：\n" + "体温36.5℃。" * 50) == "unknown"
    assert calls == [1]


def test_current_cache_with_mixed_scope_source_is_revalidated_on_read(store, monkeypatch):
    seed(store, "母の状態：\n体温38.5℃。\n本人の状態：\n"
                "本人の2026-09-19測定で体温36.5℃。本人は合成薬Bを開始し服用しています。", {
        "vitals": {"bt": 36.5},
        "meds": [{"name": "合成薬B", "action": "start", "subject": "patient", "status": "current",
                  "evidence": "本人は合成薬Bを開始し服用しています。"}]})
    rollup.rebuild(store, 1, cfg={})
    row = store.db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_rollup' "
                           "AND project_id=1").fetchone()
    cached, meta = json.loads(row["content"]), row["meta"]
    assert json.loads(meta)["period_check_version"] == rollup.PERIOD_CHECK_VERSION
    assert rollup.dirty_projects(store) == []
    assert cached["latest_vitals"]["bt"] == 36.5
    original, calls = rollup.build_rollup, []

    def counted(*args, **kwargs):
        calls.append(kwargs.get("cfg"))
        return original(*args, **kwargs)

    monkeypatch.setattr(rollup, "build_rollup", counted)
    before = store.db.total_changes
    poisoned = {**cached, "latest_vitals": {"bt": 99}, "medications": [{"name": "古い合成薬"}]}
    got = rollup.current_cached_refs(store.db, 1, poisoned, meta, cfg={})
    assert calls == [{}]
    assert got["latest_vitals"]["bt"] == 36.5
    assert [item["name"] for item in got["medications"]] == ["合成薬B"]
    assert "古い合成薬" not in json.dumps(got, ensure_ascii=False)
    for old_meta in ({}, {"period_check_version": rollup.PERIOD_CHECK_VERSION - 1}):
        refreshed = rollup.current_cached_refs(store.db, 1, poisoned, old_meta, cfg={})
        assert refreshed["latest_vitals"]["bt"] == 36.5
        assert [item["name"] for item in refreshed["medications"]] == ["合成薬B"]
        assert "古い合成薬" not in json.dumps(refreshed, ensure_ascii=False)
    assert calls == [{}, {}, {}]
    unknown = rollup.current_cached_refs(store.db, 1, poisoned, meta)
    assert calls == [{}, {}, {}] and unknown["rollup_freshness"] == "unknown"
    assert not unknown.get("latest_vitals") and not unknown.get("medications")
    assert store.db.total_changes == before


class _InPlacePattern:
    """Record whether each match call reads the body in place rather than a copied tail."""

    def __init__(self, pattern, body):
        self.pattern, self.body, self.in_place = pattern, body, []

    def match(self, string, *args):
        self.in_place.append(string is self.body)
        return self.pattern.match(string, *args)


def test_person_tail_checks_read_the_body_in_place(heading_scans, monkeypatch):
    body = "息子さんに対し看護師が合成薬Aを処方。娘さんより報告あり、" * 100 + "血圧120/80"
    reporting = _InPlacePattern(clinical_scope._REPORTING, body)
    actor = _InPlacePattern(clinical_scope._CLINICAL_ACTOR, body)
    monkeypatch.setattr(clinical_scope, '_REPORTING', reporting)
    monkeypatch.setattr(clinical_scope, '_CLINICAL_ACTOR', actor)
    end = len(body)
    clinical_scope.patient_source_scope(body, end - 10, end)
    assert reporting.in_place and actor.in_place
    # was body[match.start() + len(person):] per person mention, O(persons * body)
    assert all(reporting.in_place + actor.in_place)
