#!/usr/bin/env python3
"""明示指定した投稿のキー・型と既読非変更を確認し、本文や秘密値を出力しない。"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: F401
from mcs_adapter import (MCSAdapter, MCSError, SchemaError, UNREAD_SCREEN_CAP, _has_next,
                         _message_metadata, _valid_id, walk_reaction_actors)
from mcs_util import CACHE


def shape(value):
    if isinstance(value, dict):
        return {
            k: shape(v)
            for k, v in value.items()
            if k
            in {"type", "count", "self_reacted", "user", "station", "project", "id",
                "reaction_type", "page", "current_page", "per_page", "has_next",
                "timestamp", "total_pages", "total_entries"}
        }
    if isinstance(value, list):
        return [shape(v) for v in value[:5]]
    return type(value).__name__


def _target_unread_state(adapter, project_id, message_id, timestamp, *, parent_id=None):
    """Observe a raw target flag through the existing bounded unread route."""
    if (not _valid_id(project_id) or not _valid_id(message_id)
            or type(timestamp) is not int or timestamp <= 0
            or (parent_id is not None and (
                not _valid_id(parent_id) or parent_id == message_id))):
        raise SchemaError("probe: observation identifiers invalid")
    seen, roots, total, total_pages, target = set(), 0, None, None, None
    for page in range(1, 3):
        raw = adapter._get(f"/projects/{project_id}/messages", {
            "unread": 1, "timestamp": timestamp, "keep_read_status": 1,
            "include_meta": 1, "exclude_terminated_ex_application": 1,
            "include_paginate_totals": 1, "per_page": 50, "page": page,
        }, extend_session=False)
        items, pag = raw.get("messages"), raw.get("paginate")
        if not isinstance(items, list) or len(items) > 50 or not isinstance(pag, dict):
            raise SchemaError("probe: unread page invalid")
        has_next = _has_next(pag, "probe unread")
        for key, expected in (("current_page", page), ("per_page", 50)):
            if key in pag and (type(pag[key]) is not int or pag[key] != expected):
                raise SchemaError("probe: unread pagination invalid")
        if "total_entries" in pag:
            value = pag["total_entries"]
            if type(value) is not int or value < 0 or (total is not None and value != total):
                raise SchemaError("probe: unread total invalid")
            total = value
        if "total_pages" in pag:
            pages = pag["total_pages"]
            if (type(pages) is not int or pages < 0 or (pages == 0 and items)
                    or (total_pages is not None and pages != total_pages)):
                raise SchemaError("probe: unread pagination contradictory")
            total_pages = pages
        if total_pages is not None and (
                (has_next and total_pages <= page)
                or (not has_next and total_pages != page
                    and not (total_pages == 0 and page == 1 and not items))):
            raise SchemaError("probe: unread pagination contradictory")
        if has_next and not items:
            raise SchemaError("probe: unread pagination not advancing")
        roots += len(items)
        for root in items:
            if not isinstance(root, dict) or not _valid_id(root.get("id")):
                raise SchemaError("probe: unread message invalid")
            threads = root.get("thread_messages", [])
            if not isinstance(threads, list):
                raise SchemaError("probe: unread replies invalid")
            for row, parent in [(root, None)] + [(reply, root["id"]) for reply in threads]:
                if (not isinstance(row, dict) or not _valid_id(row.get("id"))
                        or not _valid_id(row.get("project_id", project_id))
                        or row.get("project_id", project_id) != project_id):
                    raise SchemaError("probe: unread message identity invalid")
                if row["id"] in seen:
                    raise SchemaError("probe: duplicate unread message")
                seen.add(row["id"])
                if "is_unread" in row and type(row["is_unread"]) is not bool:
                    raise SchemaError("probe: unread flag invalid")
                if row["id"] == message_id:
                    if parent != parent_id:
                        raise SchemaError("probe: unread target relation invalid")
                    target = row.get("is_unread")
        if not has_next:
            if total is not None and total < roots:
                raise SchemaError("probe: unread total contradictory")
            if roots >= UNREAD_SCREEN_CAP or (total is not None and total > roots):
                return None
            return target
    return None


def probe(adapter, project_id, message_id, *, parent_id=None):
    # Independent raw unread observations bracket both exact GETs.
    def state():
        seen, target, timestamp = set(), None, None
        for page in range(1, 6):
            raw = adapter._get('/projects', {
                'page': page, 'per_page': 100, 'include_meta': 1,
                'include_paginate_totals': 0},
                extend_session=False)
            projects, pag = raw.get('projects'), raw.get('paginate')
            if (not isinstance(projects, list) or len(projects) > 100
                    or not isinstance(pag, dict)):
                raise SchemaError('probe: projects invalid')
            for key, expected in (("current_page", page), ("per_page", 100)):
                if key in pag and (type(pag[key]) is not int or pag[key] != expected):
                    raise SchemaError('probe: project pagination invalid')
            page_ts = pag.get('timestamp')
            if type(page_ts) is not int or page_ts <= 0:
                raise SchemaError('probe: project timestamp invalid')
            timestamp = page_ts if timestamp is None else min(timestamp, page_ts)
            has_next = _has_next(pag, 'probe projects')
            for project in projects:
                if (not isinstance(project, dict) or not _valid_id(project.get('id'))
                        or type(project.get('is_unread')) is not bool):
                    raise SchemaError('probe: project unread state invalid')
                if project['id'] in seen:
                    raise SchemaError('probe: duplicate project')
                seen.add(project['id'])
                if project['id'] == project_id:
                    target = project['is_unread']
            if has_next and not projects:
                raise SchemaError('probe: project pagination not advancing')
            if not has_next:
                if target is not None:
                    return target, timestamp
                break
        raise SchemaError('probe: target state unavailable')

    before, before_ts = state()
    target_unread = _target_unread_state(
        adapter, project_id, message_id, before_ts, parent_id=parent_id)
    if before is False and target_unread is True:
        raise SchemaError('probe: unread observations contradictory')
    params = {"message_id": message_id, "per_page": 1, "keep_read_status": 1}
    path = f"/projects/{project_id}/messages"
    if parent_id is not None:
        path += f"/{parent_id}/messages"
    first = adapter._get(
        path, params, extend_session=False
    )
    items = first.get("messages")
    if (
        not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
        or not _valid_id(items[0].get("id"))
        or not _valid_id(items[0].get("project_id", project_id))
        or items[0].get("id") != message_id
        or items[0].get("project_id", project_id) != project_id
    ):
        raise SchemaError("probe: target mismatch")
    raw = items[0]
    message = (adapter.fetch_message_metadata(project_id, message_id, parent_id=parent_id)
               if parent_id is not None else
               adapter.fetch_message_metadata(project_id, message_id))
    after, after_ts = state()
    if after_ts < before_ts:
        raise SchemaError('probe: project timestamp moved backwards')
    target_after = _target_unread_state(
        adapter, project_id, message_id, after_ts, parent_id=parent_id)
    if after is False and target_after is True:
        raise SchemaError('probe: unread observations contradictory')
    preserved = before == after
    target_unchanged = (target_unread == target_after
                        if target_unread is not None and target_after is not None else None)
    first_metadata, first_errors = _message_metadata(raw, project_id)
    first_reactions = first_metadata.get("reactions")
    viewed_before = [r for r in first_reactions if isinstance(r, dict)
                     and r.get("type") == "viewed"] if isinstance(first_reactions, list) else None
    reactions_after = message.metadata.get("reactions")
    viewed_after = [r for r in reactions_after if r["type"] == "viewed"] \
        if reactions_after is not None else None
    viewed_unchanged = (viewed_before == viewed_after if viewed_before is not None
                        and viewed_after is not None else None)
    return {
        "contract": "message-metadata-probe/1",
        "keys_and_types": {
            k: shape(raw[k])
            for k in ("reactions", "mentions", "is_bookmarked", "is_pinned")
            if k in raw
        },
        "normalized_fields": sorted(message.metadata),
        "schema_errors": sorted(set(first_errors + message.metadata_errors)),
        "read_state_unchanged": preserved,
        "unread_at_start": before,
        "first_get_effect": "independent raw unread observations bracket both exact GETs",
        "target_unread_at_start": target_unread,
        "target_unread_after_gets": target_after,
        "target_read_state_basis": "raw unread response; absent flags or incomplete walks are unknown",
        "target_read_state_unchanged": target_unchanged,
        "unread_preservation_proven": (before is True and preserved
                                       and target_unread is True and target_unchanged is True),
        "viewed_unchanged_between_gets": viewed_unchanged,
        "viewed_comparison_scope": "between exact GETs only; first GET and independent observer effects unproven",
        "first_get_viewed_effect": "unproven",
        "no_extend_session": "sent, expiry effect requires separate observation",
        "post_or_mark_read_called": False,
    }


def probe_routes(adapter, project_id, message_id, *, unread_at_start, parent_id=None):
    """Report future-route shapes; unverified routes never read an unread target."""
    if unread_at_start:
        return {"state": "deferred", "reason": "unverified_routes_on_unread_target"}
    routes = [
        ("batch", "/messages", {"message_ids": str(message_id),
         "include_oldest_unread_thread_message_id": 1}, "messages"),
        ("actors_all", f"/messages/{message_id}/user_reactions",
         {"page": 1, "per_page": 50, "include_meta": 1, "include_paginate_totals": 0},
         "reactions"),
        ("actors_viewed", f"/messages/{message_id}/reactions",
         {"page": 1, "per_page": 50, "reaction_type": "viewed",
          "include_meta": 1, "include_paginate_totals": 0}, "users"),
        ("mentioned", "/messages/mentioned", {"page": 1, "per_page": 1,
         "include_meta": 1}, "messages"),
        ("bookmarked", "/messages/bookmarked", {"page": 1, "per_page": 1}, "messages"),
    ]
    path = f"/projects/{project_id}/messages"
    if parent_id is not None:
        path += f"/{parent_id}/messages"
    routes.extend((f"exact_include_meta_{meta}", path,
                   {"message_id": message_id, "per_page": 1, "keep_read_status": 1,
                    "include_meta": meta}, "messages") for meta in (0, 1))
    results = {}
    for label, path, params, key in routes:
        try:
            raw = adapter._get(path, params, extend_session=False)
            rows = raw.get(key)
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise SchemaError("probe: collection invalid")
            info = {"collection_type": "list", "rows": len(rows),
                    "row_shape": shape(rows), "paginate_shape": shape(raw.get("paginate"))}
            if key == "messages" and label not in {"mentioned", "bookmarked"}:
                info["target_returned"] = any(r.get("id") == message_id for r in rows)
            if rows and key == "messages":
                info["metadata_shape"] = {k: shape(rows[0][k]) for k in (
                    "reactions", "mentions", "is_bookmarked", "is_pinned") if k in rows[0]}
            results[label] = info
        except MCSError as error:
            results[label] = {"error": error.kind, "status": error.status}
    return {"state": "observed", "routes": results,
            "unread_preservation_proven": False,
            "no_extend_session": "sent", "increment_count_sent": False,
            "post_or_mark_read_called": False}


def probe_actor_pages(adapter, project_id, message_id, *, reaction_type=None,
                      max_pages=10, per_page=50):
    """Compare two bounded actor walks; emit counts and conclusions, never actor identities."""
    def walk():
        try:
            raw = walk_reaction_actors(adapter._get, project_id, message_id,
                                       reaction_type=reaction_type,
                                       max_pages=max_pages, per_page=per_page)
        except ValueError:
            raise ValueError("invalid actor probe arguments") from None
        actors = {(a["actor_id"], a["reaction_type"]) for a in raw["actors"]}
        result = {key: raw[key] for key in (
            "complete", "pages", "counts_match_message", "timestamp_stable")}
        result["rows"] = len(actors)
        result["multiple_kinds_per_actor_observed"] = (
            len(actors) > len({uid for uid, _ in actors}) if raw["complete"] else None)
        result.update({key: raw[key] for key in ("error", "status") if key in raw})
        return result, actors

    first, first_actors = walk()
    # An incomplete snapshot cannot prove cancellation or absence. Do not repeat failures.
    if not first["complete"]:
        return {"state": "observed", "complete": False, "samples": [first],
                "actor_set_unchanged": None, "post_or_mark_read_called": False}
    second, second_actors = walk()
    unchanged = first_actors == second_actors if second["complete"] else None
    return {"state": "observed", "complete": second["complete"] and unchanged is True,
            "samples": [first, second], "actor_set_unchanged": unchanged,
            "post_or_mark_read_called": False}


def usage_counts(adapter, *, max_pages=5):
    """Count patient datasets and group consultations; retain no identities or records."""
    if type(max_pages) is not int or not 1 <= max_pages <= 5:
        raise ValueError("invalid usage page limit")
    patients, groups, seen_projects = set(), set(), set()
    unknown_project_types, unknown_medical_kartes = 0, 0
    total_entries, total_pages = None, None
    complete = False
    for page in range(1, max_pages + 1):
        raw = adapter._get("/projects", {"page": page, "per_page": 100,
                           "include_paginate_totals": 0}, extend_session=False)
        projects, paginate = raw.get("projects"), raw.get("paginate")
        if (not isinstance(projects, list) or len(projects) > 100
                or not isinstance(paginate, dict)):
            raise SchemaError("usage: project page invalid")
        for key, expected in (("current_page", page), ("per_page", 100)):
            if key in paginate and (type(paginate[key]) is not int or paginate[key] != expected):
                raise SchemaError("usage: project pagination invalid")
        has_next = _has_next(paginate, "usage")
        for key in ("total_entries", "total_pages"):
            if key in paginate and (type(paginate[key]) is not int or paginate[key] < 0):
                raise SchemaError("usage: project totals invalid")
        if "total_entries" in paginate:
            if total_entries is not None and paginate["total_entries"] != total_entries:
                raise SchemaError("usage: project total changed")
            total_entries = paginate["total_entries"]
        if "total_pages" in paginate:
            if total_pages is not None and paginate["total_pages"] != total_pages:
                raise SchemaError("usage: project pages changed")
            total_pages = paginate["total_pages"]
        if total_pages is not None and (
                (has_next and total_pages <= page)
                or (not has_next and total_pages != page
                    and not (total_pages == 0 and page == 1 and not projects))):
            raise SchemaError("usage: project terminal state contradictory")
        if has_next and not projects:
            raise SchemaError("usage: project pagination not advancing")
        for project in projects:
            if not isinstance(project, dict) or not _valid_id(project.get("id")):
                raise SchemaError("usage: project identity invalid")
            if project["id"] in seen_projects:
                raise SchemaError("usage: duplicate project")
            seen_projects.add(project["id"])
            karte = project.get("karte")
            if isinstance(karte, dict) and _valid_id(karte.get("id")):
                patients.add(karte["id"])
            elif project.get("type") == "medical":
                unknown_medical_kartes += 1
            if project.get("type") == "group":
                groups.add(project["id"])
            elif project.get("type") != "medical":
                unknown_project_types += 1
        if total_entries is not None and (
                len(seen_projects) > total_entries
                or (has_next and len(seen_projects) >= total_entries)
                or (not has_next and len(seen_projects) != total_entries)):
            raise SchemaError("usage: project count contradictory")
        if not has_next:
            complete = True
            break
    registered = {key: set() for key in (
        "medication_periods", "observation_items", "consultations")}
    unknown = {key: set() for key in registered}
    errors = {}
    routes = []
    for kid in patients:
        routes.extend([(kid, "medication_periods", f"/kartes/{kid}/medication_periods", {}),
                  (kid, "observation_items", f"/kartes/{kid}/observation_items",
                   {"page": 1, "per_page": 1, "include_all": 1})])
    routes.extend((pid, "consultations", f"/projects/{pid}/consultations",
                   {"page": 1, "per_page": 1, "include_paginate_totals": 0}) for pid in groups)
    for entity_id, key, path, params in routes:
        try:
            response = adapter._get(path, params, extend_session=False)
            rows = response.get(key)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise SchemaError("usage: collection invalid")
            if not rows and "paginate" in response:
                paginate = response["paginate"]
                if not isinstance(paginate, dict):
                    raise SchemaError("usage: collection pagination invalid")
                if "has_next" in paginate and _has_next(paginate, "usage collection"):
                    raise SchemaError("usage: empty collection not terminal")
                for field, expected in (("current_page", params.get("page", 1)),
                                        ("per_page", params.get("per_page"))):
                    if expected is not None and field in paginate and (
                            type(paginate[field]) is not int or paginate[field] != expected):
                        raise SchemaError("usage: collection pagination invalid")
                for field, allowed in (("total_entries", (0,)), ("total_pages", (0, 1))):
                    if field in paginate and (
                            type(paginate[field]) is not int or paginate[field] not in allowed):
                        raise SchemaError("usage: empty collection totals contradictory")
            if rows:
                registered[key].add(entity_id)
        except MCSError as error:
            unknown[key].add(entity_id)
            reason = f"{error.kind}:{error.status}" if error.status else error.kind
            errors.setdefault(key, {})[reason] = errors.get(key, {}).get(reason, 0) + 1
    datasets = {key: {"unit": "patients", "patients_with_records": len(registered[key]),
                      "patients_unknown": len(unknown[key]),
                      "complete": (complete and not unknown_project_types
                                   and not unknown_medical_kartes and not unknown[key])}
                for key in ("medication_periods", "observation_items")}
    datasets["consultations"] = {
        "unit": "groups", "groups_with_records": len(registered["consultations"]),
        "groups_unknown": len(unknown["consultations"]),
        "complete": complete and not unknown_project_types and not unknown["consultations"],
        "patient_count_known": False,
    }
    return {"contract": "metadata-usage-counts/2", "inventory_complete": complete,
            "patients_in_inventory": len(patients), "groups_in_inventory": len(groups),
            "projects_with_unknown_type": unknown_project_types,
            "medical_projects_with_unknown_karte": unknown_medical_kartes,
            "datasets": datasets, "errors": errors, "post_or_mark_read_called": False}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-id", type=int)
    ap.add_argument("--message-id", type=int)
    ap.add_argument("--parent-id", type=int, help="root post ID when checking a reply")
    ap.add_argument("--usage-counts", action="store_true",
                    help="count #26/#27 registrations; never print patient IDs or records")
    ap.add_argument("--extended-contracts", action="store_true",
                    help="also inspect future GET routes on a previously read target")
    ap.add_argument("--actor-pages", action="store_true",
                    help="compare bounded complete actor walks on a previously read target")
    ap.add_argument("--actor-per-page", type=int, default=50,
                    help="actor diagnostic page size (1..50); 1 forces paging with few actors")
    ap.add_argument(
        "--read-only-target",
        action="store_true",
        required=True,
        help="confirm this exact post is authorized for read-only verification",
    )
    ap.add_argument("--token-cache", default=CACHE)
    args = ap.parse_args()
    if not 1 <= args.actor_per_page <= 50 or (args.actor_per_page != 50 and not args.actor_pages):
        ap.error("--actor-per-page requires --actor-pages and a value in 1..50")
    if args.usage_counts:
        if (args.project_id is not None or args.message_id is not None or args.parent_id is not None
                or args.extended_contracts or args.actor_pages):
            ap.error("--usage-counts does not accept post IDs or extended contracts")
    elif not _valid_id(args.project_id) or not _valid_id(args.message_id):
        ap.error("valid --project-id and --message-id are required")
    if args.parent_id is not None and (
            not _valid_id(args.parent_id) or args.parent_id == args.message_id):
        ap.error("valid distinct --parent-id is required for a reply")
    adapter = MCSAdapter(token_cache=args.token_cache)
    adapter.set_deadline(time.monotonic() + (300 if args.usage_counts else 90 if args.actor_pages else 45))
    # Do not refresh cache, auto-login, or read Keychain.
    adapter._token = adapter._read_cache()
    if not adapter._token:
        print(json.dumps({"error": "cached_session_required"}))
        return 1
    try:
        report = (usage_counts(adapter) if args.usage_counts else
                  probe(adapter, args.project_id, args.message_id, parent_id=args.parent_id))
        unverified_routes_deferred = (not args.usage_counts and (
            report["unread_at_start"] is not False
            or report["target_unread_at_start"] is not False
            or report["read_state_unchanged"] is not True
            or report.get("target_read_state_unchanged") is not True
            or bool(report["schema_errors"])))
        if args.extended_contracts:
            report["extended_contracts"] = probe_routes(
                adapter, args.project_id, args.message_id,
                unread_at_start=unverified_routes_deferred, parent_id=args.parent_id)
        if args.actor_pages:
            report["actor_pages"] = (
                {"state": "deferred", "reason": "unverified_routes_on_unread_target"}
                if unverified_routes_deferred else {
                    label: probe_actor_pages(adapter, args.project_id, args.message_id,
                                             reaction_type=kind, per_page=args.actor_per_page)
                    for label, kind in (("all", None), ("viewed", "viewed"))})
    except MCSError as error:
        print(json.dumps({"error": error.kind}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    if args.usage_counts:
        return 0 if all(d["complete"] for d in report["datasets"].values()) else 2
    if (not report["read_state_unchanged"] or report["schema_errors"]
            or report.get("target_read_state_unchanged") is False
            or report["viewed_unchanged_between_gets"] is False):
        return 1
    if args.actor_pages:
        if report["actor_pages"].get("state") == "deferred" or any(
                not result.get("complete") for result in report["actor_pages"].values()
                if isinstance(result, dict)):
            return 2
    return 0 if (report["unread_preservation_proven"]
                 and report["viewed_unchanged_between_gets"] is True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
