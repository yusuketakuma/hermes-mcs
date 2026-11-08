"""Public drug-information contracts over fictional rows and a stubbed local gateway; no network."""
import update_apply
import update_cli
import update_recovery
import update_runtime_migration
import update_services
import json
import os

import pytest

import drug_info
import mcs_drug
import mcs_update
import safe_io

BODY = "2026年6月改訂（第4版）\n販売名 架空アオ錠１０ｍｇ「架空」\n\n禁忌 架空の記載\n\n用法及び用量 架空の記載"
PDF = "https://www.pmda.go.jp/PmdaSearch/iyakuDetail/ResultDataSetPDF/1_{}_1_01"
ROWS = [
    {"medicine": "900000001", "name": "架空アオ錠１０ｍｇ「架空」", "general_name": "", "abolished_on": "99999999"},
    {"medicine": "900000002", "name": "架空アオ錠２０ｍｇ「架空」", "general_name": "", "abolished_on": "99999999"},
    {"medicine": "900000003", "name": "架空般錠", "general_name": "【般】架空錠", "abolished_on": "0"},
    {"medicine": "900000004", "name": "架空別般錠", "general_name": "【般】架空錠", "abolished_on": "0"},
]
MENU = ("**※ 医薬品マスターは令和7年度版(令和8年3月31日まで適用)** | | "
        "医薬品マスター　（1MB） | 100件 | 令和 8年 3月17日 | ○ | 特定器材マスター　（1KB） | 5件 | 令和 8年 2月27日 | "
        "医薬品マスター　（1MB） | 200件 | 令和 8年10月13日 | ○ |")


class Gateway:
    """Records every outbound request; serves fictional search/fetch answers."""
    def __init__(self, bodies=None, results=None, refuse=None, etag=None):
        self.sent = []
        self.bodies = bodies or {}
        self.results = results if results is not None else [PDF.format("A"), "https://www.pmda.go.jp/x?user=1", PDF.format("B")]
        self.refuse, self.etag = refuse, etag

    def __call__(self, name, args, timeout=75.0, deadline=None):
        self.sent.append((name, args))
        if self.refuse:
            raise drug_info.DrugInfoError(self.refuse)
        if name == "search":
            return {"data": {"results": [{"url": u, "title": "架空"} for u in self.results]}}
        url = args["urls"][0]
        if args.get("if_none_match") and args["if_none_match"] == self.etag:
            page = {"text": "", "not_modified": True, "etag": self.etag}
        elif url in self.bodies:
            page = {"text": self.bodies[url], "etag": self.etag}
        else:
            return {"results": [{"url": url, "result": {"data": {"results": [], "errors": [{"error": "target_unreachable"}]}}}]}
        return {"results": [{"url": url, "result": {"retrieved_at": 1, "cache_hit": False, "data": {"results": [page], "errors": []}}}]}


@pytest.fixture
def gateway(monkeypatch):
    def install(**kw):
        fake = Gateway(**kw)
        monkeypatch.setattr(drug_info, "call", fake)
        return fake
    return install


def test_only_exact_unique_master_names_resolve():
    assert [e["code"] for e in drug_info.resolve("架空アオ錠10mg「架空」", ROWS)[0]] == ["900000001"]  # NFKC
    entries, suggestions = drug_info.resolve("架空アオ", ROWS)
    assert entries == [] and suggestions
    assert len(drug_info.resolve("【般】架空錠", ROWS)[0]) == 2  # shared generic label stays ambiguous


def test_bundled_public_rows_do_not_import_the_drug_cli(tmp_path, monkeypatch):
    import builtins
    import import_drug_master as importer
    from test_mcs_drug_bundled import _bundle

    bundle = _bundle(tmp_path)
    load = importer.load_bundled_master
    monkeypatch.setattr(importer, "load_bundled_master", lambda: load(bundle))
    original_import = builtins.__import__

    def without_cli(name, *args, **kwargs):
        assert name != "mcs_drug", "public master reads must not load the drug CLI"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_cli)
    manifest, rows = drug_info.master_rows()
    assert manifest["rows"] == len(rows) == 2
    assert {row["medicine"] for row in rows} == {"900000001", "900000002"}


def test_insert_is_confirmed_only_by_the_product_name_in_the_document(gateway):
    fake = gateway(bodies={PDF.format("A"): "販売名 別の架空錠", PDF.format("B"): BODY})
    entry = drug_info.resolve("架空アオ錠１０ｍｇ「架空」", ROWS)[0][0]
    result = drug_info.find_insert(entry)
    assert (result["status"], result["url"], result["revision"]) == ("found", PDF.format("B"), "2026年6月改訂（第4版）")
    assert result["excerpts_are_partial"] and result["read_the_source_document"]
    assert result["checked_before"][0]["result"] == "product name not in document"
    assert all("user=1" not in a.get("urls", [""])[0] for n, a in fake.sent if n == "fetch_content")
    assert fake.sent[0] == ("search", {"query": "架空アオ錠10mg「架空」 添付文書", "include_domains": ["pmda.go.jp"],
                                       "public_only": True, "cache_policy": "docs"})
    gateway(bodies={PDF.format("A"): "無関係", PDF.format("B"): "無関係"})
    assert drug_info.find_insert(entry)["status"] == "unconfirmed"


def test_listing_compare_never_turns_unknown_into_no_update():
    assert drug_info.listed_editions(MENU) == [("20260317", 100), ("20261013", 200)]
    assert drug_info.compare_listing(MENU, ("20261013", 200))[0] == "match"
    assert drug_info.compare_listing(MENU, ("20260930", 190))[0] == "newer"
    assert drug_info.compare_listing(MENU, ("20261013", 199))[0] == "mismatch"
    for broken in ("layout changed", MENU.replace("10月13日", "13月13日"),
                   MENU + " 医薬品マスター | 300件 | 日付不明 |"):
        assert drug_info.compare_listing(broken, ("20261013", 200))[0] == "unknown"
    assert drug_info.compare_listing(MENU, None)[0] == "unknown"


def test_listing_fetch_failures_are_unknown(gateway):
    gateway(refuse="shared_free_quota_exhausted")
    assert drug_info.check_listing(("20261013", 200))[0] == "unknown"
    gateway(bodies={drug_info.MENU: MENU}, etag='"e"')
    assert drug_info.check_listing(("20261013", 200)) == ("match", "公式の最新 20261013（200件）と同梱版が一致")


def test_guideline_accepts_only_general_disease_keywords(gateway):
    fake = gateway(results=["https://minds.jcqhc.or.jp/summary/c1/", "https://example.org/x"])
    assert [r["url"] for r in drug_info.guideline("高血圧")["results"]] == ["https://minds.jcqhc.or.jp/summary/c1/"]
    for bad in ("2026年10月 架空", "x", "あ" * 41, "ID:1", "a@example.org", "高血圧１"):
        with pytest.raises(drug_info.DrugInfoError):
            drug_info.guideline(bad)
    assert len(fake.sent) == 1


def test_formulary_resolves_locally_first_and_keeps_shared_inserts(gateway, tmp_path):
    fake = gateway(bodies={PDF.format("B"): BODY + " 架空アオ錠２０ｍｇ「架空」"})
    names = ["# コメント", "患者 架空花子 架空アオ", "架空アオ錠１０ｍｇ「架空」", "架空アオ錠２０ｍｇ「架空」", "【般】架空錠"]
    report = {r["name"]: r for r in drug_info.formulary_add(names, ROWS, limit=5, root=tmp_path)}
    assert report["患者 架空花子 架空アオ"]["status"] == "unresolved" and not report["患者 架空花子 架空アオ"]["sent"]
    assert report["【般】架空錠"]["status"] == "ambiguous"
    assert report["架空アオ錠１０ｍｇ「架空」"]["status"] == "registered"
    assert report["架空アオ錠２０ｍｇ「架空」"]["status"] == "already_watched_url"
    assert all("架空花子" not in a.get("query", "") for _n, a in fake.sent)
    watch = drug_info.load_watch(tmp_path)
    assert len(watch["inserts"]) == 1
    assert (tmp_path / "watch.json").stat().st_mode & 0o777 == 0o600


def test_formulary_stops_on_quota_and_honours_the_limit(gateway, tmp_path):
    names = ["架空アオ錠１０ｍｇ「架空」", "架空アオ錠２０ｍｇ「架空」"]
    fake = gateway(refuse="shared_free_quota_exhausted")
    assert [r["status"] for r in drug_info.formulary_add(names, ROWS, root=tmp_path)] == ["error", "deferred"]
    assert len(fake.sent) == 1
    gateway()
    assert drug_info.formulary_add(names, ROWS, limit=1, root=tmp_path)[1]["status"] == "deferred"


def test_recheck_keeps_reporting_until_ack_and_uses_etag(gateway, tmp_path):
    gateway(bodies={PDF.format("B"): BODY}, etag='"v1"')
    entry = drug_info.resolve("架空アオ錠１０ｍｇ「架空」", ROWS)[0][0]
    drug_info.register({"url": PDF.format("B"), "sha256": "0" * 64, "revision": "旧", "etag": None}, entry, tmp_path)
    assert [s for _i, s, _r in drug_info.formulary_check(root=tmp_path)] == ["changed"]
    fake = gateway(bodies={PDF.format("B"): BODY}, etag='"v1"')
    assert [s for _i, s, _r in drug_info.formulary_check(root=tmp_path)] == ["changed"]  # 304, still not acked
    assert fake.sent[-1][1]["if_none_match"] == '"v1"'
    observed = drug_info.load_watch(tmp_path)["inserts"][0]["observed_sha256"]
    drug_info.formulary_ack("架空アオ錠１０ｍｇ「架空」", observed, tmp_path)
    assert [s for _i, s, _r in drug_info.formulary_check(root=tmp_path)] == ["unchanged"]
    assert drug_info.load_watch(tmp_path)["inserts"][0]["revision"] == "2026年6月改訂（第4版）"
    with pytest.raises(drug_info.DrugInfoError):
        drug_info.formulary_ack("架空アオ錠１０ｍｇ「架空」", "x", tmp_path)


def test_reregistration_during_a_check_discards_the_old_observation(gateway, tmp_path, monkeypatch):
    gateway(bodies={PDF.format("B"): BODY})
    entry = drug_info.resolve("架空アオ錠１０ｍｇ「架空」", ROWS)[0][0]
    drug_info.register({"url": PDF.format("B"), "sha256": "0" * 64, "revision": None}, entry, tmp_path)
    real_update = drug_info.update

    def reregister_then_merge(change, root=None):
        def wipe(data):
            data["inserts"][0]["gen"] = "new-generation"
        real_update(wipe, root)
        return real_update(change, root)
    monkeypatch.setattr(drug_info, "update", reregister_then_merge)
    assert drug_info.formulary_check(root=tmp_path) == []
    assert drug_info.load_watch(tmp_path)["inserts"][0]["last_checked"] == 0


def test_daily_notices_are_owed_once_and_marked_only_after_enqueue(gateway, tmp_path):
    gateway(bodies={drug_info.MENU: MENU})
    owed = drug_info.daily_notices(("20260930", 190), tmp_path)
    assert [k for k, _v, _t in owed] == ["listing"] and "新しい" in owed[0][2]
    assert drug_info.daily_notices(("20260930", 190), tmp_path) != []  # not marked yet -> still owed
    drug_info.mark_notified(owed[0][0], owed[0][1], tmp_path)
    assert drug_info.daily_notices(("20260930", 190), tmp_path) == []
    gateway(refuse="gateway_unconfigured")
    assert drug_info.daily_notices(("20261013", 200), tmp_path) == []
    assert drug_info.status(tmp_path)["listing"]["status"] == "unknown"


def test_update_check_hook_is_best_effort_and_opt_out(monkeypatch):
    sent, marked = [], []
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda text, use_run_lock=False, **_notice_fields: sent.append(text) or len(sent) == 1)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(drug_info, "daily_notices", lambda local, root=None: [("a", 1, "one"), ("b", 2, "two")])
    monkeypatch.setattr(drug_info, "mark_notified", lambda k, v, root=None: marked.append(k))
    import import_drug_master
    monkeypatch.setattr(import_drug_master, "load_bundled_master", lambda directory=None: ({"edition": "20261013", "rows": 1}, None))
    mcs_update._drug_info_notices({})
    assert sent == ["one", "two"] and marked == ["a"]  # second enqueue failed -> not marked
    sent.clear()
    mcs_update._drug_info_notices({"drug_info_watch": False})
    assert sent == []

    def boom(local, root=None):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(drug_info, "daily_notices", boom)
    mcs_update._drug_info_notices({})  # never raises into the update check


GOOD_ITEM = {"name": "架空アオ錠１０ｍｇ「架空」", "code": "900000001", "url": PDF.format("A"),
             "sha256": "a" * 64, "revision": None, "gen": "g1", "added_at": 1.0, "last_checked": 0,
             "last": None}


@pytest.mark.parametrize("state", [
    "{not json",
    '{"inserts": {}}',
    '{"inserts": [{"name": "x"}]}',
    '{"inserts": [], "notified": []}',
    '{"inserts": [], "listing": "ok"}',
    '{"inserts": [ITEM_NAN]}',
])
def test_damaged_watch_state_is_refused_untouched_before_any_request(gateway, tmp_path, state):
    path = tmp_path / "watch.json"
    path.write_text(state.replace("ITEM_NAN", '{"name": "n", "code": "c", "url": "u", "sha256": "s", "gen": "g",'
                                              ' "added_at": NaN, "last_checked": 0, "last": null}'))
    before = path.read_bytes()
    fake = gateway(bodies={drug_info.MENU: MENU})
    for action in (lambda: drug_info.formulary_check(root=tmp_path),
                   lambda: drug_info.daily_notices(("20261013", 200), tmp_path),
                   lambda: drug_info.mark_notified("listing", "k", tmp_path),
                   lambda: drug_info.formulary_ack("n", "a" * 64, tmp_path),
                   lambda: drug_info.status(tmp_path),
                   lambda: drug_info.formulary_add(["架空アオ錠１０ｍｇ「架空」"], ROWS, root=tmp_path)):
        with pytest.raises(drug_info.DrugInfoError, match="watch_state_malformed"):
            action()
    assert path.read_bytes() == before and fake.sent == []


def test_older_saved_state_without_notified_still_loads(tmp_path):
    (tmp_path / "watch.json").write_text(json.dumps({"inserts": [GOOD_ITEM]}, ensure_ascii=False))
    assert drug_info.status(tmp_path)["inserts"][0]["name"] == GOOD_ITEM["name"]


@pytest.mark.parametrize("answer", [{}, {"data": None}, {"data": {}}, {"data": {"results": {}}},
                                    {"data": {"results": ["https://www.pmda.go.jp/x"]}}])
def test_malformed_search_answer_is_an_error_not_zero_results(monkeypatch, answer):
    monkeypatch.setattr(drug_info, "call", lambda name, args, timeout=75.0: answer)
    with pytest.raises(drug_info.DrugInfoError, match="search_response_invalid"):
        drug_info.search("架空", drug_info.PMDA)
    assert drug_info.find_insert(ROWS[0])["status"] == "error"


def test_empty_search_answer_is_zero_results(monkeypatch):
    monkeypatch.setattr(drug_info, "call", lambda name, args, timeout=75.0: {"data": {"results": []}})
    assert drug_info.search("架空", drug_info.PMDA) == []
    assert drug_info.find_insert(ROWS[0])["status"] == "not_found"


def test_excerpt_offsets_are_exact():
    text = "  " + "x" * 3000 + " 禁忌 " + "y" * 800
    found = drug_info.excerpts(text, ["禁忌"])
    assert "禁忌" in found[0]["text"]
    assert text[found[0]["offset"]:found[0]["offset"] + len(found[0]["text"])] == found[0]["text"]


# --- independent review IR1-IR6 (fixed 3057bbeb) ------------------------------


TOKEN = "t" * 64
SENTINEL = "SENTINEL-raw-error-\x1b[31m-text"


class Wire:
    """Stands in for bounded_http: records each request and answers from a script."""
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, _bounded_http, name, arguments, timeout, token, deadline=None):
        self.calls.append({"name": name, "token": token, "deadline": deadline})
        status, body = self.answers.pop(0)
        return status, {}, json.dumps(body).encode()


def _token_file(tmp_path, monkeypatch, content=TOKEN + "\n", mode=0o600):
    path = tmp_path / "local-token"
    if content is not None:
        path.write_text(content)
        path.chmod(mode)
    monkeypatch.setattr(drug_info, "TOKEN_PATH", path)
    return path


def test_valid_private_token_is_sent(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    wire = Wire([(200, {"data": {"results": []}})])
    monkeypatch.setattr(drug_info, "_request", wire)
    assert drug_info.search("架空", drug_info.PMDA) == []
    assert [c["token"] for c in wire.calls] == [TOKEN]


@pytest.mark.parametrize("case", ["missing", "symlink", "fifo", "directory", "oversize", "loose_mode",
                                  "foreign_uid", "not_ascii", "short"])
def test_token_state_controls_available_transport(tmp_path, monkeypatch, case):
    if case == "missing":
        _token_file(tmp_path, monkeypatch, content=None)
    elif case == "symlink":
        real = tmp_path / "real"
        real.write_text(TOKEN)
        real.chmod(0o600)
        link = tmp_path / "local-token"
        link.symlink_to(real)
        monkeypatch.setattr(drug_info, "TOKEN_PATH", link)
    elif case == "fifo":
        fifo = tmp_path / "local-token"
        os.mkfifo(fifo, 0o600)
        monkeypatch.setattr(drug_info, "TOKEN_PATH", fifo)  # a blocking open would hang this test
    elif case == "directory":
        folder = tmp_path / "local-token"
        folder.mkdir(mode=0o700)
        monkeypatch.setattr(drug_info, "TOKEN_PATH", folder)
    elif case == "oversize":
        _token_file(tmp_path, monkeypatch, content="t" * 2000)
    elif case == "loose_mode":
        _token_file(tmp_path, monkeypatch, mode=0o644)
    elif case == "foreign_uid":
        _token_file(tmp_path, monkeypatch)
        uid = os.getuid()
        monkeypatch.setattr(safe_io.os, "getuid", lambda: uid + 1)
    elif case == "not_ascii":
        _token_file(tmp_path, monkeypatch, content="ｔ" * 40)
    else:
        _token_file(tmp_path, monkeypatch, content="short-token")
    wire = Wire([])
    monkeypatch.setattr(drug_info, "_request", wire)
    import bounded_http
    monkeypatch.setattr(bounded_http, "public_mcp_request",
                        lambda *args, **kwargs: pytest.fail("unexpected external fallback"))
    with pytest.raises(drug_info.DrugInfoError, match="^gateway_unconfigured$"):
        drug_info.search("架空", drug_info.PMDA)
    assert wire.calls == []


@pytest.mark.parametrize("answer", [
    (401, {"error": SENTINEL}),
    (400, {"error": "shared_free_quota_exhausted; " + SENTINEL}),
    (200, {"results": [{"url": PDF.format("A"), "error": SENTINEL}]}),
    (200, {"results": [{"url": PDF.format("A"), "result": {"data": {"results": [], "errors": [{"error": SENTINEL}]}}}]}),
])
def test_gateway_and_provider_error_text_never_leaves_as_is(tmp_path, monkeypatch, capsys, answer):
    _token_file(tmp_path, monkeypatch)
    monkeypatch.setattr(drug_info, "STATE_DIR", tmp_path / "state")
    with pytest.raises(drug_info.DrugInfoError) as caught:
        monkeypatch.setattr(drug_info, "_request", Wire([answer]))
        drug_info.fetch_page(PDF.format("A"))
    assert str(caught.value) in drug_info.KNOWN_ERRORS | {"gateway_refused", "fetch_error"}
    # the same answers through the user-facing paths
    monkeypatch.setattr(drug_info, "_request", Wire([answer]))
    assert mcs_drug.main(["info-guideline", "高血圧"]) == 1
    monkeypatch.setattr(drug_info, "_request", Wire([answer]))
    status, detail = drug_info.check_listing(("20261013", 200))
    drug_info.update(lambda data: data.__setitem__("listing", {"status": status, "detail": detail, "at": 1.0}))
    out = capsys.readouterr()
    stored = (tmp_path / "state" / "watch.json").read_text()
    for text in (out.out, out.err, detail, stored, json.dumps(drug_info.status())):
        assert "SENTINEL" not in text and "\x1b" not in text


def test_quota_code_still_stops_the_formulary_run(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    wire = Wire([(400, {"error": "shared_free_quota_exhausted; wait for reset"})])
    monkeypatch.setattr(drug_info, "_request", wire)
    report = drug_info.formulary_add(["架空アオ錠１０ｍｇ「架空」", "架空アオ錠２０ｍｇ「架空」"], ROWS,
                                     root=tmp_path)
    assert [r["status"] for r in report] == ["error", "deferred"]
    assert report[0]["reason"] == "shared_free_quota_exhausted" and len(wire.calls) == 1


def test_unauthorized_gateway_stops_the_run_and_keeps_state(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    wire = Wire([(401, {"error": "unauthorized"})])
    monkeypatch.setattr(drug_info, "_request", wire)
    report = drug_info.formulary_add(["架空アオ錠１０ｍｇ「架空」", "架空アオ錠２０ｍｇ「架空」"], ROWS,
                                     root=tmp_path)
    assert [(r["status"], r.get("reason")) for r in report] == [("error", "unauthorized"), ("deferred", "stopped")]
    assert len(wire.calls) == 1
    items = [dict(GOOD_ITEM, url=PDF.format(c), gen=c, last_checked=i) for i, c in enumerate("AB")]
    (tmp_path / "watch.json").write_text(json.dumps({"inserts": items}, ensure_ascii=False))
    wire = Wire([(401, {"error": "unauthorized"}), (401, {"error": "unauthorized"})])
    monkeypatch.setattr(drug_info, "_request", wire)
    results = drug_info.formulary_check(limit=2, root=tmp_path)
    assert [(item["url"], state) for item, state, _r in results] == [(PDF.format("A"), "error")]
    assert len(wire.calls) == 1  # the second item is not tried with the same rejected token
    saved = json.loads((tmp_path / "watch.json").read_text())["inserts"]
    assert [x["sha256"] for x in saved] == ["a" * 64, "a" * 64] and saved[1]["last_checked"] == 1
    wire = Wire([(401, {"error": "unauthorized"}), (401, {"error": "unauthorized"})])
    monkeypatch.setattr(drug_info, "_request", wire)
    assert drug_info.daily_notices(("20261013", 200), tmp_path) == []  # nothing owed, nothing marked
    assert drug_info.status(tmp_path)["listing"]["status"] == "unknown"


def _open_fds():
    return set(os.listdir("/dev/fd"))


@pytest.mark.parametrize("fault", ["fdopen", "fdopen_after_close", "fsync", "replace"])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt, SystemExit])
def test_failed_save_keeps_old_state_and_leaks_nothing(tmp_path, monkeypatch, fault, error):
    path = tmp_path / "watch.json"
    path.write_text(json.dumps({"inserts": [GOOD_ITEM], "notified": {}}, ensure_ascii=False))
    path.chmod(0o600)
    before, fds = path.read_bytes(), _open_fds()
    real_fdopen = os.fdopen

    def boom(*args, **kwargs):
        raise error("synthetic")

    def close_then_raise(fd, *args, **kwargs):
        os.close(fd)
        raise error("synthetic")
    target = {"fdopen": ("fdopen", boom), "fdopen_after_close": ("fdopen", close_then_raise),
              "fsync": ("fsync", boom), "replace": ("replace", boom)}[fault]
    monkeypatch.setattr(safe_io.os, target[0], target[1])
    try:
        with pytest.raises(error):
            drug_info.mark_notified("listing", "k", tmp_path)
    finally:
        monkeypatch.setattr(safe_io.os, "fdopen", real_fdopen)
    assert path.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".drug-info-")]
    assert _open_fds() <= fds  # no descriptor left behind


def test_save_orders_file_fsync_replace_then_directory_fsync(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace, real_dir = os.fsync, os.replace, safe_io.fsync_dir
    monkeypatch.setattr(safe_io.os, "fsync", lambda fd: order.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(safe_io.os, "replace", lambda a, b: order.append("replace") or real_replace(a, b))
    monkeypatch.setattr(safe_io, "fsync_dir", lambda p, **kw: order.append("dir") or real_dir(p, **kw))
    drug_info.mark_notified("listing", "k", tmp_path)
    assert order == ["fsync", "replace", "dir", "fsync"]  # file, rename, then the directory itself
    assert oct((tmp_path / "watch.json").stat().st_mode & 0o777) == "0o600"


def test_directory_fsync_failure_is_not_reported_as_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(safe_io, "fsync_dir", lambda p, **kw: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        drug_info.mark_notified("listing", "k", tmp_path)


@pytest.mark.parametrize("value", ["1" + "0" * 400, "-1" + "0" * 400, "true", "Infinity", "NaN"])
def test_unrepresentable_times_are_refused_untouched(gateway, tmp_path, value):
    item = json.dumps(GOOD_ITEM, ensure_ascii=False).replace('"added_at": 1.0', '"added_at": ' + value)
    path = tmp_path / "watch.json"
    path.write_text('{"inserts": [' + item + ']}')
    before = path.read_bytes()
    fake = gateway(bodies={drug_info.MENU: MENU})
    for action in (lambda: drug_info.status(tmp_path), lambda: drug_info.formulary_check(root=tmp_path),
                   lambda: drug_info.daily_notices(("20261013", 200), tmp_path),
                   lambda: drug_info.mark_notified("listing", "k", tmp_path)):
        with pytest.raises(drug_info.DrugInfoError, match="watch_state_malformed"):
            action()
    assert path.read_bytes() == before and fake.sent == []


def test_watch_budget_bounds_every_request_it_starts(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch)
    items = [dict(GOOD_ITEM, url=PDF.format(c), gen=c, last_checked=i) for i, c in enumerate("ABC")]
    (tmp_path / "watch.json").write_text(json.dumps({"inserts": items}, ensure_ascii=False))
    clock = [1000.0]
    monkeypatch.setattr(drug_info.time, "monotonic", lambda: clock[0])
    body = {"results": [{"url": "u", "result": {"retrieved_at": 1, "data": {"results": [
        {"text": BODY}], "errors": []}}}]}
    wire = Wire([(200, body)] * 3)

    def timed(*args, **kwargs):
        answer = wire(*args, **kwargs)
        clock[0] += 100  # each fetch takes 100 s of a 150 s budget
        return answer
    monkeypatch.setattr(drug_info, "_request", timed)
    results = drug_info.formulary_check(limit=3, root=tmp_path, budget_seconds=150)
    assert [c["deadline"] for c in wire.calls] == [1150.0, 1150.0]  # the second may use only what is left
    assert [item["url"] for item, _state, _r in results] == [PDF.format("A"), PDF.format("B")]
    clock[0] = 2000.0
    with pytest.raises(drug_info.DrugInfoError, match="^budget_exhausted$"):
        drug_info.call("search", {}, deadline=1999.0)
    assert len(wire.calls) == 2  # nothing starts once the budget is spent
