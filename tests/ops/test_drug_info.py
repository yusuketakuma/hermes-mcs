"""Public drug-information contracts over fictional rows and a stubbed local gateway; no network."""
import pytest

import drug_info
import mcs_update

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

    def __call__(self, name, args, timeout=75.0):
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
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda text, use_run_lock=False: sent.append(text) or len(sent) == 1)
    monkeypatch.setattr(drug_info, "daily_notices", lambda local, root=None: [("a", 1, "one"), ("b", 2, "two")])
    monkeypatch.setattr(drug_info, "mark_notified", lambda k, v, root=None: marked.append(k))
    import mcs_drug
    monkeypatch.setattr(mcs_drug, "load_bundled_master", lambda directory=None: ({"edition": "20261013", "rows": 1}, None))
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
    import json
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
