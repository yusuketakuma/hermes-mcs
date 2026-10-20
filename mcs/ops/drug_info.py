#!/usr/bin/env python3
"""公開医薬品情報（PMDA添付文書・Mindsガイドライン・公式マスター一覧）を同梱マスター起点で照会する。

All Web access goes through the operator's local free TinyFish gateway
(loopback ``/call`` with its local bearer token) via ``bounded_http``: no proxy,
no redirect, bounded size and time. Only names that exactly match the bundled
public MHLW master, and general disease keywords, are ever sent. Patient
messages, attributes, the ledger and private dictionaries are never read or
sent, and nothing here is triggered by an MCS message. Results are unreviewed
reference material, not clinical decisions.

The updater imports this module lazily; keep Python 3.10 grammar.
"""
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _mcs_path  # noqa: E402,F401
from mcs_util import HOME  # noqa: E402
from safe_io import atomic_write, read_private_bytes  # noqa: E402
from stats_math import finite_number  # noqa: E402

GATEWAY_URL = "http://127.0.0.1:3149/call"
TOKEN_PATH = Path.home() / ".local/state/tinyfish-free/local-token"
STATE_DIR = Path(HOME) / "data" / "drug-info"
MENU = "https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/"
MENU_DOMAIN = "shinryohoshu.mhlw.go.jp"
PMDA = "pmda.go.jp"
MINDS = "minds.jcqhc.or.jp"
INSERT_URL = re.compile(r"https://www\.pmda\.go\.jp/PmdaSearch/iyakuDetail/ResultDataSetPDF/[0-9A-Za-z_]+")
DEFAULT_TERMS = ("禁忌", "相互作用", "用法及び用量", "重要な基本的注意")
REVISION = re.compile(r"([0-9]{4})年\s*([0-9]{1,2})月改訂\s*[（(]\s*第\s*([0-9]+)\s*版\s*[）)]")
# Any medicine-master table row (name, optional size, cell boundary); header notes are not rows.
ANY_ROW = re.compile(r"医薬品マスター[ \t　]*(?:（[^）\n|]{0,20}）)?[ \t　]*\|")
ROW = re.compile(r"医薬品マスター[^|\n]{0,40}\|\s*([0-9][0-9,]*)件\s*\|\s*令和\s*([0-9]{1,2})年\s*([0-9]{1,2})月\s*([0-9]{1,2})日")
STOP = ("quota", "cooldown", "api_key", "http_4", "gateway", "keychain", "unauthorized")  # same for every item
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{32,256}")  # secrets.token_urlsafe(48) from the gateway installer
# Codes the local gateway can return; any other text is replaced by a generic code and never kept.
KNOWN_ERRORS = frozenset({
    "after_date_later_than_before_date", "batch_response_too_large", "browser_origins_refused",
    "clock_moved_backwards", "conditional_fetch_requires_single_url",
    "date_range_not_combinable_with_recency_or_research_paper", "domain_both_included_and_excluded",
    "fetch_failed", "gateway_busy", "gateway_failed", "gateway_refused", "host_refused",
    "invalid_arguments", "invalid_request", "invalid_search_filter", "invalid_selector",
    "invalid_validator", "keychain_reader_unavailable", "keychain_unavailable",
    "local_gateway_unavailable", "only_search_and_fetch_content_allowed", "paid_or_unknown_api_refused",
    "pub_year_min_greater_than_max", "pub_year_requires_research_paper", "public_https_url_required",
    "public_input_required", "request_too_large", "research_paper_recency_not_supported",
    "sensitive_input_refused", "shared_free_quota_exhausted", "tinyfish_api_key_required",
    "unauthorized", "upstream_cooldown", "upstream_response_too_large", "upstream_unavailable",
})
MAX_TEXT = 16000


class DrugInfoError(ValueError):
    """Safe to display: never contains request data or response bodies."""


def safe_code(raw, default):
    """Fixed local code for a gateway/provider error text; the text itself is never kept or shown."""
    code = raw.split(";", 1)[0].strip() if isinstance(raw, str) else ""
    if code in KNOWN_ERRORS or re.fullmatch(r"tinyfish_http_[0-9]{3}", code):
        return code
    return default


def norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


# --- local gateway -------------------------------------------------------

def _token():
    """Local bearer token: an owner-only regular file of the expected shape, read without waiting."""
    try:
        token = read_private_bytes(TOKEN_PATH, 1024).decode("ascii").strip()
    except (OSError, ValueError):  # link, FIFO, foreign owner, loose mode, oversize or not ASCII
        raise DrugInfoError("gateway_unconfigured") from None
    if not TOKEN_SHAPE.fullmatch(token):
        raise DrugInfoError("gateway_unconfigured")
    return token


def call(name, arguments, timeout=75.0, deadline=None):
    """One request to the local gateway; returns its JSON object or raises DrugInfoError.

    ``deadline`` is a time.monotonic() bound: no request starts at or after it."""
    import bounded_http
    token = _token()
    if deadline is not None and deadline - time.monotonic() <= 0:
        raise DrugInfoError("budget_exhausted")
    try:
        status, _headers, raw = _request(bounded_http, name, arguments, timeout, token, deadline)
    except (OSError, TimeoutError, ValueError):
        raise DrugInfoError("gateway_unavailable") from None
    if len(raw) > bounded_http.MAX_RESPONSE_BYTES:
        raise DrugInfoError("gateway_response_too_large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise DrugInfoError("gateway_response_invalid") from None
    if not isinstance(data, dict):
        raise DrugInfoError("gateway_response_invalid")
    if status != 200:
        raise DrugInfoError(safe_code(data.get("error"), "gateway_refused"))
    return data


def _request(bounded_http, name, arguments, timeout, token, deadline=None):
    return bounded_http.bounded_http_request(
        GATEWAY_URL, "POST", {"name": name, "arguments": arguments}, timeout,
        api_key=token, deadline=deadline, allowed_endpoints=(GATEWAY_URL,))


def fetch_page(url, *, fresh=False, validators=None, deadline=None):
    """Fetch one public URL; returns the provider page dict and the envelope metadata."""
    args = {"urls": [url], "public_only": True, "cache_policy": "docs", "force_fresh": fresh,
            "include_etag_and_last_modified": True}
    args.update(validators or {})
    data = call("fetch_content", args, deadline=deadline)
    try:
        row = data["results"][0]
        if "error" in row:
            raise DrugInfoError(safe_code(row["error"], "fetch_error"))
        envelope = row["result"]
        page_data = envelope["data"]
        if page_data.get("errors"):
            raise DrugInfoError("fetch_error")  # provider text is not ours to repeat
        page = page_data["results"][0]
    except (KeyError, IndexError, TypeError, AttributeError):
        raise DrugInfoError("fetch_response_invalid") from None
    if not isinstance(page, dict):
        raise DrugInfoError("fetch_response_invalid")
    return page, {"retrieved_at": envelope.get("retrieved_at"), "cache_hit": envelope.get("cache_hit")}


def search(query, domain):
    """Result rows; an empty list only when the gateway answered with no results."""
    data = call("search", {"query": query, "include_domains": [domain], "public_only": True,
                           "cache_policy": "docs"})
    body = data.get("data")
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        raise DrugInfoError("search_response_invalid")  # never a silent "0 results"
    return results


def excerpts(text, terms, limit=5, width=1200):
    """Windows around matches inside a paragraph; ``offset`` is the exact start of each slice."""
    if not terms:
        return []
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.I)
    seps = [(m.start(), m.end()) for m in re.finditer(r"\n\s*\n", text)]
    found, covered = [], 0
    for m in pattern.finditer(text):
        if m.start() < covered:
            continue
        before = [end for start, end in seps if start <= m.start()]
        after = [start for start, _end in seps if start > m.start()]
        lo = max(covered, before[-1] if before else 0)
        hi = after[0] if after else len(text)
        a = max(lo, m.start() - (width - (m.end() - m.start())) // 2)
        b = min(hi, a + width)
        a = max(lo, b - width)
        while a < m.start() and text[a].isspace():
            a += 1
        while b > m.end() and text[b - 1].isspace():
            b -= 1
        found.append({"offset": a, "text": text[a:b]})
        covered = b
        if len(found) == limit:
            break
    return found


# --- bundled master ------------------------------------------------------

def master_rows():
    """Validated rows of the converter's bundled edition (read-only, via the existing importer)."""
    import import_drug_master as importer
    manifest, archive = importer.load_bundled_master()
    pin = {"schema": importer.PIN_SCHEMA, "layout": importer.LAYOUT, "edition": importer.EDITION,
           "sha256": manifest["archive_sha256"], "member": manifest["member"],
           "as_of": max(date.today(), importer.EDITION_DATE).isoformat()}
    catalog = importer.catalog(archive, pin)
    return manifest, list(catalog["rows"].values())


def resolve(name, rows):
    """Exact NFKC match on the sales name or the standard generic label; (entries, local suggestions)."""
    key = norm(name)
    if not 2 <= len(key) <= 80:
        return [], []
    hits = [r for r in rows if key in (norm(r["name"]), norm(r.get("general_name")))]
    entries = [{"name": r["name"], "code": r["medicine"], "generic": r.get("general_name") or None,
                "abolished": r.get("abolished_on") not in ("0", "99999999")} for r in hits]
    suggestions = [] if entries else sorted({r["name"] for r in rows if key in norm(r["name"])})[:5]
    return entries, suggestions


# --- official listing check ----------------------------------------------

def listed_editions(text):
    found = []
    for row in ANY_ROW.finditer(text or ""):
        parsed = ROW.match(text, row.start())
        if parsed is None:
            return None  # an unreadable row might be the newest edition
        count, year, month, day = parsed.groups()
        try:
            when = date(2018 + int(year), int(month), int(day))  # 令和 n 年 = 2018 + n
        except ValueError:
            return None
        found.append((when.strftime("%Y%m%d"), int(count.replace(",", ""))))
    return found


def compare_listing(text, local):
    """(status, detail): match | newer | mismatch | unknown. Unknown is never "no update"."""
    if local is None:
        return "unknown", "同梱 manifest を読めない"
    editions = listed_editions(text)
    if editions is None:
        return "unknown", "公式ページに読み取れない医薬品マスターの行がある"
    if not editions:
        return "unknown", "公式ページから医薬品マスターの行を読み取れない"
    latest = max(editions)
    if latest[0] > local[0]:
        return "newer", "公式 {}（{:,}件）が同梱 {}（{:,}件）より新しい".format(latest[0], latest[1], local[0], local[1])
    if latest == local:
        return "match", "公式の最新 {}（{:,}件）と同梱版が一致".format(latest[0], latest[1])
    return "mismatch", "公式の最新 {}（{:,}件）と同梱 {}（{:,}件）が対応しない".format(latest[0], latest[1], local[0], local[1])


def check_listing(local):
    try:
        page, _meta = fetch_page(MENU, fresh=True)
    except DrugInfoError as error:
        return "unknown", "公式ページを取得できない: " + str(error)
    text = page.get("text")
    if not isinstance(text, str) or page.get("not_modified"):
        return "unknown", "本文が不完全"
    return compare_listing(text, local)


# --- package inserts and guidelines --------------------------------------

def revision(text):
    m = REVISION.search(text or "")
    return "{}年{}月改訂（第{}版）".format(m.group(1), int(m.group(2)), m.group(3)) if m else None


def insert_candidates(results):
    urls = []
    for item in results:
        url = item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str) and INSERT_URL.fullmatch(url) and url not in urls:
            urls.append(url)
    return urls[:3]


def find_insert(entry, terms=DEFAULT_TERMS):
    """One PMDA search, then up to 3 package-insert PDFs until one names this exact product."""
    try:
        candidates = insert_candidates(search(norm(entry["name"]) + " 添付文書", PMDA))
    except DrugInfoError as error:
        return {"status": "error", "reason": str(error)}
    if not candidates:
        return {"status": "not_found", "reason": "no package-insert PDF in PMDA search results"}
    checked = []
    for url in candidates:
        try:
            page, meta = fetch_page(url)
        except DrugInfoError as error:
            if any(k in str(error) for k in STOP):
                return {"status": "error", "reason": str(error)}
            checked.append({"url": url, "result": str(error)})
            continue
        text = page.get("text")
        if not isinstance(text, str) or norm(entry["name"]) not in norm(text):
            checked.append({"url": url, "result": "product name not in document"})
            continue
        return {"status": "found", "url": url, "revision": revision(text),
                "sha256": hashlib.sha256(text.encode()).hexdigest(), "etag": page.get("etag"),
                "retrieved_at": meta["retrieved_at"], "excerpts": excerpts(text, list(terms)),
                "text_truncated": len(text) > MAX_TEXT, "checked_before": checked,
                "excerpts_are_partial": True, "read_the_source_document": True, "assessment": "unreviewed"}
    return {"status": "unconfirmed", "reason": "no candidate PDF names this product", "checked": checked}


def disease_term(value):
    value = (value or "").strip()
    if not 2 <= len(value) <= 40 or re.search(r"[0-9０-９@＠/:：]", value):
        raise DrugInfoError("disease name: 2-40 characters, no digits, IDs, dates or addresses")
    return value


def guideline(disease):
    results = search(disease_term(disease) + " 診療ガイドライン", MINDS)
    out = []
    for item in results:
        url = item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str) and re.match(r"https://(www\.)?" + re.escape(MINDS) + r"/", url):
            out.append({"url": url, "title": str(item.get("title") or "")[:200]})
    return {"disease": disease, "results": out[:5], "assessment": "unreviewed",
            "note": "検索結果の一覧のみ。ガイドライン本体を読んでから使う"}


# --- adopted-product watch list (private state under data/drug-info) -----

def _state_path(name, root=None):
    return Path(root or STATE_DIR) / name


@contextmanager
def _locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


_LAST = (None, "error", "changed", "unchanged", "acknowledged")


def _valid_item(item):
    return (isinstance(item, dict)
            and all(isinstance(item.get(k), str) and item[k] for k in ("name", "code", "url", "sha256", "gen"))
            and all(finite_number(item.get(k)) for k in ("added_at", "last_checked"))
            and item.get("last") in _LAST
            and all(item.get(k) is None or isinstance(item[k], str)
                    for k in ("revision", "etag", "observed_sha256", "observed_revision")))


def _load(path):
    """A damaged file is refused as-is (never rewritten as an empty list)."""
    if not path.exists():
        return {"inserts": [], "notified": {}}
    try:
        data = json.loads(path.read_text())
    except ValueError:  # includes UnicodeDecodeError
        raise DrugInfoError("watch_state_malformed") from None
    if (not isinstance(data, dict) or not isinstance(data.get("inserts"), list)
            or not all(map(_valid_item, data["inserts"]))
            or not isinstance(data.setdefault("notified", {}), dict)
            or not isinstance(data.get("listing", {}), dict)):
        raise DrugInfoError("watch_state_malformed")
    return data


def _save(data, path):
    atomic_write(str(path), lambda stream: json.dump(data, stream, ensure_ascii=False, indent=1),
                 0o600, tmp_prefix=".drug-info-", tmp_suffix=".json", makedirs=False)


def update(change, root=None):
    """Whole read-modify-write under one lock."""
    path = _state_path("watch.json", root)
    with _locked(path):
        data = _load(path)
        result = change(data)
        _save(data, path)
        return result


def load_watch(root=None):
    return _load(_state_path("watch.json", root))


def register(result, entry, root=None):
    """Add an insert unless that URL is already watched (one insert often covers several strengths)."""
    def change(data):
        existing = next((x for x in data["inserts"] if x["url"] == result["url"]), None)
        if existing is not None:
            return "already_watched_url"
        data["inserts"].append({"name": entry["name"], "code": entry["code"], "url": result["url"],
                                "sha256": result["sha256"], "revision": result["revision"],
                                "etag": result.get("etag"), "gen": uuid.uuid4().hex,
                                "added_at": time.time(), "last_checked": 0, "last": None})
        return "registered"
    return update(change, root)


def formulary_add(names, rows, limit=10, root=None):
    """Resolve every line locally first; unknown/ambiguous lines are never sent."""
    lines = [n.strip() for n in names if n.strip() and not n.strip().startswith("#")]
    resolved = [(name,) + tuple(resolve(name, rows)) for name in lines]
    watched = {x["name"] for x in load_watch(root)["inserts"]}
    report, done, stopped = [], 0, False
    for name, entries, suggestions in resolved:
        if len(entries) != 1:
            report.append({"name": name, "status": "unresolved" if not entries else "ambiguous",
                           "suggestions": suggestions, "sent": False})
            continue
        entry = entries[0]
        if entry["name"] in watched:
            report.append({"name": name, "status": "already_watched", "sent": False})
            continue
        if stopped or done >= limit:
            report.append({"name": name, "status": "deferred", "sent": False,
                           "reason": "stopped" if stopped else "per-run limit"})
            continue
        done += 1
        result = find_insert(entry)
        if result["status"] != "found":
            report.append(dict(result, name=name, sent=True))
            stopped = any(k in result.get("reason", "") for k in STOP)
            continue
        status = register(result, entry, root)
        watched.add(entry["name"])
        report.append({"name": name, "status": status, "revision": result["revision"], "sent": True})
    return report


def formulary_check(limit=3, root=None, budget_seconds=150):
    """Re-check the least recently checked inserts (ETag when available); merge by URL and generation."""
    snapshot = load_watch(root)
    due = sorted(snapshot["inserts"], key=lambda x: x["last_checked"])[:limit]
    observations = []
    deadline = time.monotonic() + budget_seconds  # bounds each request, not only its start
    for item in due:
        if time.monotonic() >= deadline:
            break
        validators = {"if_none_match": item["etag"]} if item.get("etag") else None
        try:
            page, _meta = fetch_page(item["url"], fresh=True, validators=validators, deadline=deadline)
        except DrugInfoError as error:
            if str(error) == "budget_exhausted":
                break
            observations.append({"url": item["url"], "gen": item["gen"], "state": "error", "reason": str(error)})
            if any(k in str(error) for k in STOP):
                break
            continue
        if page.get("not_modified") is True:
            observations.append({"url": item["url"], "gen": item["gen"], "state": "not_modified"})
            continue
        text = page.get("text")
        if not isinstance(text, str) or not text.strip():
            observations.append({"url": item["url"], "gen": item["gen"], "state": "error", "reason": "empty_content"})
            continue
        observations.append({"url": item["url"], "gen": item["gen"], "state": "fetched",
                             "sha256": hashlib.sha256(text.encode()).hexdigest(),
                             "revision": revision(text), "etag": page.get("etag")})

    def merge(data):
        by_url = {x["url"]: x for x in data["inserts"]}
        results = []
        for o in observations:
            item = by_url.get(o["url"])
            if item is None or item.get("gen") != o["gen"]:
                continue  # removed or re-registered while checking
            item["last_checked"] = time.time()
            if o["state"] == "error":
                item.pop("etag", None)  # a failed conditional fetch must not repeat every day
                item["last"] = "error"
                results.append((item, "error", o["reason"]))
                continue
            if o["state"] == "fetched":
                item["observed_sha256"], item["observed_revision"] = o["sha256"], o["revision"]
                if o.get("etag"):
                    item["etag"] = o["etag"]
                else:
                    item.pop("etag", None)
            observed = item.get("observed_sha256")
            item["last"] = "changed" if observed and observed != item["sha256"] else "unchanged"
            results.append((item, item["last"], None))
        return results
    return update(merge, root)


def formulary_ack(name, sha256, root=None):
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise DrugInfoError("sha256 must be lowercase hex")

    def change(data):
        for item in data["inserts"]:
            if item["name"] == name:
                item["sha256"], item["last"] = sha256, "acknowledged"
                if item.get("observed_revision"):
                    item["revision"] = item["observed_revision"]
                return item
        raise DrugInfoError("product is not on the watch list")
    return update(change, root)


# --- daily hook for mcs_update check -------------------------------------

def daily_notices(local, root=None):
    """Notices owed today as (key, value, text); silent on unknown/errors, which are recorded in state.

    The caller marks each one with mark_notified() only after it was enqueued,
    so a busy tick or failed enqueue retries on the next check."""
    watched = load_watch(root)["inserts"]  # a damaged state stops here, before any request
    status, detail = check_listing(local)
    changed = []
    if watched:
        changed = [item for item, state, _r in formulary_check(root=root) if state == "changed"]

    def record(data):
        owed = []
        data["listing"] = {"status": status, "detail": detail, "at": time.time()}
        key = status + ":" + detail
        if status in ("newer", "mismatch") and data["notified"].get("listing") != key:
            owed.append(("listing", key, "[MCS] 医薬品マスター: " + detail
                         + "。release-mcs の手順で公式原本を確認してください"))
        for item in changed:
            note_key = "insert:" + item["url"]
            if data["notified"].get(note_key) != item.get("observed_sha256"):
                revised = item.get("observed_revision") or "改訂情報不明"
                owed.append((note_key, item.get("observed_sha256"),
                             "[MCS] 添付文書の本文が変わりました: " + item["name"] + "（" + revised + "）。"
                             "読み直してから mcs drug formulary-ack で受け入れてください"))
        return owed
    return update(record, root)


def mark_notified(key, value, root=None):
    def change(data):
        data["notified"][key] = value
    update(change, root)


def status(root=None):
    data = load_watch(root)
    return {"listing": data.get("listing"), "inserts": [
        {k: x.get(k) for k in ("name", "revision", "observed_revision", "last", "last_checked")}
        for x in data["inserts"]], "read_only": True}
