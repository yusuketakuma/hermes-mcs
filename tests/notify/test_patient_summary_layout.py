"""🧾 患者まとめ: what needs action (open tasks) comes before the
extracted material, and an overdue task is marked."""
from notify_testkit import _add_request, _seed_thread, led  # noqa: F401
from notify_views import patient_summary_text
import json

import inference_version as inference
import rollup

__all__ = ["led"]


def test_open_tasks_come_first_and_overdue_is_marked(led):
    _seed_thread(led)
    _add_request(led, title="合成の確認", due="2000-01-01")
    _add_request(led, title="合成の予定", due="2999-01-01")
    _, text = patient_summary_text(led.db, 1)
    lines = text.split("\n")
    tasks = lines.index("■ 未完了タスク")
    assert tasks < next(i for i, ln in enumerate(lines) if ln.startswith("■ 背景"))
    assert "合成の確認 — 期限 2000-01-01 ⚠期限切れ" in text
    assert "合成の予定 — 期限 2999-01-01" in text
    assert "2999-01-01 ⚠" not in text


def test_old_inference_summary_does_not_turn_unparsed_medications_into_absence(led):
    _seed_thread(led)
    led.artifact_add(rollup.KIND, json.dumps({"medications": [{"name": "旧版の架空薬"}]}),
                     project_id=1, meta={inference.VERSION_KEY: inference.LEGACY_VERSION})
    _, text = patient_summary_text(led.db, 1)
    assert "現在使用中の薬剤: 未確認" in text
    assert "旧版の架空薬" not in text and "抽出された薬なし" not in text


def test_planned_and_unknown_drugs_are_visible_without_claiming_current_use(led, monkeypatch):
    _seed_thread(led)
    led.artifact_add(rollup.KIND, "{}", project_id=1)
    monkeypatch.setattr("notify_views.current_cached_refs", lambda *args: {
        "planned_medications": [{"name": "架空の予定薬", "dose": "10mg"}],
        "unverified_medications": [{"name": "架空の未確認薬", "dose": "5mg"}]})
    _, text = patient_summary_text(led.db, 1)
    assert "現在使用中の薬剤: 未確認" in text
    assert "予定: 架空の予定薬 10mg" in text
    assert "確認待ち: 架空の未確認薬 5mg" in text
    assert "抽出された薬なし" not in text


def test_source_coverage_gap_never_presents_medication_reports_as_a_complete_current_state(led, monkeypatch):
    _seed_thread(led)
    led.artifact_add(rollup.KIND, "{}", project_id=1)
    monkeypatch.setattr("notify_views.current_cached_refs", lambda *args: {
        "medications": [{"name": "架空の報告薬", "dose": "5mg"}],
        "clinical_state": {"dependencies": {"complete": False, "gaps": 3}}})
    _, text = patient_summary_text(led.db, 1)
    assert "薬（現在の状態は未確定）" in text and "変更前の根拠が未確認: 3件" in text
    assert "架空の報告薬 5mg" in text
