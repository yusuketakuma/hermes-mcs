"""Synthetic completeness quality testkit contracts with isolated shared fixtures."""
import extract_llm_context
import extract_llm_engine
import extract_llm_prepare
import extract_llm_proofs
import extract_llm_runner
import extract_llm_store
import extract_quality
import copy
import json
import extract_llm
from extract_testkit import _ledger, _message


OBSERVATIONS = {"symptoms": [
    {"text": "発熱", "status": "ongoing", "subject": "patient", "evidence": "発熱"},
    {"text": "咳", "status": "ongoing", "subject": "patient", "evidence": "咳"}]}


PREFIX = "本人に発熱と咳があります。"


def hints(body):
    return extract_llm._rule_hints({"body_text": body, "posted_at": "2032-06-01"})


def _legacy_unsealed_multileaf(tmp_path, monkeypatch, *, with_vitals):
    """Commit a real multi-leaf result, then drop only the embedded claim
    field — modelling a final artifact written before the leaf-claim seal
    existed, with its checkpoints already cleaned."""
    db = _ledger(tmp_path)
    pieces = [f"本人に合成{tag}の発熱と咳があります。本人の血圧{sbp}/{dbp}です。\n"
              for tag, sbp, dbp in (("A", 120, 80), ("B", 125, 85), ("C", 130, 90))]
    db.save_messages([_message(body="".join(pieces))])
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", len(pieces[0]))
    monkeypatch.setattr(extract_llm_context, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_engine, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_prepare, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_proofs, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_store, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)

    def model(prompt, **kwargs):
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        index = "ABC".index(next(tag for tag in "ABC" if f"合成{tag}" in piece))
        return {"symptoms": [{"text": text, "evidence": piece} for text in ("発熱", "咳")],
                **({"vitals": {"sbp": 120 + 5 * index, "dbp": 80 + 5 * index}}
                   if with_vitals else {})}

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    for _ in range(4):
        if extract_llm.run_pending(db, limit=1, budget_s=200)["done"]:
            break
    [final] = db.artifacts(extract_llm.KIND, message_id=1)
    content = json.loads(final["content"])
    content.pop(extract_llm._INTERNAL_LEAF_CLAIMS)
    legacy_content = json.dumps(content, ensure_ascii=False)
    meta = json.loads(final["meta"])
    # Rebind the stored provider evidence to the unsealed bytes: a genuine
    # pre-seal artifact's result_hash was computed over this exact content.
    meta["provider_provenance"]["result_hash"] = extract_llm._provider_result_hash(legacy_content)
    with db.db:
        db.db.execute("UPDATE artifacts SET content=?, meta=? WHERE artifact_id=?",
                      (legacy_content, json.dumps(meta, ensure_ascii=False),
                       final["artifact_id"]))
    assert not db.artifacts("extract_llm_chunk", message_id=1)
    final = db.artifacts(extract_llm.KIND, message_id=1)[0]
    return db, final


CONFIRM = {"to": None, "kind": "request", "action": "記録確認",
           "evidence": "記録確認をお願いします"}


CONTACT = {"to": None, "kind": "request", "action": "主治医へ連絡",
           "evidence": "主治医へ連絡をお願いします"}


BOTH = "本人の記録の確認と主治医への連絡をお願いします"


BOTH_REVIEW = "記録確認、主治医へ連絡をお願いします"


SECOND_CONFIRM = {"to": None, "kind": "request", "action": "検査値確認",
                  "evidence": "検査値確認をお願いします"}


LATER_QUESTION = {"to": "不明", "kind": "question", "action": "体温の確認",
                  "evidence": "本人の体温を教えてください"}


LATER_CONTACT = {"to": "主治医", "kind": "request", "action": "主治医へ連絡",
                 "evidence": "主治医へ連絡してください"}


REVIEW_QUESTION = {"to": None, "kind": "question", "action": "体温を教えて",
                   "evidence": "体温を教えてください"}


REVIEW_CONTACT = {"to": None, "kind": "request", "action": "主治医へ連絡",
                  "evidence": "主治医へ連絡してください"}


TWO_CONFIRMS = "本人の記録の確認と検査値の確認をお願いします"


RECORD = {"to": "不明", "kind": "request", "action": "記録確認", "evidence": TWO_CONFIRMS}


LAB = {"to": "不明", "kind": "request", "action": "検査値確認", "evidence": TWO_CONFIRMS}


REVIEW_TWO_CONFIRMS = "記録確認、検査値確認をお願いします"


REVIEW_RECORD = {"to": None, "kind": "request", "action": "記録確認", "evidence": REVIEW_TWO_CONFIRMS}


REVIEW_LAB = {"to": None, "kind": "request", "action": "検査値確認", "evidence": REVIEW_TWO_CONFIRMS}


def _confirm(action, quote):
    return {"to": "不明", "kind": "request", "action": action, "evidence": quote}


HEEL = "本人のかかとの確認と検査値の確認をお願いします"


NURSING = "本人の記録の確認と看護記録の確認をお願いします"


THREE = "本人の記録の確認と検査値の確認と体温の確認をお願いします"


THREE_ITEMS = [_confirm("記録確認", THREE), _confirm("検査値確認", THREE), _confirm("体温確認", THREE)]


BP_BS = "血圧の記録の確認と血糖の記録の確認をお願いします"


def _run(monkeypatch, body, first, repaired=None):
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        requests = first if len(calls) == 1 or repaired is None else repaired
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    return calls, extract_llm.llm_extract(body, hints=hints(body))


VT = "本人の体温の確認と血圧の記録の確認と血糖の記録の確認をお願いします"


VT_ALL = [_confirm("体温確認", VT), _confirm("血圧記録確認", VT), _confirm("血糖記録確認", VT)]


CROSS = "本人の記録の確認と投薬を終えたので、主治医へ連絡をお願いします"


RIGHT_NEG = "本人の記録を確認してください、転倒はありません"


LEFT_DONE = "記録は確認済みですが、主治医へ連絡してください"


CROSS_CONTACT = {"to": "主治医", "kind": "request", "action": "主治医へ連絡",
                 "evidence": "主治医へ連絡をお願いします"}


LEFT_CONTACT = {"to": "主治医", "kind": "request", "action": "主治医へ連絡",
                "evidence": "主治医へ連絡してください"}


STOP = "本人の架空薬Aを中止しました"


STOPPED = {"name": "架空薬A", "dose": None, "action": "stop", "status": "past",
           "subject": "patient", "negated": False, "evidence": STOP}


NAMED = {**STOPPED, "action": "none", "status": "current"}


TWO_STARTS = "本人は架空薬A5mgを開始し、架空薬B10mgも開始しました"


A_START = {**STOPPED, "dose": "5mg", "action": "start", "status": "current", "evidence": "架空薬A5mgを開始し"}


B_START = {**A_START, "name": "架空薬B", "dose": "10mg", "evidence": "架空薬B10mgも開始しました"}


def _final_meta(db):
    row = db.db.execute("SELECT artifact_id, meta FROM artifacts WHERE kind=? "
                        "ORDER BY artifact_id DESC LIMIT 1", (extract_llm.KIND,)).fetchone()
    return row["artifact_id"], json.loads(row["meta"])


def _rx_gap(text, meds):
    # Legacy output can reach this guard without the new validator's scope rejection.
    return extract_quality._rx_action_gaps({"meds": meds}, hints(text), text)


def _med(name, text, action="start", status="current", dose=None):
    return {"name": name, "dose": dose, "action": action, "status": status,
            "subject": "patient", "negated": False, "evidence": text}


def _repair(monkeypatch, text, first, repaired):
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: calls.append(args) or copy.deepcopy(
        {**OBSERVATIONS, "meds": first if len(calls) == 1 else repaired}))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    return calls, extract_llm.llm_extract(PREFIX + text, hints=hints(PREFIX + text))


AB = "本人の架空薬Aを中止し、架空薬Bを開始しました。"
