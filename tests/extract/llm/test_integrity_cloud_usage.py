"""Known cloud token counters survive the shared integrity summary (#155)."""
import anthropic_client
import extract_llm
import openai_client

ANTHROPIC = {"input_tokens": 10, "output_tokens": 2,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3}
OPENAI = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}


def _run(monkeypatch, usages):
    """Real _llm_call with a stub provider whose usage passed the real parser."""
    monkeypatch.setattr(extract_llm._CALL_NOTES, "items", [], raising=False)
    for kind, usage in usages:
        def provider(prompt, *, _usage=usage, **kwargs):
            return {"status": 200, "text": '{"ok":true}', "finish_reason": "stop",
                    "model": "synthetic", "usage": _usage}
        extract_llm._llm_call("合成の本文", kind=kind, provider=provider)
    return extract_llm._integrity_summary(extract_llm._note_list())


def test_anthropic_first_and_repair_keep_input_output_and_no_invented_total(monkeypatch):
    usage = anthropic_client.validate_usage(ANTHROPIC)
    assert _run(monkeypatch, [("first", usage)])["usage"] == ANTHROPIC
    summary = _run(monkeypatch, [("first", usage), ("repair", usage)])
    assert summary["first_calls"] == summary["repair_calls"] == 1
    assert summary["usage"] == {key: 2 * value for key, value in ANTHROPIC.items()}
    assert "total_tokens" not in summary["usage"]
    assert extract_llm._llm_call_stats([summary])["tokens"] is None


def test_openai_keeps_input_and_output_besides_total(monkeypatch):
    usage = openai_client.validate_usage(dict(OPENAI, input_tokens_details={
        "cached_tokens": 4, "cache_write_tokens": 0}))
    summary = _run(monkeypatch, [("first", usage), ("repair", usage)])
    assert summary["usage"] == {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}
    assert extract_llm._llm_call_stats([summary])["tokens"] == 24


def test_local_reused_and_partial_controls_are_unchanged():
    local = {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}
    summary = extract_llm._integrity_summary([
        {"kind": "first", "usage": local}, {"kind": "repair", "usage": local},
        {"kind": "repair", "usage": OPENAI, "reused_response": True},
        {"kind": "first", "usage": {"input_tokens": 5, "output_tokens": -1}}])
    assert summary["reused_responses"] == 1 and summary["calls"] == 3
    assert summary["usage"] == {"prompt_tokens": 14, "completion_tokens": 2,
                                "total_tokens": 16, "input_tokens": 5}
