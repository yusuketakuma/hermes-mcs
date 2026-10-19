"""Pharmacy aggregates preserve source, parent, drug grouping and historical wording."""
import copy
import json

import pytest

import patient_context
import rollup
from extract_testkit import _hash, _ledger, _message


def context(quote, *, subject="patient", names=("合成薬A",), values=("3錠",), key="residual_quantity"):
    return {"category": "medication_management", "text": quote, "evidence": quote,
            "subject": subject, "details": [
                *({"key": "drug_name", "value": name, "evidence": quote} for name in names),
                *({"key": key, "value": value, "evidence": quote} for value in values)]}


def save(led, mid, body, items, *, pid=1):
    led.save_messages([_message(mid=mid, project_id=pid, body=body)])
    return led.artifact_add("extract_llm", json.dumps({"patient_context": items}), project_id=pid,
                            message_id=mid, meta={"hash": _hash(led, mid)})


def test_multiple_drugs_subjects_values_and_parents_are_never_collapsed(tmp_path):
    led = _ledger(tmp_path)
    quotes = ["本人の合成薬Aは残薬3錠と別袋8錠。", "家族の合成薬Bは残薬3錠。", "知人の合成薬Cは残薬3錠。"]
    parents = [context(quotes[0], values=("3錠", "8錠")),
               context(quotes[1], subject="family", names=("合成薬B",)),
               context(quotes[2], subject="other", names=("合成薬C",))]
    try:
        save(led, 1, "\n".join(quotes), parents)
        row = led.db.execute("SELECT * FROM messages").fetchone()
        original = copy.deepcopy(parents)
        reports = patient_context.pharmacy_state_items({"patient_context": parents}, row)
        assert parents == original
        assert [(r["drug_names"], r["subject"], r["value"]) for r in reports] == [
            (["合成薬A"], "patient", "3錠"), (["合成薬A"], "patient", "8錠"),
            (["合成薬B"], "family", "3錠"), (["合成薬C"], "other", "3錠")]
        assert len({r["fingerprint"] for r in reports}) == 4
        assert reports[0]["parent_fingerprint"] == reports[1]["parent_fingerprint"]
        assert len({r["parent_fingerprint"] for r in reports}) == 3
        for report in reports:
            assert report["message_id"] == report["project_id"] == 1
            assert report["content_hash"] == _hash(led) and report["posted_at_ts"] == row["posted_at_ts"]
            assert report["source"] == "chat" and len(report["fingerprint"]) == 64
    finally:
        led.close()


def test_multi_drug_parent_is_one_group_not_a_value_distributed_to_each_drug(tmp_path):
    led = _ledger(tmp_path)
    quote = "合成薬Aと合成薬Bを合わせた残薬は3錠。"
    try:
        save(led, 1, quote, [context(quote, names=("合成薬A", "合成薬B"))])
        reports = rollup.build_rollup(led.db, 1)["pharmacy_state"]["items"]
        assert len(reports) == 1 and reports[0]["drug_names"] == ["合成薬A", "合成薬B"]
        assert reports[0]["value"] == "3錠"
    finally:
        led.close()


@pytest.mark.parametrize("wording", ["以前は", "今後は", "予定はなく", "未確認だが"])
def test_aggregation_keeps_history_plans_negation_and_uncertainty_as_reported(tmp_path, wording):
    led = _ledger(tmp_path)
    quote = f"{wording}合成薬Aの残薬3錠と記載。"
    try:
        save(led, 1, quote, [context(quote)])
        report = rollup.build_rollup(led.db, 1)["pharmacy_state"]["items"][0]
        assert report["parent"]["text"] == report["parent"]["evidence"] == quote
        assert "current" not in report and "status" not in report
    finally:
        led.close()


def test_latest_order_cap_total_and_earlier_artifacts_remain_intact(tmp_path):
    led = _ledger(tmp_path)
    try:
        for mid in range(1, 106):
            quote = f"合成報告{mid}、合成薬Aの残薬3錠。"
            save(led, mid, quote, [context(quote)])
        before = led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm'").fetchone()[0]
        result = rollup.build_rollup(led.db, 1)["pharmacy_state"]
        assert (result["total"], len(result["items"]), result["truncated"]) == (105, 100, True)
        assert [r["message_id"] for r in result["items"]] == list(range(105, 5, -1))
        assert led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm'").fetchone()[0] == before
    finally:
        led.close()


@pytest.mark.parametrize("change", ["edit", "delete", "other_project"])
def test_stale_deleted_or_foreign_sources_do_not_supply_reports(tmp_path, change):
    led = _ledger(tmp_path)
    quote = "合成薬Aの残薬3錠。"
    try:
        save(led, 1, quote, [context(quote)], pid=2 if change == "other_project" else 1)
        if change == "edit":
            led.save_messages([_message(body="合成薬Aの残薬は未確認。")])
        elif change == "delete":
            with led.db:
                led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
        result = rollup.build_rollup(led.db, 1)["pharmacy_state"]
        assert result == {"items": [], "total": 0, "truncated": False, "source": "chat"}
        assert led.artifacts("extract_llm")  # aggregation never deletes source history
    finally:
        led.close()


def test_old_results_and_unfetched_context_do_not_invent_pharmacy_values(tmp_path):
    led = _ledger(tmp_path)
    try:
        save(led, 1, "合成の経過です。", [])
        result = rollup.build_rollup(led.db, 1)
        assert result["patient_context"] == {"memo": {}, "chat": {}}
        assert result["pharmacy_state"]["items"] == []
        assert rollup.PERIOD_CHECK_VERSION == 11
        rollup.rebuild(led, 1)
        rollup.rebuild(led, 1)
        artifacts = led.artifacts("patient_rollup", project_id=1)
        assert len(artifacts) == 1
        assert json.loads(artifacts[0]["meta"])["period_check_version"] == 11
    finally:
        led.close()


@pytest.mark.parametrize("key,value", [
    ("inquiry_result", "変更なし"), ("storage_status", "冷蔵保管"), ("storage_place", "冷蔵庫"),
    ("medication_manager", "家族"), ("interaction_check", "確認済み"),
    ("support_method", "一包化"), ("residual_quantity", "3錠"),
    ("missed_doses", "2回"), ("followup", "次回確認"),
])
def test_each_requested_pharmacy_attribute_reaches_the_report(tmp_path, key, value):
    led = _ledger(tmp_path)
    quote = f"合成薬Aについて{value}と記載。"
    try:
        save(led, 1, quote, [context(quote, key=key, values=(value,))])
        reports = rollup.build_rollup(led.db, 1)["pharmacy_state"]["items"]
        assert len(reports) == 1 and reports[0]["key"] == key and reports[0]["value"] == value
    finally:
        led.close()


def test_identical_values_on_distinct_posts_keep_both_source_bindings(tmp_path):
    led = _ledger(tmp_path)
    quote = "合成薬Aの残薬3錠。"
    try:
        for mid in (1, 2):
            save(led, mid, quote, [context(quote)])
        reports = rollup.build_rollup(led.db, 1)["pharmacy_state"]["items"]
        assert len(reports) == 2 and [r["message_id"] for r in reports] == [2, 1]
        assert reports[0]["value"] == reports[1]["value"] == "3錠"
    finally:
        led.close()


@pytest.mark.parametrize("relation", ["CONTRADICTION", "UNRESOLVED"])
def test_disputed_canonical_medications_stay_reported_and_never_become_current(tmp_path, relation):
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy
    from semantic_testkit import EV, v2_doc, v2_fact
    led = _ledger(tmp_path)
    drugs = ["アムロジピン", "カロナール"]
    body = "本人について、" + "、".join(f"{drug}を継続" for drug in drugs) + "と記載。"
    facts, evidence = [], []
    for index, drug in enumerate(drugs):
        eid, fid = f"ev_{index}", f"f{index}"
        start = body.index(drug)
        evidence.append({**EV, "evidence_id": eid, "start": start, "end": start + len(drug), "quote": drug})
        facts.append(v2_fact(fid, statement=drug + "継続", action="continue", evidence_ids=[eid]))
    document = v2_doc(facts, evidence, relations=[{
        "left_fact_id": "f0", "right_fact_id": "f1", "type": relation}])
    try:
        led.save_messages([_message(body=body)])
        projection = project_v2_doc_legacy(document)
        assert len(projection["meds"]) == 2
        led.artifact_add("canonical_projection", json.dumps(projection), project_id=1, message_id=1,
                         meta={"hash": _hash(led), "projection_version": PROJECTION_VERSION})
        out = rollup.build_rollup(led.db, 1)
        assert not out.get("medications")
        assert {m["name"] for m in out["unverified_medications"]} == set(drugs)
        assert {f["fact_id"] for f in out["canonical_facts"]} == {"f0", "f1"}
    finally:
        led.close()
