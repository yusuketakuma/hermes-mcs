"""Shared pieces every chat transport builds on (synthetic data only)."""
from adapters.common.context_view import context_entries, safe_url

import card_worker_status
import health_watch

VIEW = {"layout": "patient_context", "title": "患者の全体像", "head": ["関係者: 看護師"],
        "items": [{"group": "主病名", "project_id": 1, "quotes": ["疾患A"], "date": "10/01",
                   "source_rest": "看護師 #1", "url": "https://example.invalid/projects/1"},
                  {"group": "服薬", "project_id": 1, "quotes": ["残薬"], "source": "10/03 · 投稿#3",
                   "date": "10/03", "url": "javascript:alert(1)"},
                  {"group": "服薬", "project_id": 2, "quotes": ["範囲外"], "source": "x"}],
        "notes": ["表示は暫定"], "empty": "該当なし"}


def test_context_entries_are_ordered_scoped_and_link_only_safe_urls():
    entries = context_entries(VIEW, lambda pid: pid == 1)
    assert [e["kind"] for e in entries] == [
        "title", "head", "group", "quotes", "source", "divider", "group", "quotes", "source",
        "divider", "notes"]
    sources = [e for e in entries if e["kind"] == "source"]
    assert sources[0]["url"] == "https://example.invalid/projects/1" and sources[0]["rest"] == "看護師 #1"
    assert sources[1]["url"] is None and sources[1]["rest"] == "10/03 · 投稿#3"
    assert "範囲外" not in str(entries)
    assert context_entries({**VIEW, "layout": "list"}, lambda pid: True) is None
    empty = context_entries({**VIEW, "items": []}, lambda pid: True)
    assert {"kind": "empty", "text": "該当なし"} in empty and {"kind": "divider"} not in empty
    assert not safe_url("https://a.invalid/x y") and not safe_url("http://a.invalid")


def test_lineworks_adapter_start_is_diagnosed_like_other_workers(tmp_path):
    cfg = {"notify": {"interactive": "lineworks", "lineworks": {}}}
    assert card_worker_status.diagnose(tmp_path, cfg)["reason"] == "worker_start_unverified"
    card_worker_status.record(tmp_path, "lineworks", "default", "worker_started")
    assert card_worker_status.diagnose(tmp_path, cfg)["state"] == "started"
    standalone = {**cfg, "runtime_mode": "standalone"}
    assert card_worker_status.diagnose(tmp_path, standalone)["state"] == "not_applicable"


def test_lineworks_alert_actions_point_at_the_adapter_not_the_gateway():
    report = {"status": "degraded", "notify_transport": "lineworks"}
    text = "\n".join(health_watch._reason_rows(report, ["card_delivery_stalled", "worker_start_unverified"]))
    assert "ai.mcs.lineworks" in text and "hermes gateway" not in text
    slack = "\n".join(health_watch._reason_rows({"status": "degraded"}, ["card_delivery_stalled"]))
    assert "hermes gateway restart" in slack
