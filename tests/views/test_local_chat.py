"""Synthetic-only patient chat scope, snapshot, cursor and HTTP privacy contracts."""
from email.message import Message
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

import pytest

import ledger
import local_chat_view
import mcs_local_chat as chat
import mcs_view
from patient_context import extract_context
from inference_version import CURRENT_VERSION, VERSION_KEY


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    try:
        with store.db:
            for pid, name in ((1, "合成 花子"), (2, "非対象 太郎")):
                store.db.execute("INSERT INTO patients(project_id,patient_name,fetch_state) VALUES(?,?,'incomplete')", (pid, name))
            for mid in range(1, 124):
                pid = 2 if mid == 123 else 1
                parent = 1 if 61 <= mid <= 115 else None
                store.db.execute("INSERT INTO messages(message_id,project_id,parent_id,sender_id,sender_name,"
                                 "profession,organization,posted_at_ts,body_text,body_state,content_hash,reply_count) "
                                 "VALUES(?,?,?,7,'合成 看護師','看護師','合成事業所',?,?,'full',?,?)",
                                 (mid, pid, parent, mid // 2, f'合成記録 {mid} ﾛｷｿﾆﾝ <script>alert(1)</script>', f'{mid:064x}', 55 if mid == 1 else 0))
            store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=122")
            store.db.execute("UPDATE messages SET posted_at_ts=NULL WHERE message_id=121")
            store.db.execute("INSERT INTO attachments(message_id,name,state,url,local_path,error) VALUES"
                             "(1,'合成資料.pdf','failed','https://private.invalid/signed','/private/file','download_failed'),"
                             "(122,'削除済み資料.pdf','failed',NULL,NULL,'download_failed')")
            for mid, pid in ((1, 1), (122, 1), (123, 2)):
                body = f"病名: 合成疾患{mid}\n合成記録 {mid} ﾛｷｿﾆﾝ <script>alert(1)</script>"
                store.db.execute("UPDATE messages SET body_text=? WHERE message_id=?", (body, mid))
                store.artifact_add_tx("extract_v1", json.dumps({"patient_context": extract_context(body)}),
                                      project_id=pid, message_id=mid,
                                      meta={"hash": f"{mid:064x}", VERSION_KEY: CURRENT_VERSION})
        yield ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
    finally:
        store.close()


def test_patients_scope_and_fold(snapshot):
    view = mcs_view.View(snapshot)
    try:
        got = local_chat_view.patients(view, {1}, query="合成")
        assert [item["project_id"] for item in got["items"]] == [1]
        assert "非対象" not in json.dumps(got, ensure_ascii=False)
        assert local_chat_view.patients(view, set())["items"] == []
        assert len(local_chat_view.patients(view, None)["items"]) == 2
    finally:
        view.close()


@pytest.mark.parametrize("kind", ["timeline", "search", "thread", "attachments", "evidence", "context"])
def test_every_detail_route_rejects_foreign_patient(snapshot, kind):
    with pytest.raises(ValueError, match="project_not_allowed"):
        chat.response(snapshot, {1}, kind, "project=2&message=123&q=合成")


@pytest.mark.parametrize("kind", ["thread", "attachments", "evidence"])
def test_message_must_belong_to_patient(snapshot, kind):
    with pytest.raises(ValueError):
        chat.response(snapshot, {1}, kind, "project=1&message=123")


def test_timeline_pages_have_no_gaps_or_duplicates_and_no_replies(snapshot):
    seen, cursor = [], None
    while True:
        query = "project=1" + ("&cursor=" + cursor if cursor else "")
        page = chat.response(snapshot, {1}, "timeline", query)
        seen.extend(row["message_id"] for row in page["items"])
        assert all(row["parent_id"] is None for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 67
    assert seen[-1] == 121  # unknown posting time is not invented


def test_search_includes_replies_and_normalizes_japanese(snapshot):
    page = chat.response(snapshot, {1}, "search", "project=1&q=ロキソニン")
    assert any(row["parent_id"] == 1 for row in page["items"])
    assert all(row["project_id"] == 1 for row in page["items"])
    assert all("body_html" not in row and "source_url" not in row for row in page["items"])


def test_thread_paginates_and_attachment_detail_has_no_paths(snapshot):
    first = chat.response(snapshot, {1}, "thread", "project=1&message=1")
    second = chat.response(snapshot, {1}, "thread", "project=1&message=1&cursor=" + first["next_cursor"])
    assert len(first["items"]) + len(second["items"]) == 55
    assert not ({r["message_id"] for r in first["items"]} & {r["message_id"] for r in second["items"]})
    files = chat.response(snapshot, {1}, "attachments", "project=1&message=1")
    assert files["items"][0]["name"] == "合成資料.pdf"
    assert files["items"][0]["state"] == "failed"
    assert "private" not in json.dumps(files)


def test_deleted_post_hides_retained_body_and_attachment(snapshot):
    result = chat.response(snapshot, {1}, "evidence", "project=1&message=122")["message"]
    assert result["body_text"] is None
    assert result["summary_lines"] == result["patient_context"] == result["attachments"] == []
    assert chat.response(snapshot, {1}, "attachments", "project=1&message=122")["items"] == []


def test_shared_summary_and_stamp_projection(snapshot, monkeypatch):
    monkeypatch.setattr(local_chat_view, "structured_summary", lambda db, mid, **kwargs: ([f"合成要約 {mid}"], None))
    result = chat.response(snapshot, {1}, "evidence", "project=1&message=1")["message"]
    assert result["summary_lines"] == ["合成要約 1"]
    assert result["stamp_line"] == "スタンプ 未取得"
    assert result["profession"] == "看護師"


def test_cursor_binds_query_patient_and_generation(snapshot):
    page = chat.response(snapshot, None, "timeline", "project=1")
    cursor = page["next_cursor"]
    for route, query in (("timeline", "project=2"), ("search", "project=1&q=合成")):
        with pytest.raises(ValueError, match="cursor_scope_or_generation_changed"):
            chat.response(snapshot, None, route, query + "&cursor=" + cursor)
    view = mcs_view.View(snapshot)
    try:
        view.meta["generation_id"] = "another-synthetic-generation"
        with pytest.raises(ValueError, match="cursor_scope_or_generation_changed"):
            local_chat_view.read(view, {1}, "timeline", 1, cursor=cursor)
    finally:
        view.close()


def test_live_ledger_and_missing_snapshot_are_rejected(tmp_path):
    path = tmp_path / "live.db"
    store = ledger.Ledger(str(path))
    store.close()
    with pytest.raises(ValueError):
        chat.response(path, None, "patients", "")
    with pytest.raises(SystemExit) as error:
        chat.main(["--snapshot", str(tmp_path / "missing.db"), "--all-patients"])
    assert error.value.code == 2


def test_reads_write_nothing_to_the_snapshot(snapshot, monkeypatch):
    # The summary state, reply checks and OCR states read configuration as the chat cards do; nothing is written.
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    path = Path(snapshot)
    before = hashlib.sha256(path.read_bytes()).digest()
    for kind, query in (("patients", ""), ("timeline", "project=1"), ("evidence", "project=1&message=1"),
                        ("counts", "project=1"), ("attachments", "project=1&message=1")):
        chat.response(snapshot, {1}, kind, query)
    assert hashlib.sha256(path.read_bytes()).digest() == before


def handler(path, headers=None):
    obj = chat.ChatHandler.__new__(chat.ChatHandler)
    obj.server = SimpleNamespace(server_port=8765, token="synthetic-capability", snapshot="unused", allowed={1}, tailnet_host=None)
    obj.path = path
    obj.headers = Message()
    for name, value in (headers if headers is not None else [("Host", "127.0.0.1:8765")]):
        obj.headers[name] = value
    results = []
    obj._send = lambda *args: results.append(args)
    return obj, results


@pytest.mark.parametrize("headers", [[], [("Host", "evil.invalid:8765")],
    [("Host", "127.0.0.1:8765"), ("Host", "127.0.0.1:8765")],
    [("Host", "127.0.0.1:8765"), ("Origin", "http://evil.invalid")],
    [("Host", "127.0.0.1:8765"), ("Sec-Fetch-Site", "cross-site")],
    [("Host", "127.0.0.1:8765"), ("Sec-Fetch-Site", "same-site")]])
def test_http_rejects_rebinding_and_cross_site(headers):
    obj, result = handler("/synthetic-capability/", headers)
    obj.do_GET()
    assert result[0][0] == 403


@pytest.mark.parametrize("path", ["/", "/wrong/", "/synthetic-capability/../config.json", "/synthetic-capability/style.css/../app.js", "/synthetic-capability/あ"])
def test_http_requires_capability_and_allowlisted_routes(path):
    obj, result = handler(path)
    obj.do_GET()
    assert result[0][0] == 404


def test_assets_are_static_and_patient_text_is_not_html():
    obj, result = handler("/synthetic-capability/")
    obj.do_GET()
    assert result[0][0] == 200
    assert b"synthetic" not in result[0][1]
    js = (chat.ASSETS / "app.js").read_text()
    assert "textContent" in js and "innerHTML" not in js
    assert "localStorage" not in js and "sessionStorage" not in js


def test_http_errors_do_not_echo_paths_queries_or_exception(monkeypatch):
    obj, result = handler("/synthetic-capability/api/patients?q=private")
    monkeypatch.setattr(chat, "response", lambda *args: (_ for _ in ()).throw(ValueError("private")))
    obj.do_GET()
    assert result == [(400, b'{"error": "bad_request"}', "application/json")]


@pytest.mark.parametrize("code", ["search_query_empty", "bad_search_filter", "search_cursor_invalid"])
def test_http_passes_fixed_search_codes(monkeypatch, code):
    obj, result = handler("/synthetic-capability/api/sources?project=1&q=")
    monkeypatch.setattr(chat, "response", lambda *args: (_ for _ in ()).throw(ValueError(code)))
    obj.do_GET()
    assert result == [(400, json.dumps({"error": code}).encode(), "application/json")]


def test_http_unreadable_settings_are_unavailable_not_defaults(monkeypatch):
    import mcs_util
    obj, result = handler("/synthetic-capability/api/registered?project=1")
    monkeypatch.setattr(chat, "response", lambda *args: (_ for _ in ()).throw(
        mcs_util.ProviderSettingsUnreadable("provider_settings_unreadable")))
    obj.do_GET()
    assert result == [(503, b'{"error":"config_unreadable"}', "application/json")]


def test_headers_forbid_cache_external_resources_and_embedding():
    obj, _ = handler("/")
    headers = {}
    obj.send_response = lambda status: None
    obj.send_header = lambda key, value: headers.update({key: value})
    obj.end_headers = lambda: None
    obj.wfile = BytesIO()
    chat.ChatHandler._send(obj, 200, b"synthetic")
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert not any("Access-Control" in key for key in headers)


def test_server_is_loopback_only_and_capability_changes(monkeypatch):
    addresses = []
    monkeypatch.setattr(chat.ThreadingHTTPServer, "__init__", lambda self, address, handler: addresses.append(address))
    one, two = chat.ChatServer("synthetic", {1}), chat.ChatServer("synthetic", {1})
    assert addresses == [("127.0.0.1", 0), ("127.0.0.1", 0)]
    assert one.token != two.token and len(one.token) >= 40


def test_browser_preconnections_do_not_block_other_clients(monkeypatch):
    assert issubclass(chat.ChatServer, chat.ThreadingHTTPServer)
    assert chat.ChatServer.daemon_threads
    timeouts = []
    obj = chat.ChatHandler.__new__(chat.ChatHandler)
    obj.request = SimpleNamespace(settimeout=timeouts.append)
    monkeypatch.setattr(chat.BaseHTTPRequestHandler, "setup", lambda self: None)
    obj.setup()
    assert timeouts == [10]


def test_loopback_bind_never_resolves_network_names(monkeypatch):
    bound = []
    monkeypatch.setattr(chat.TCPServer, "server_bind", lambda server: bound.append(server.server_address))
    monkeypatch.setattr("socket.getfqdn", lambda *_: pytest.fail("loopback server must not trigger DNS or mDNS"))
    server = chat.ChatServer.__new__(chat.ChatServer)
    server.server_address = ("127.0.0.1", 12345)
    server.server_bind()
    assert bound == [("127.0.0.1", 12345)]
    assert (server.server_name, server.server_port) == ("127.0.0.1", 12345)


@pytest.mark.parametrize("args", [[], ["--project", "0"], ["--project", "-1"], ["--all-patients", "--port", "65536"]])
def test_cli_requires_explicit_valid_scope(args):
    with pytest.raises(SystemExit) as error:
        chat.main(["--snapshot", "synthetic", *args])
    assert error.value.code == 2


def test_browser_dom_contracts():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; browser DOM contract checks require it")
    subprocess.run([node, str(Path(__file__).with_name("local_chat_dom_checks.js")),
                    str(chat.ASSETS / "app.js")], check=True, capture_output=True, text=True)


def test_patient_header_context_is_scoped_grounded_and_readonly(snapshot):
    before = hashlib.sha256(Path(snapshot).read_bytes()).digest()
    data = chat.response(snapshot, {1}, "context", "project=1")
    text = json.dumps(data, ensure_ascii=False)
    assert "合成疾患1" in text
    assert "合成疾患122" not in text  # deleted source
    assert "合成疾患123" not in text  # different patient
    assert "source_url" not in text and "local_path" not in text
    assert data["sections"][0]["label"] == "主病名"
    assert data["sections"][0]["items"][0]["evidence"].startswith("病名:")
    assert hashlib.sha256(Path(snapshot).read_bytes()).digest() == before
    assert chat.response(snapshot, {2}, "context", "project=2")["sections"]
    with pytest.raises(ValueError):
        chat.response(snapshot, {1}, "context", "project=1&message=123")


@pytest.mark.parametrize("body,category,heading", [
    ("アレルギー：なし\n訪問しました。体温36.5℃。朝食は全量。", "allergies", "アレルギー：なし"),
    ("アレルギー：なし\nただし家族から合成薬による発疹の情報あり。確認中。", "allergies", "アレルギー：なし"),
    ("服薬管理: 家族が管理\n本日は合成の訪問記録。", "medication_management", "服薬管理: 家族が管理"),
    ("既往歴\n合成疾患の治療歴。\n後続の記録。", "history", None),
])
def test_multiline_context_never_promotes_unscoped_continuation(body, category, heading):
    original = extract_context(body)[0]
    assert original["category"] == category
    projected = local_chat_view._context_item(original, category)
    assert projected["source_evidence"] == body
    assert projected["subject"] == "unspecified"
    assert "分類範囲は未確認" in projected["scope_note"]
    assert projected["details"] == []
    assert projected["evidence"] == heading
    assert "訪問しました" not in projected["text"]
    assert "確認中" not in projected["text"]


def test_multiline_context_boundary_applies_to_header_and_message(tmp_path):
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    body = "アレルギー：なし\n訪問しました。体温36.5℃。朝食は全量。"
    try:
        with store.db:
            store.db.execute("INSERT INTO patients(project_id,patient_name) VALUES(1,'合成花子'),(2,'合成太郎')")
            for mid, text in ((1, body), (2, "アレルギー：合成薬Bで発疹")):
                store.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state,content_hash) "
                                 "VALUES(?,?,?,'full',?)", (mid, mid, text, f"{mid:064x}"))
                store.artifact_add_tx("extract_v1", json.dumps({"patient_context": extract_context(text)}),
                                      project_id=mid, message_id=mid,
                                      meta={"hash": f"{mid:064x}", VERSION_KEY: CURRENT_VERSION})
        snapshot = ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
        header = chat.response(snapshot, {1, 2}, "context", "project=1")["sections"][0]["items"][0]
        detail = chat.response(snapshot, {1, 2}, "evidence", "project=1&message=1")["message"]["patient_context"][0]
        assert header["text"] == detail["text"] == "アレルギー：なし"
        assert header["evidence"] == detail["evidence"] == "アレルギー：なし"
        assert header["source_evidence"] == detail["source_evidence"] == body
        other = chat.response(snapshot, {1, 2}, "context", "project=2")
        assert "合成薬Bで発疹" in json.dumps(other, ensure_ascii=False)
        assert "体温" not in json.dumps(other, ensure_ascii=False)
    finally:
        store.close()


def test_context_retains_wider_published_quote_and_subject():
    quote = "母の記録です。\nアレルギー：なし\nただし合成薬について確認中。"
    item = {"text": "なし", "evidence": "アレルギー：なし", "quote": quote,
            "subject": "unspecified", "reported_subject": "family", "details": []}
    got = local_chat_view._context_item(item, "allergies")
    assert got["text"] == item["text"]
    assert got["evidence"] == item["evidence"]
    assert got["source_evidence"] == quote
    assert got["reported_subject"] == "family"
    assert "原文あり" in got["scope_note"]


@pytest.mark.parametrize("subject", ["family", "patient", "other", "unspecified"])
def test_narrow_context_fact_does_not_take_another_subjects_heading(subject):
    evidence = "アレルギー：なし\n母は合成薬Bで発疹。"
    item = {"text": "母は合成薬Bで発疹。", "evidence": evidence, "subject": subject}
    got = local_chat_view._context_item(item, "allergies")
    assert got["text"] == item["text"]
    assert got["subject"] == subject
    assert got["source_evidence"] == evidence
    assert got["evidence"] is None
    assert got["text"] != "アレルギー：なし"


@pytest.mark.parametrize("subject,reported", [("unspecified", None), ("unspecified", "unspecified"),
    ("unspecified", "family"), ("family", "family"), ("patient", "patient"), ("other", "other")])
def test_multiline_derived_record_stays_out_of_classified_text(subject, reported):
    evidence = "アレルギー：なし\n母は合成薬Eで発疹。"
    item = {"text": "なし\n母は合成薬Eで発疹。", "evidence": evidence, "quote": evidence,
            "subject": subject, "reported_subject": reported, "details": []}
    got = local_chat_view._context_item(item, "allergies")
    assert "\n" not in got["text"] and "合成薬E" not in got["text"]
    assert got["subject"] == subject and got["reported_subject"] == reported
    assert got["source_evidence"] == evidence
    assert got["details"] == []


def test_multiline_record_wording_not_in_raw_quote_is_preserved_separately():
    item = {"text": "合成の保存済み記載。\nただし確認中。", "evidence": "確認中。", "subject": "unspecified"}
    got = local_chat_view._context_item(item, "allergies")
    assert got["text"] == "複数行の記録（分類範囲は未確認）"
    assert got["record_text"] == item["text"]
    assert got["source_evidence"] == item["evidence"]


def test_orphan_reply_remains_readable_without_exposing_foreign_parent(tmp_path):
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    try:
        with store.db:
            store.db.execute("INSERT INTO patients(project_id,patient_name) VALUES(1,'合成'),(2,'別患者')")
            store.db.execute("INSERT INTO messages(message_id,project_id,parent_id,body_text,body_state) VALUES"
                             "(7,1,999,'合成の孤立した返信','full'),(999,2,NULL,'別患者の本文','full')")
        snapshot = ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
        data = chat.response(snapshot, {1}, "search", "project=1&q=合成")
        assert data["items"][0]["parent_available"] is False
        assert "別患者" not in json.dumps(data, ensure_ascii=False)
        evidence = chat.response(snapshot, {1}, "evidence", "project=1&message=7")
        assert evidence["message"]["body_text"] == "合成の孤立した返信"
        assert chat.response(snapshot, {1}, "thread", "project=1&message=7")["items"] == []
    finally:
        store.close()


def test_sections_list_every_card_view_except_slack_only_measurements():
    keys = [item["key"] for item in chat.response("unused", {1}, "sections", "")["items"]]
    assert keys[0] == "summary" and "previsit" in keys and "timeline" in keys and "signals" in keys
    assert "measurements" not in keys and "meds" not in keys


@pytest.mark.parametrize("section", [item["key"] for item in local_chat_view.sections()["items"]])
def test_panel_reuses_card_views_scoped_and_readonly(snapshot, section):
    before = hashlib.sha256(Path(snapshot).read_bytes()).digest()
    data = chat.response(snapshot, {1}, "panel", f"project=1&section={section}")
    assert data["section"] == section and data["panel"]["title"]
    text = json.dumps(data, ensure_ascii=False)
    assert "非対象" not in text and "合成疾患123" not in text
    assert all(set(item) <= {"group", "text", "quotes", "source", "message_id"} for item in data["panel"]["items"])
    with pytest.raises(ValueError, match="project_not_allowed"):
        chat.response(snapshot, {1}, "panel", f"project=2&section={section}")
    assert hashlib.sha256(Path(snapshot).read_bytes()).digest() == before


def test_panel_medication_view_is_bound_to_one_post_of_the_patient(snapshot):
    data = chat.response(snapshot, {1}, "panel", "project=1&section=meds&message=1")
    assert data["panel"]["title"].endswith("選択した投稿の薬剤")
    for query in ("project=1&section=meds", "project=1&section=meds&message=123",
                  "project=1&section=summary&message=1"):
        with pytest.raises(ValueError):
            chat.response(snapshot, {1}, "panel", query)


@pytest.mark.parametrize("route,query", [
    ("panel", "project=1&section=unknown"), ("panel", "project=1&section=summary&page=-1"),
    ("panel", "project=1&section=summary&page=x"), ("panel", "project=1&section=background&role=nope"),
    ("panel", "project=1&section=summary&q=合成"), ("timeline", "project=1&section=summary"),
    ("timeline", "project=1&page=1"), ("patients", "role=pharmacy")])
def test_panel_arguments_are_strict(snapshot, route, query):
    with pytest.raises(ValueError):
        chat.response(snapshot, {1}, route, query)


@pytest.mark.parametrize("last_run, errors, definite", [(None, [], False), (1.0, ["synthetic"], False), (1.0, [], True)])
def test_empty_signal_list_is_definite_only_after_a_complete_readable_run(monkeypatch, last_run, errors, definite):
    import mcs_signals
    monkeypatch.setattr(mcs_signals, "current_open", lambda *_args, **_kw: {
        "items": [], "pipeline_last_run_at": last_run, "truncated": False, "returned": 0, "total": 0, "errors": errors})
    view = local_chat_view._signals(SimpleNamespace(db=None), 1)
    assert (view["empty"] == "未解消のシグナルはありません。") is definite
    assert definite or "見つかりませんでした" in view["empty"]


def test_drug_search_reads_only_the_approved_dictionary(monkeypatch, tmp_path):
    import drug_catalog
    import mcs_util
    from medications_testkit import _source_document

    document = _source_document()
    document["entries"][0]["products"] = [{"medicine": "900000001", "name": "架空製品錠5mg", "unit": "錠", "form_code": "1"}]
    raw = json.dumps(document, ensure_ascii=False).encode()
    (tmp_path / "catalog.json").write_bytes(raw)
    (tmp_path / "catalog.json").chmod(0o600)
    cfg = {"drug_map": {"path": str(tmp_path / "catalog.json"), "sha256": hashlib.sha256(raw).hexdigest()}}
    monkeypatch.setattr(mcs_util, "load_config", lambda: cfg)
    data = chat.response("unused", {1}, "drugs", "q=架空製品")
    assert data == {"status": "active", "items": [{"code": "900000001", "code_system": drug_catalog.CODE_SYSTEM,
                                                   "name": "架空製品錠5mg", "generic_name": None, "unit": "錠"}]}
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    assert chat.response("unused", {1}, "drugs", "q=架空") == {"status": "unconfigured", "items": []}
    for query in ("q=", "q=架空&project=1", "q=架空&cursor=x"):
        with pytest.raises(ValueError):
            chat.response("unused", {1}, "drugs", query)


def test_visit_brief_route_is_scoped_and_takes_the_last_visit_time(snapshot, monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    data = chat.response(snapshot, {1}, "brief", "project=1&since=1793458800")
    assert data["since"] == 1793458800 and data["snapshot"]["generation_id"]
    assert [section["key"] for section in data["sections"]][-1] == "signals"
    assert chat.response(snapshot, {1}, "brief", "project=1")["since"] is None
    with pytest.raises(ValueError, match="project_not_allowed"):
        chat.response(snapshot, {1}, "brief", "project=2")
    for query in ("project=1&since=nan", "project=1&since=-1", "project=1&since=1.5", "project=1&message=1",
                  "project=1&q=x", "since=1"):
        with pytest.raises(ValueError):
            chat.response(snapshot, {1}, "brief", query)
    with pytest.raises(ValueError):
        chat.response(snapshot, {1}, "timeline", "project=1&since=1")


def test_context_panel_rows_keep_their_source_post_for_the_open_button(snapshot):
    items = chat.response(snapshot, {1}, "panel", "project=1&section=background")["panel"]["items"]
    assert 1 in {item["message_id"] for item in items}
    import notify_views
    view = mcs_view.View(snapshot)
    try:
        card = notify_views.patient_context_list(view.db, 1, "background", cfg={}, since=0,
                                                 now=view.meta["generated_at"], first_view=True)
    finally:
        view.close()
    assert all("message_id" not in item for item in card["items"])  # chat cards are unchanged


def test_attachment_ocr_rows_open_the_post_that_carries_the_attachment(snapshot, monkeypatch):
    import notify_views
    rows = [{"text": "添付の記録", "message_id": 0, "parent_message_id": 7},
            {"text": "出所のない記録"}]
    monkeypatch.setattr(notify_views, "patient_context_list", lambda *a, **k: {"items": rows})
    items = chat.response(snapshot, {1}, "panel", "project=1&section=background")["panel"]["items"]
    assert [item["message_id"] for item in items] == [7, None]


def test_visit_sheet_pages_count_from_the_same_last_visit(snapshot):
    sheet = chat.response(snapshot, {1}, "panel", "project=1&section=changes&since=1700000000&page=0")["panel"]
    assert any(line.startswith("前回の訪問: ") for line in sheet["head"]), sheet["head"]
    plain = chat.response(snapshot, {1}, "panel", "project=1&section=changes")["panel"]
    assert any(line.startswith("初回（直近7日）") for line in plain["head"]), plain["head"]
    for query in ("project=1&section=summary&since=1", "project=1&section=signals&since=1",
                  "project=1&section=changes&since=-1", "project=1&since=1"):
        with pytest.raises(ValueError):
            chat.response(snapshot, {1}, "panel" if "section" in query else "timeline", query)


@pytest.mark.parametrize("project", ["%2B1", "%201", "1%20", "%EF%BC%91", "1_0", "-1", ""])
def test_a_room_is_named_in_plain_digits_so_the_access_record_names_the_same_room(snapshot, project):
    # int() would read these as a room that read_subject cannot name: the read would go unrecorded.
    with pytest.raises(ValueError, match="bad_arguments"):
        chat.response(snapshot, {1, 10}, "timeline", "project=" + project)
    assert chat.read_subject("timeline", "project=" + project) == (None, [], False)
    assert chat.read_subject("timeline", "project=1") == (1, [1], False)
