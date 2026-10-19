"""Private summary clicks retain the configured extraction generation."""
import notify_actions


def test_private_summary_click_passes_current_config(monkeypatch):
    cfg = {"local_llm": {"model": "synthetic-model"}}
    seen = []

    def summary(db, project_id, *, cfg=None):
        seen.append((project_id, cfg))
        return "合成のまとめ", "情報抽出: 処理中"

    monkeypatch.setattr(notify_actions, "patient_summary_text", summary)
    result = notify_actions._act_view(None, {}, {"project_id": 1}, "summary", {}, 0, cfg=cfg)
    assert seen == [(1, cfg)]
    # db=None makes the nightly navigation fail, so the notice precedes the summary
    assert result["body"].endswith("\n\n情報抽出: 処理中")
