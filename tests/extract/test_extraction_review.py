"""Synthetic regressions for extraction review: state, leases and recovery."""
import json
import time

import pytest

import extract
import extract_bench
import extract_llm
from ledger import Ledger
from mcs_adapter import Message
import rollup


def _message(mid=1, body="合成本文", day=20):
    return Message(message_id=mid, project_id=1, parent_id=None,
                   sender_id=1, sender_name="SYNTH", sender_type="user",
                   profession="", organization="",
                   posted_at=f"2026-09-{day:02d}T00:00:00+09:00",
                   body_html=body, body_state="full", is_unread=False,
                   reply_count=0)


@pytest.fixture
def db(tmp_path):
    led = Ledger(str(tmp_path / "ledger.db"))
    led.ensure_patient(1)
    yield led
    led.close()


def artifact(db, mid, content):
    chash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=?",
                         (mid,)).fetchone()[0]
    db.artifact_add("extract_llm", json.dumps(content), project_id=1,
                    message_id=mid, meta={"hash": chash,
                                         "extract_version": extract_llm.EXTRACT_VERSION})


def test_family_mention_does_not_cancel_patient_medication(db):
    db.save_messages([_message(1, day=19), _message(2, day=20)])
    artifact(db, 1, {"meds": [{"name": "合成薬A", "subject": "patient",
                               "status": "current", "action": "start"}]})
    artifact(db, 2, {"meds": [{"name": "合成薬A", "subject": "family",
                               "status": "past", "action": "stop"}]})
    assert [m["name"] for m in rollup.build_rollup(db, 1)["medications"]] == ["合成薬A"]


def test_same_message_stop_supersedes_start_and_rule_candidate(db):
    db.save_messages([_message()])
    artifact(db, 1, {"meds": [
        {"name": "合成薬A", "subject": "patient", "status": "current", "action": "start"},
        {"name": "合成薬A", "subject": "patient", "status": "current", "action": "stop"}]})
    assert not rollup.build_rollup(db, 1).get("medications")


def test_family_symptom_does_not_become_patient_state(db):
    db.save_messages([_message()])
    artifact(db, 1, {"symptoms": [{"text": "合成症状", "subject": "family",
                                   "status": "ongoing", "negated": False}]})
    assert not rollup.build_rollup(db, 1).get("recent_symptoms")


def test_schema_allows_subject_and_raw_relative_deadline():
    props = extract_llm._SCHEMA["schema"]["properties"]
    assert "subject" in props["symptoms"]["items"]["properties"]
    assert "due_text" in props["requests"]["items"]["properties"]


def test_claimed_head_does_not_starve_other_pending_messages(db, monkeypatch):
    db.save_messages([_message(1, day=20), _message(2, day=19)])
    row = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._claim(db, row)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda *a, **kw: {"summary": "合成"})
    result = extract_llm.run_pending(db, limit=1)
    assert result["done"] == 1
    assert db.artifacts("extract_llm")[0]["message_id"] == 2


def test_changed_source_is_not_overwritten_by_inflight_extraction(db, monkeypatch):
    db.save_messages([_message(body="古い合成本文")])

    def infer(*args, **kwargs):
        db.save_messages([_message(body="変更後の合成本文")])
        artifact(db, 1, {"summary": "変更後の結果"})
        return {"summary": "古い結果"}

    monkeypatch.setattr(extract_llm, "llm_extract", infer)
    result = extract_llm.run_pending(db)
    assert result["done"] == 0
    assert [json.loads(a["content"])["summary"]
            for a in db.artifacts("extract_llm")] == ["変更後の結果"]


@pytest.mark.parametrize("workers", [1, 2])
def test_chunk_survives_later_exception_and_all_leases_release(db, monkeypatch, workers):
    db.save_messages([_message(1, body="あ" * 2500 + "\n" + "い" * 1200),
                      _message(2, body="あ" * 2500 + "\n" + "い" * 1200)])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")

    def infer(prompt, **kwargs):
        if "い" * 100 in prompt:
            raise RuntimeError("synthetic interruption")
        return {"summary": "完了チャンク"}

    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        extract_llm.run_pending(db, workers=workers)
    assert db.artifacts("extract_llm_chunk")
    assert not db.artifacts("extract_llm")
    assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0


def test_lease_covers_whole_batch_budget(db, monkeypatch):
    db.save_messages([_message()])

    def infer(*args, **kwargs):
        expiry = db.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]
        assert expiry - time.time() >= 3600
        return {"summary": "合成"}

    monkeypatch.setattr(extract_llm, "llm_extract", infer)
    assert extract_llm.run_pending(db, budget_s=3600)["done"] == 1


def test_missing_post_date_does_not_fabricate_regimen_year():
    out = extract.extract_message("内服薬 9/1-9/14", "不明")
    assert all(not p.get("start") and not p.get("end")
               for p in out.get("med_periods", []))


def test_failed_benchmark_counts_safety_attribute_misses():
    case = {"id": "synthetic", "expect": {"meds": [
        {"name": "合成薬A", "status": "past", "subject": "patient", "negated": False}]}}
    scored = extract_bench._score_case(case, None)
    assert scored["fields"]["med_status"]["fn"] == 1
    assert scored["fields"]["med_subject"]["fn"] == 1
    assert scored["fields"]["med_negated"]["fn"] == 1


@pytest.mark.parametrize(("body", "posted", "start", "end"), [
    ("内服薬12/28-1/10まで投与した", "2027-01-20", "2026-12-28", "2027-01-10"),
    ("内服薬2025/9/1-9/14", "2026-09-23", "2025-09-01", "2025-09-14"),
])
def test_regimen_keeps_explicit_year_and_recent_wrapped_period(body, posted, start, end):
    period = extract.extract_message(body, posted)["med_periods"][0]
    assert (period["start"], period["end"]) == (start, end)


def test_rule_change_reprocesses_unchanged_body(db):
    db.save_messages([_message(body="内服薬9/24-10/7の予定")])
    chash = db.db.execute("SELECT content_hash FROM messages").fetchone()[0]
    db.artifact_add("extract_v1", json.dumps({"med_periods": [
        {"start": "2025-09-24", "end": "2026-10-07"}]}),
        project_id=1, message_id=1, meta={"hash": chash})
    assert extract.run_pending(db)["done"] == 1
    period = json.loads(db.artifacts("extract_v1")[0]["content"])["med_periods"][0]
    assert period["start"] == "2026-09-24"
    assert extract.run_pending(db)["done"] == 0


def test_resident_idle_wait_honors_stop_and_reports_loaded_generation(monkeypatch, capsys):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(extract_llm.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(extract_llm.time, "sleep", clock.sleep)
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--stop-after", "2"])
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **kw: {
        "done": 0, "failed": 0, "left": 0, "selected": 0})
    assert extract_llm.main() == 0
    assert clock.now == 2
    record = json.loads(capsys.readouterr().err)
    assert record["extract_version"] == extract_llm.EXTRACT_VERSION
    assert record["source_digests"] == extract_llm._LOADED_SOURCE_DIGESTS


def test_chunk_checkpoint_requires_same_interpretation_context(db):
    db.save_messages([_message()])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    extract_llm._persist_chunks(db, row, {0: {"summary": "合成"}}, "元の文脈")
    assert extract_llm._saved_chunks(db, row, "元の文脈")
    assert not extract_llm._saved_chunks(db, row, "変更後の文脈")


def test_chunk_merge_preserves_restart_and_separate_subjects():
    def med(action, dose):
        return {"name": "合成薬", "subject": "patient", "action": action, "dose": dose}
    merged = extract_llm._merge([
        {"meds": [med("start", "1mg")],
         "symptoms": [{"text": "合成症状", "subject": "patient"}]},
        {"meds": [med("stop", None)]},
        {"meds": [med("start", "2mg")],
         "symptoms": [{"text": "合成症状", "subject": "family", "negated": True}]},
    ])
    assert merged["meds"][-1] == med("start", "2mg")
    assert {s["subject"] for s in merged["symptoms"]} == {"patient", "family"}


@pytest.mark.parametrize(("body", "posted", "expected"), [
    ('2025/2/29 訪問しました', '2025-03-01', {}),
    ('2/20 訪問しました。次回3/1', '不明', {}),
    ('2024/2/29 訪問しました', '不明', {'visit_date': '2024-02-29'}),
    ('12/31 訪問しました。次回1/3', '2027-01-01',
     {'visit_date': '2026-12-31', 'next_planned': '2027-01-03'}),
    ('12/28 訪問しました。次回1/3', '2026-12-30',
     {'visit_date': '2026-12-28', 'next_planned': '2027-01-03'}),
])
def test_visit_dates_require_a_valid_year_anchor(body, posted, expected):
    result = extract.extract_message(body, posted)
    assert {key: result[key] for key in ('visit_date', 'next_planned')
            if key in result} == expected


def test_date_rule_revision_replaces_fabricated_year(db):
    db.save_messages([_message(body='2025/2/29 訪問しました')])
    content_hash = db.db.execute('SELECT content_hash FROM messages').fetchone()[0]
    db.artifact_add('extract_v1', json.dumps({'visit_date': '2024-02-29'}),
                    project_id=1, message_id=1,
                    meta={'hash': content_hash,
                          'rule_version': extract.RULE_VERSION - 1})
    assert extract.run_pending(db)['done'] == 1
    assert 'visit_date' not in json.loads(db.artifacts('extract_v1')[0]['content'])
    assert extract.run_pending(db)['done'] == 0


@pytest.mark.parametrize(('body', 'high'), [
    ('急ぎではありません', False),
    ('緊急の対応は不要です', False),
    ('緊急性はありません', False),
    ('至急連絡する必要はありません', False),
    ('救急搬送はしません', False),
    ('先月、緊急搬送された', False),
    ('救急搬送しました', False),
    ('緊急対応は済みです', False),
    ('明日すぐに連絡します', False),
    ('来週、緊急受診の予定です', False),
    ('発熱について連絡します', False),
    ('至急ご確認ください', True),
    ('緊急搬送してください', True),
    ('すぐに連絡をお願いします', True),
    ('至急対応予定です', True),
    ('急ぎではありませんが至急ご確認ください', True),
    ('救急搬送しましたが現在すぐに連絡をお願いします', True),
    ('来週は緊急受診の予定。本日至急ご確認ください', True),
    ('先月緊急搬送されました。今は至急対応が必要です', True),
    ('緊急の対応は不要ではありません', True),
    ('昨日から38度の発熱、至急往診お願いします', True),
    ('至急ただちに受診してください', True),
    ('明日までに至急ご返信ください', True),
    ('緊急搬送されたので連絡します', False),
    ('搬送の必要はありません', False),
    ('救急要請なし', False),
    ('昨日の採血でK 6.5、至急ご連絡ください', True),
    ('先週処方した薬で発疹、すぐに中止してください', True),
    ('明日の訪問前に至急ご連絡ください', True),
    ('昨日より発熱、至急往診お願いします', True),
    ('明日、至急ご連絡ください', True),
    ('明日、緊急でお願いします', True),
    ('明日、至急受診してください', True),
    ('明日は至急往診をお願いします', True),
    ('今度は至急お願いします', True),
    ('緊急で搬送しました', False),
    ('すぐに搬送の必要はないが、経過観察', False),
    ('明日すぐに至急連絡します', False),
])
def test_urgency_requires_current_affirmative_evidence(body, high):
    assert (extract.extract_message(body, '2026-10-04').get('urgency')
            == 'high') is high


def test_urgency_revision_replaces_old_rule_and_shared_display(db):
    from structured_view import message_urgency

    db.save_messages([_message(body='急ぎではありません')])
    content_hash = db.db.execute('SELECT content_hash FROM messages').fetchone()[0]
    db.artifact_add('extract_v1', json.dumps({'urgency': 'high'}),
                    project_id=1, message_id=1,
                    meta={'hash': content_hash,
                          'rule_version': extract.RULE_VERSION - 1})
    assert message_urgency(db.db, 1) == 'rule'
    assert extract.run_pending(db)['done'] == 1
    assert message_urgency(db.db, 1) is None
    artifacts = db.artifacts('extract_v1')
    assert len(artifacts) == 1
    assert json.loads(artifacts[0]['meta'])['rule_version'] == extract.RULE_VERSION
    assert extract.run_pending(db)['done'] == 0


def test_rule_revision_cut_by_deadline_keeps_old_artifact(db, monkeypatch):
    db.save_messages([_message(1, body='至急ご確認ください', day=19),
                      _message(2, body='至急ご確認ください', day=20)])
    for mid, h in db.db.execute('SELECT message_id, content_hash FROM messages'):
        db.artifact_add('extract_v1', json.dumps({'urgency': 'high'}),
                        project_id=1, message_id=mid,
                        meta={'hash': h, 'rule_version': extract.RULE_VERSION - 1})
    clock = iter([0, 0, 10, 10, 10])
    monkeypatch.setattr(extract.time, 'monotonic', lambda: next(clock))
    assert extract.run_pending(db, deadline=5)['done'] == 1
    metas = sorted(json.loads(a['meta'])['rule_version']
                   for a in db.artifacts('extract_v1'))
    assert metas == [extract.RULE_VERSION - 1, extract.RULE_VERSION]
    monkeypatch.undo()
    assert extract.run_pending(db)['done'] == 1
    assert [json.loads(a['meta'])['rule_version']
            for a in db.artifacts('extract_v1')] == [extract.RULE_VERSION] * 2


@pytest.mark.parametrize('workers', [1, 2])
def test_ambiguous_batch_index_retries_only_its_message(db, monkeypatch, workers):
    db.save_messages([_message(1, body='合成本文A', day=19),
                      _message(2, body='合成本文B', day=20)])
    monkeypatch.setattr(extract_llm, '_llm_call', lambda *a, **kw: {
        'items': [{'i': 0, 'summary': '先の候補'},
                  {'i': 1, 'summary': '一意の候補'},
                  {'i': 0, 'summary': '矛盾する候補'},
                  {'i': 0, 'summary': '三つ目の候補'}]})
    calls = []
    monkeypatch.setattr(extract_llm, 'llm_extract',
                        lambda body, **kw: calls.append(body) or {'summary': '再抽出'})
    result = extract_llm.run_pending(db, budget_s=30, batch_k=2, workers=workers)
    assert result['done'] == 2 and result['failed'] == 0
    assert calls == ['合成本文B']
    summaries = {a['message_id']: json.loads(a['content'])['summary']
                 for a in db.artifacts('extract_llm')}
    assert summaries == {1: '一意の候補', 2: '再抽出'}


# U05-F02: oxygen flow and arrhythmia counts are not vitals.
@pytest.mark.parametrize(('body', 'key'), [
    ('酸素10L投与中。', 'spo2'),
    ('在宅酸素 3L→酸素 12Lへ増量', 'spo2'),
    ('酸素120L', 'spo2'),
    ('酸素 2リットルで経過観察', 'spo2'),
    ('不整脈は20回程度', 'hr'),
])
def test_v1_vitals_reject_flow_units_and_findings(body, key):
    vit = extract.extract_message(body, '2026-09-20T10:00:00+09:00') \
        .get('vitals') or {}
    assert key not in vit


@pytest.mark.parametrize(('body', 'expected'), [
    ('SpO2 96%', {'spo2': 96}),
    ('酸素 95%', {'spo2': 95}),
    ('脈拍72回/分', {'hr': 72}),
    ('脈 88', {'hr': 88}),
])
def test_v1_vitals_keep_labelled_readings(body, expected):
    assert extract.extract_message(
        body, '2026-09-20T10:00:00+09:00')['vitals'] == expected


def test_rollup_vitals_do_not_fall_back_to_v1_over_llm_row(db):
    db.save_messages([_message(body='合成本文')])
    h = db.db.execute('SELECT content_hash FROM messages').fetchone()[0]
    db.artifact_add('extract_v1', json.dumps({'v': 1,
                                              'vitals': {'spo2': 10}}),
                    project_id=1, message_id=1,
                    meta={'hash': h, 'rule_version': extract.RULE_VERSION})
    db.artifact_add('extract_llm', json.dumps({'summary': '合成要約'}),
                    project_id=1, message_id=1,
                    meta={'hash': h,
                          'extract_version': extract_llm.EXTRACT_VERSION})
    assert rollup.build_rollup(db, 1).get('latest_vitals') is None


# U05-F04: a planned visit is not a past visit_date.
@pytest.mark.parametrize(('body', 'expected'), [
    ('次回10/5訪問予定です', {'next_planned': '2026-10-05'}),
    ('明日9/21訪問予定', {}),
    ('9/25 訪問します', {}),
    ('2026/10/5 訪問の予定', {}),
    ('予定通り9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定どおり9/18訪問', {'visit_date': '2026-09-18'}),
    ('予定通りなら9/20訪問', {}),
    ('予定どおりなら9/20訪問', {}),
    ('次回9/20訪問', {'next_planned': '2026-09-20'}),
    ('9/20訪問予定', {}),
    ('予定の9/20訪問', {}),
    ('予定通りなら、9/20訪問', {}),
    ('予定どおりなら 9/20訪問', {}),
    ('予定通りなら　９/２０訪問', {}),
    ('予定通り、9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定どおり、9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定通りに9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定通り　９/１８訪問しました', {'visit_date': '2026-09-18'}),
    ('予定通りなら行く。9/18訪問しました', {'visit_date': '2026-09-18'}),
    # an unrelated planned cue 7+ chars before the date is not the date's
    ('明日連絡します 9/18訪問済', {'visit_date': '2026-09-18'}),
    ('明日電話します、9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('今度の件は了解、9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定を変更して9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定変更となり9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定が合わず、9/18訪問しました', {'visit_date': '2026-09-18'}),
    ('予定を早めて、9/18訪問', {'visit_date': '2026-09-18'}),
    ('明後日も伺います9/18訪問', {'visit_date': '2026-09-18'}),
    ('今度こそ確認、9/18訪問', {'visit_date': '2026-09-18'}),
    ('来週まで様子見、9/18訪問', {'visit_date': '2026-09-18'}),
    # next_planned here is HEAD's unchanged (pre-existing) resolution
    ('次回持参物確認し9/18訪問',
     {'visit_date': '2026-09-18', 'next_planned': '2027-09-18'}),
    ('次回10/5訪問予定。9/18訪問しました',
     {'visit_date': '2026-09-18', 'next_planned': '2026-10-05'}),
])
def test_planned_visit_is_not_a_past_visit_date(body, expected):
    result = extract.extract_message(body, '2026-09-20T10:00:00+09:00')
    assert {key: result[key] for key in ('visit_date', 'next_planned')
            if key in result} == expected


def test_rule_pass_commits_in_chunks_and_resumes(db, monkeypatch):
    monkeypatch.setattr(extract, "_CHUNK", 3)
    db.save_messages([_message(mid=i, body=f"合成本文{i}") for i in range(1, 8)])
    real, calls = extract.extract_message, []

    def fail_on_fifth(body, posted):
        calls.append(body)
        if len(calls) == 5:
            raise RuntimeError("synthetic crash")
        return real(body, posted)

    monkeypatch.setattr(extract, "extract_message", fail_on_fifth)
    with pytest.raises(RuntimeError):
        extract.run_pending(db)
    assert len(db.artifacts("extract_v1")) == 3     # first chunk kept
    monkeypatch.setattr(extract, "extract_message", real)
    out = extract.run_pending(db)
    assert out["done"] == 4 and out["pids"] == [1]
    assert len(db.artifacts("extract_v1")) == 7
