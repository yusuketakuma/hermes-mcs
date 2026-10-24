"""Anonymous fixed-provider contracts over synthetic MCP replies; no network."""
import json

import pytest

import bounded_http
import drug_info


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(drug_info, "TOKEN_PATH", tmp_path / "missing-token")
    monkeypatch.setattr(drug_info.time, "monotonic", lambda: 100.0)
    calls = []

    def install(text="Title: 架空\nURL: https://www.pmda.go.jp/example", *, status=200, envelope=None, sse=False):
        answer = envelope if envelope is not None else {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": text}]}}
        raw = json.dumps(answer, ensure_ascii=False).encode()
        if sse:
            raw = b"event: message\r\ndata: " + raw + b"\r\n\r\n"

        def wire(endpoint, body, timeout, *, deadline=None, allowed_endpoints=()):
            assert allowed_endpoints == (drug_info.EXA_URL,)
            calls.append((endpoint, body, timeout, deadline))
            return status, {}, raw

        monkeypatch.setattr(bounded_http, "public_mcp_request", wire)
        return calls
    return install


def test_absent_token_uses_only_fixed_anonymous_search_and_filters_domain(mcp):
    calls = mcp("Title: 架空\nURL: https://www.pmda.go.jp/example\n---\n"
                "Title: 外部\nURL: https://example.org/private\n---\n"
                "Title: 偽装\nURL: https://pmda.go.jp.evil.example/x")
    assert drug_info.search("架空 添付文書", drug_info.PMDA) == [
        {"url": "https://www.pmda.go.jp/example", "title": "架空"}]
    endpoint, body, timeout, deadline = calls[0]
    assert endpoint == drug_info.EXA_URL
    assert body == {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "web_search_exa", "arguments": {"query": "site:pmda.go.jp 架空 添付文書", "numResults": 5}}}
    assert timeout == 30.0 and deadline == 175.0


def test_sse_fetch_retains_source_text_and_deadline(mcp):
    text = "販売名 架空薬\u2028本文\n禁忌 架空記載"
    calls = mcp(text, sse=True)
    page, meta = drug_info.fetch_page("https://www.pmda.go.jp/example", deadline=123.0)
    assert page == {"text": text}
    assert meta == {"cache_hit": None, "retrieved_at": None}
    assert calls[0][1]["params"] == {"name": "web_fetch_exa", "arguments": {
        "urls": ["https://www.pmda.go.jp/example"]}}
    assert calls[0][3] == 123.0


@pytest.mark.parametrize("case", ["symlink", "directory", "loose", "invalid"])
def test_existing_invalid_token_never_switches(mcp, tmp_path, case):
    calls = mcp()
    token = drug_info.TOKEN_PATH
    if case == "symlink":
        token.symlink_to(tmp_path / "absent-target")
    elif case == "directory":
        token.mkdir()
    else:
        token.write_text("x" if case == "invalid" else "A" * 64)
        token.chmod(0o644 if case == "loose" else 0o600)
    with pytest.raises(drug_info.DrugInfoError, match="^gateway_unconfigured$"):
        drug_info.search("架空", drug_info.PMDA)
    assert calls == []


@pytest.mark.parametrize("failure", ["unauthorized", "shared_free_quota_exhausted", "gateway_unavailable"])
def test_configured_gateway_failures_never_switch(mcp, monkeypatch, failure):
    calls = mcp()
    token = drug_info.TOKEN_PATH
    token.write_text("A" * 64)
    token.chmod(0o600)

    def refused(*args, **kwargs):
        if failure == "gateway_unavailable":
            raise OSError("synthetic private diagnostic")
        return 401, {}, json.dumps({"error": failure}).encode()

    monkeypatch.setattr(drug_info, "_request", refused)
    with pytest.raises(drug_info.DrugInfoError, match="^" + failure + "$"):
        drug_info.search("架空", drug_info.PMDA)
    assert calls == []


@pytest.mark.parametrize("envelope", [
    {"jsonrpc": "2.0", "id": 1, "error": {"message": "synthetic private diagnostic"}},
    {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "private"}]}},
    {"jsonrpc": "2.0", "id": 1, "result": {"content": []}},
    {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "private"}]}},
    {"result": []},
])
def test_bad_mcp_payload_is_failure_without_echo(mcp, envelope):
    mcp(envelope=envelope)
    with pytest.raises(drug_info.DrugInfoError) as error:
        drug_info.search("架空", drug_info.PMDA)
    assert str(error.value) in {"web_response_invalid", "web_provider_refused"}


def test_no_results_differs_from_unrecognized_success_text(mcp):
    mcp("No results found.")
    assert drug_info.search("架空", drug_info.PMDA) == []
    mcp("Unexpected provider text")
    with pytest.raises(drug_info.DrugInfoError, match="^search_response_invalid$"):
        drug_info.search("架空", drug_info.PMDA)


def test_http_failure_and_oversized_reply_are_not_empty_results(mcp):
    mcp(status=429)
    with pytest.raises(drug_info.DrugInfoError, match="^web_provider_refused$"):
        drug_info.search("架空", drug_info.PMDA)
    mcp("x" * bounded_http.MAX_RESPONSE_BYTES)
    with pytest.raises(drug_info.DrugInfoError, match="^web_response_too_large$"):
        drug_info.search("架空", drug_info.PMDA)


def test_nonofficial_fetch_refused_before_provider(mcp):
    calls = mcp()
    with pytest.raises(drug_info.DrugInfoError, match="^host_refused$"):
        drug_info.fetch_page("https://example.org/private")
    assert calls == []


def test_fresh_watch_requests_do_not_claim_unsupported_freshness(mcp):
    calls = mcp()
    with pytest.raises(drug_info.DrugInfoError, match="^web_freshness_unavailable$"):
        drug_info.fetch_page(drug_info.MENU, fresh=True)
    assert calls == []


def test_elapsed_budget_starts_neither_transport(mcp, monkeypatch):
    calls = mcp()
    monkeypatch.setattr(drug_info.time, "monotonic", lambda: 100.0)
    with pytest.raises(drug_info.DrugInfoError, match="^budget_exhausted$"):
        drug_info.call("search", {}, deadline=99.0)
    assert calls == []


@pytest.mark.parametrize("raw", [b"not JSON", b"data: {broken}\n\n", b"\xff",
                                 b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'])
def test_malformed_json_sse_and_encoding_fail_without_echo(mcp, monkeypatch, raw):
    mcp()
    monkeypatch.setattr(bounded_http, "public_mcp_request", lambda *args, **kwargs: (200, {}, raw))
    with pytest.raises(drug_info.DrugInfoError, match="^web_response_invalid$"):
        drug_info.search("架空", drug_info.PMDA)


def test_search_result_count_is_bounded(mcp):
    mcp("\n---\n".join(f"Title: 架空{i}\nURL: https://www.pmda.go.jp/{i}" for i in range(20)))
    assert len(drug_info.search("架空", drug_info.PMDA)) == 5


@pytest.mark.parametrize("raw", [
    b'{"jsonrpc":"2.0","id":1,"id":1,"result":{"content":[]}}',
    b'{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}',
    b'{"jsonrpc":"2.0","id":1,"result":{"value":Infinity}}',
    b'{"jsonrpc":"2.0","id":1,"result":{"value":1e999}}',
])
def test_external_json_rejects_duplicate_keys_and_nonfinite_values(mcp, monkeypatch, raw):
    mcp()
    monkeypatch.setattr(bounded_http, "public_mcp_request", lambda *a, **k: (200, {}, raw))
    with pytest.raises(drug_info.DrugInfoError, match="^web_response_invalid$"):
        drug_info.search("架空", drug_info.PMDA)


@pytest.mark.parametrize("identifier", [True, False, 1.0, "1", None])
def test_json_rpc_identifier_requires_exact_integer(mcp, identifier):
    mcp(envelope={"jsonrpc": "2.0", "id": identifier, "result": {
        "content": [{"type": "text", "text": "No results found."}]}})
    with pytest.raises(drug_info.DrugInfoError, match="^web_response_invalid$"):
        drug_info.search("架空", drug_info.PMDA)


@pytest.mark.parametrize("text", [
    "URL:", "Title: 架空\nURL:", "Title: 架空\nURL: not-a-url",
    "Title: 架空\nURL: https://", "Title: 架空\nURL: https://www.pmda.go.jp:invalid/path",
    "Title: 架空", "Title: 架空\nURL: https://www.pmda.go.jp/x\nURL: https://www.pmda.go.jp/y",
])
def test_invalid_search_rows_are_not_no_results(mcp, text):
    mcp(text)
    with pytest.raises(drug_info.DrugInfoError, match="^search_response_invalid$"):
        drug_info.search("架空", drug_info.PMDA)


def test_valid_external_domain_is_distinct_from_no_results(mcp):
    mcp("Title: 外部結果\nURL: https://example.org/public")
    with pytest.raises(drug_info.DrugInfoError, match="^search_official_results_missing$"):
        drug_info.search("架空", drug_info.PMDA)


def test_adjacent_title_blocks_keep_each_result_without_separator(mcp):
    mcp("Title: 架空一\nURL: https://www.pmda.go.jp/one\n"
        "Highlights: 本文\nTitle: 架空二\nURL: https://www.pmda.go.jp/two")
    assert drug_info.search("架空", drug_info.PMDA) == [
        {"title": "架空一", "url": "https://www.pmda.go.jp/one"},
        {"title": "架空二", "url": "https://www.pmda.go.jp/two"}]


def test_multiline_sse_data_is_one_json_envelope(mcp, monkeypatch):
    mcp()
    raw = (b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,\r\n'
           b'data: "result":{"content":[{"type":"text","text":"No results found."}]}}\r\n\r\n')
    monkeypatch.setattr(bounded_http, "public_mcp_request", lambda *a, **k: (200, {}, raw))
    assert drug_info.search("架空", drug_info.PMDA) == []


@pytest.mark.parametrize("timeout,deadline,expected", [(0.5, None, 100.5), (2.0, 101.0, 101.0), (1.0, 200.0, 101.0)])
def test_fallback_keeps_caller_timeout_and_absolute_budget(mcp, timeout, deadline, expected):
    calls = mcp("No results found.")
    drug_info.call("search", {"query": "架空", "include_domains": [drug_info.PMDA]},
                   timeout=timeout, deadline=deadline)
    assert calls[0][2] == timeout
    assert calls[0][3] == expected


def test_structured_content_alone_is_not_inferred_as_source_document(mcp):
    mcp(envelope={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"text": "架空"}}})
    with pytest.raises(drug_info.DrugInfoError, match="^web_response_invalid$"):
        drug_info.fetch_page("https://www.pmda.go.jp/example")
