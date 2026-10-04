"""Opt-in cross-project mentioned/bookmarked acquisition and storage-only capture.

Public contracts: OLVJNCFG (mentioned), FUWQG6Z4 (bookmarked), KBHQOTFV
(shared timestamp pager); see docs/roadmap/mcs-api-survey.md and stamps.md.
Only project.id is authoritative; redundant scope fields must agree.
keep_read_status is grounded for bookmark *exact refresh*, not these lists,
so it is not invented here. GET/no_extend_session does not prove unread or
session preservation. Nonempty shape, continuation and side effects still
require owner real-API acceptance before scheduler/publication integration.

No login, source-body upsert, metadata-current mutation, notification,
read mark, completion or unread snapshot/floor advancement. Parent callers
own the writer lock and publication gate. Capture retains normalized identity,
body availability and optional metadata, not bodies, names or attachment URLs.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import TypedDict

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import _mcs_path  # noqa: F401

from mcs_adapter import (
    API, MCSError, Message, SchemaError, SessionExpired,
    _message_metadata, _norm_message, _valid_id,
)

ARTIFACT_KIND = "cross_list_v1"
DATASETS = ("mentioned", "bookmarked")
JSON = str | int | float | bool | None | list["JSON"] | dict[str, "JSON"]


class CapturedRow(TypedDict):
    project_id: int
    message_id: int
    parent_id: int | None
    posted_at: str
    body_state: str
    is_unread: bool | None
    metadata: dict[str, JSON]
    metadata_errors: list[str]


class CapturedList(TypedDict):
    contract: str
    dataset: str
    unread_only: bool
    attempted_at: float
    complete: bool
    reason: str | None
    http_status: int | None
    timestamp: int | None
    pages: int
    rows: list[CapturedRow]


@dataclass(frozen=True)
class CrossListEntry:
    message: Message
    is_unread: bool | None


@dataclass
class CrossListFetch:
    entries: list[CrossListEntry] = field(default_factory=list)
    complete: bool = False
    reason: str | None = None
    http_status: int | None = None
    pages: int = 0
    timestamp: int | None = None


def _options(dataset: str, unread_only: bool) -> None:
    if dataset not in DATASETS or type(unread_only) is not bool:
        raise ValueError("invalid cross list options")
    if dataset == "bookmarked" and unread_only:
        raise ValueError("bookmarked has no grounded unread filter")


def _normalize(row) -> CrossListEntry:
    """Resolve project/parent from public nested fields; reject conflicting IDs."""
    if not isinstance(row, dict) or not _valid_id(row.get("id")):
        raise SchemaError("cross list: message id invalid")
    project = row.get("project")
    if not isinstance(project, dict) or not _valid_id(project.get("id")):
        raise SchemaError("cross list: project missing")
    pid = project["id"]
    if "project_id" in row and (
            not _valid_id(row["project_id"]) or row["project_id"] != pid):
        raise SchemaError("cross list: project mismatch")
    parent = row.get("parent_message")
    parent_id = None
    if parent is not None:
        if (not isinstance(parent, dict) or not _valid_id(parent.get("id"))
                or parent["id"] == row["id"]):
            raise SchemaError("cross list: parent invalid")
        parent_id = parent["id"]
        if "project_id" in parent and (
                not _valid_id(parent["project_id"]) or parent["project_id"] != pid):
            raise SchemaError("cross list: parent project mismatch")
        if "project" in parent and (
                not isinstance(parent["project"], dict)
                or not _valid_id(parent["project"].get("id"))
                or parent["project"].get("id") != pid):
            raise SchemaError("cross list: parent project mismatch")
    if "parent_id" in row and (
            (row["parent_id"] is not None and not _valid_id(row["parent_id"]))
            or row["parent_id"] != parent_id):
        raise SchemaError("cross list: parent mismatch")
    unread = row.get("is_unread")
    if "is_unread" in row and type(unread) is not bool:
        raise SchemaError("cross list: unread invalid")
    return CrossListEntry(_norm_message(row, pid, parent_id=parent_id), unread)


def fetch_cross_list(adapter, dataset: str, *, unread_only: bool = False,
                     max_pages: int = 5, per_page: int = 20,
                     max_rows: int = 100, deadline_s: float = 25) -> CrossListFetch:
    """Return normalized entries only after a complete, bounded snapshot walk.

    Bounds: 1..10 pages, 1..20 rows/page, 1..200 total rows, 0<deadline<=120s.
    No retries/backoff. Missing timestamp is accepted only for a terminal first
    page; continuation requires the same positive server timestamp throughout.
    Missing is_unread remains None on entries, never evidence of a read post.
    """
    _options(dataset, unread_only)
    if (type(max_pages) is not int or not 1 <= max_pages <= 10
            or type(per_page) is not int or not 1 <= per_page <= 20
            or type(max_rows) is not int or not 1 <= max_rows <= 200
            or type(deadline_s) not in (int, float)
            or not math.isfinite(deadline_s) or not 0 < deadline_s <= 120):
        raise ValueError("invalid cross list bounds")
    result = CrossListFetch()
    if not adapter._token:
        result.reason = "cached_session_required"
        return result
    previous_deadline = adapter._deadline
    end = time.monotonic() + deadline_s
    adapter.set_deadline(min(end, previous_deadline) if previous_deadline is not None else end)
    entries, seen = [], set()
    total, total_pages = None, None
    try:
        for page in range(1, max_pages + 1):
            params = {"page": page, "per_page": per_page, "include_paginate_totals": 0}
            if dataset == "mentioned":
                params["unread"] = int(unread_only)
                if page == 1:
                    params["include_meta"] = 1
            if result.timestamp is not None:
                params["timestamp"] = result.timestamp
            # Ordinary _request can probe/retry/sleep when classifying 403.
            # Use its existing bounded worker: no login, retry or extra probe.
            params["no_extend_session"] = 1
            response = adapter._io(
                "api", adapter.timeout,
                url=f"{API}/messages/{dataset}?{urllib.parse.urlencode(params)}",
                method="GET", data=None,
                headers={"Authorization": f"Bearer {adapter._token}",
                         "Accept": "application/json"})
            status = response["status"]
            if status == 401:
                raise SessionExpired(status=status)
            if status >= 300:
                raise MCSError("http_error", status=status)
            body = base64.b64decode(response["body"], validate=True)
            adapter._remaining_timeout(adapter.timeout)
            try:
                raw = json.loads(body)
            except (ValueError, RecursionError):
                raise SchemaError("cross list: json invalid") from None
            result.pages += 1
            if not isinstance(raw, dict):
                raise SchemaError("cross list: object invalid")
            rows, pag = raw.get("messages"), raw.get("paginate")
            if (not isinstance(rows, list) or not isinstance(pag, dict)
                    or len(rows) > per_page):
                raise SchemaError("cross list: collection invalid")
            for key, wanted in (("current_page", page), ("per_page", per_page)):
                if key in pag and (type(pag[key]) is not int or pag[key] != wanted):
                    raise SchemaError("cross list: page invalid")
            has_next = pag.get("has_next")
            if "has_next" in pag and type(has_next) is not bool:
                raise SchemaError("cross list: has_next invalid")
            if type(has_next) is not bool:
                pages = pag.get("total_pages")
                if (type(pages) is not int or pages < page
                        or pag.get("current_page") != page):
                    raise SchemaError("cross list: terminal state missing")
                has_next = page < pages
            if "total_pages" in pag:
                value = pag["total_pages"]
                if (type(value) is not int or value < 0
                        or (total_pages is not None and value != total_pages)):
                    raise SchemaError("cross list: page total invalid")
                total_pages = value
            if total_pages is not None and (
                    (has_next and total_pages <= page)
                    or (not has_next and total_pages != page
                        and not (page == 1 and total_pages == 0 and not rows))):
                raise SchemaError("cross list: page total mismatch")
            stamp = pag.get("timestamp")
            if stamp is not None and not _valid_id(stamp):
                raise SchemaError("cross list: timestamp invalid")
            if page == 1:
                result.timestamp = stamp
            elif stamp != result.timestamp:
                raise SchemaError("cross list: timestamp changed")
            if has_next and result.timestamp is None:
                result.reason = "snapshot_missing"
                break
            if has_next and not rows:
                raise SchemaError("cross list: no progress")
            if len(entries) + len(rows) > max_rows:
                result.reason = "row_limit"
                break
            if "total_entries" in pag:
                value = pag["total_entries"]
                if (type(value) is not int or value < 0
                        or (total is not None and value != total)):
                    raise SchemaError("cross list: total invalid")
                total = value
            for row in rows:
                adapter._remaining_timeout(adapter.timeout)
                entry = _normalize(row)
                identity = (entry.message.project_id, entry.message.message_id)
                if identity in seen:
                    raise SchemaError("cross list: duplicate identity")
                seen.add(identity)
                entries.append(entry)
            if total is not None and (
                    (has_next and len(entries) >= total)
                    or (not has_next and len(entries) != total)):
                raise SchemaError("cross list: total mismatch")
            adapter._remaining_timeout(adapter.timeout)
            if not has_next:
                result.complete = True
                result.entries = entries
                break
        else:
            result.reason = "page_limit"
    except MCSError as error:
        result.reason = error.kind
        result.http_status = error.status
    finally:
        adapter.set_deadline(previous_deadline)
    return result


def capture_rows(result: CrossListFetch) -> list[CapturedRow]:
    """Minimal normalized observations; never substitute absent flags with false."""
    return [{"project_id": e.message.project_id, "message_id": e.message.message_id,
             "parent_id": e.message.parent_id, "posted_at": e.message.posted_at,
             "body_state": e.message.body_state, "is_unread": e.is_unread,
             "metadata": e.message.metadata, "metadata_errors": e.message.metadata_errors}
            for e in result.entries] if result.complete else []


def validate_capture(rows) -> list[CapturedRow]:
    """Parse the persisted minimal contract without trusting arbitrary artifact JSON."""
    if not isinstance(rows, list) or len(rows) > 200:
        raise SchemaError("cross list capture: rows invalid")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
                "project_id", "message_id", "parent_id", "posted_at", "body_state",
                "is_unread", "metadata", "metadata_errors"}:
            raise SchemaError("cross list capture: row invalid")
        pid, mid, parent = row["project_id"], row["message_id"], row["parent_id"]
        if (not _valid_id(pid) or not _valid_id(mid)
                or (parent is not None and (not _valid_id(parent) or parent == mid))
                or (row["is_unread"] is not None and type(row["is_unread"]) is not bool)
                or not isinstance(row["posted_at"], str)
                or row["body_state"] not in ("unknown", "snippet", "full", "deleted")
                or (pid, mid) in seen):
            raise SchemaError("cross list capture: identity invalid")
        seen.add((pid, mid))
        metadata, errors = row["metadata"], row["metadata_errors"]
        if not isinstance(metadata, dict) or not isinstance(errors, list) or any(
                e not in ("reactions_invalid", "mentions_invalid",
                          "is_bookmarked_invalid", "is_pinned_invalid") for e in errors):
            raise SchemaError("cross list capture: metadata invalid")
        raw = dict(metadata)
        if "mentions" in raw:
            mentions = raw["mentions"]
            if not isinstance(mentions, list) or any(
                    not isinstance(m, dict) or set(m) != {"type", "id"}
                    or m["type"] not in ("user", "station", "project") for m in mentions):
                raise SchemaError("cross list capture: mentions invalid")
            raw["mentions"] = [{"type": m["type"], m["type"]: {"id": m["id"]}}
                               for m in mentions]
        normalized, invalid = _message_metadata(raw, pid)
        if invalid or normalized != metadata:
            raise SchemaError("cross list capture: metadata invalid")
    return rows


def sync_cross_list(ledger, adapter, dataset: str, *, enabled: bool = False,
                    unread_only: bool = False, now: float | None = None,
                    max_pages: int = 5, per_page: int = 20,
                    max_rows: int = 100, deadline_s: float = 25) -> dict[str, JSON]:
    """Append one storage-only attempt through existing artifacts; caller owns lock.

    Complete collections are immutable artifacts. Failures store no partial set;
    prior complete artifacts survive. Current chat/semantic/metadata are untouched.
    An existing local message with a conflicting project/parent fails the whole set.
    """
    _options(dataset, unread_only)
    if type(enabled) is not bool or (now is not None and (
            type(now) not in (int, float) or not math.isfinite(now) or now < 0)):
        raise ValueError("invalid cross list capture options")
    if not enabled:
        return {"state": "disabled", "artifact_id": None}
    result = adapter.fetch_cross_list(
        dataset, unread_only=unread_only, max_pages=max_pages, per_page=per_page,
        max_rows=max_rows, deadline_s=deadline_s)
    if result.reason == "cached_session_required":
        return {"state": "unknown", "reason": result.reason, "artifact_id": None}
    rows = capture_rows(result)
    for row in rows:
        stored = ledger.db.execute(
            "SELECT project_id,parent_id FROM messages WHERE message_id=?",
            (row["message_id"],)).fetchone()
        if stored is not None and (
                stored["project_id"] != row["project_id"]
                or stored["parent_id"] != row["parent_id"]):
            result.complete, result.reason, rows = False, "scope_mismatch", []
            break
    payload = {"contract": "cross-list/1", "dataset": dataset,
               "unread_only": unread_only, "attempted_at": time.time() if now is None else now,
               "complete": result.complete, "reason": result.reason,
               "http_status": result.http_status, "timestamp": result.timestamp,
               "pages": result.pages, "rows": rows}
    aid = ledger.artifact_add(ARTIFACT_KIND, json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return {"state": "complete" if result.complete else "failed",
            "reason": result.reason, "artifact_id": aid}


def main(argv: list[str] | None = None) -> int:
    """Explicit storage-only GET under the existing writer lock; no publication."""
    from ledger import Ledger
    from mcs_adapter import MCSAdapter
    from mcs_util import acquire_run_lock

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--dataset", choices=DATASETS, required=True)
    parser.add_argument("--unread-only", action="store_true")
    parser.add_argument("--read-only-get", action="store_true")
    parser.add_argument("--token-cache", required=True)
    args = parser.parse_args(argv)
    if not args.read_only_get:
        parser.error("explicit --read-only-get required")
    if args.dataset == "bookmarked" and args.unread_only:
        parser.error("bookmarked has no grounded unread filter")
    if not os.path.isfile(args.database):
        parser.error("existing database required")
    lock_fd = acquire_run_lock(os.path.join(
        os.path.dirname(os.path.abspath(args.database)), "run.lock"))
    if lock_fd is None:
        print(json.dumps({"state": "held", "reason": "run_lock_busy"}))
        return 1
    store = None
    try:
        store = Ledger(args.database)
        adapter = MCSAdapter(token_cache=args.token_cache)
        adapter._token = adapter._read_cache()
        adapter.set_deadline(time.monotonic() + 25)
        result = sync_cross_list(store, adapter, args.dataset,
                                 enabled=True, unread_only=args.unread_only)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result["state"] == "complete" else 1
    finally:
        if store is not None:
            store.close()
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
