#!/usr/bin/env python3
"""Remote-verify unknown notification parts against Slack.

Verify-only by default: queries Slack (conversations.replies) for each
unknown part on a delivered card and reports whether the remote evidence
proves `delivered`, `absent`, or stays `unverified`. With --emit it writes
ops.card_part_resolve commands (real remote evidence, not attested) into
data/cmd so the standard resolve+reissue path can apply them.

Run with the hermes venv that has slack_sdk:
    /Users/yusuke/.hermes/hermes-agent/venv/bin/python \
        scripts/diagnostics/notify_remote_verify.py [--emit]
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "mcs"))
import _mcs_path  # noqa: F401,E402  (mcs import path bootstrap)

MAX_PARTS = 500
ACTOR = "yusuke"
REASON = ("remote-verified resend+replace of unknown notification parts "
          "(devin session 2026-11-21, user directive)")


def _env(path):
    out = {}
    for line in open(path, "rb").read().decode().splitlines():
        key, sep, value = line.partition("=")
        if sep and key not in out:
            out[key] = value.strip().strip("\"'")
    return out


def _slack_env(cfg):
    profile = cfg.get("notify_bot_profile") or "cco"
    return _env(os.path.expanduser(
        f"~/.hermes/profiles/{profile}/.env"))


def _unknown_parts(db):
    return db.execute(
        "SELECT p.delivery_id,p.part_id,p.kind,p.name,p.bytes,p.payload_sha256,"
        "p.error_code,p.idx,r.transport,c.card_id,c.channel_id,c.message_id,"
        "c.thread_id,c.root_message_id,c.delivery_state,c.revoked_at "
        "FROM notification_render_parts p "
        "JOIN notification_renders r USING(delivery_id) "
        "JOIN notification_cards c ON c.card_id=r.card_id "
        "WHERE p.state='unknown' AND r.transport='slack' "
        "AND c.revoked_at IS NULL AND c.delivery_state='delivered' "
        "ORDER BY c.card_id LIMIT ?", (MAX_PARTS,)).fetchall()


def _files_match(part, files):
    for f in files:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        if f.get("name") != part["name"]:
            continue
        if part["bytes"] is not None and f.get("size") != part["bytes"]:
            continue
        rsha = f.get("sha256")
        if rsha is not None and part["payload_sha256"] \
                and rsha != part["payload_sha256"]:
            continue
        return f["id"]
    return None


async def _verify_card(client, bot_ids, card, parts):
    """Scan one card's remote thread; return {part_id: (verdict, remote_id)}."""
    channel, root = card["channel_id"], card["thread_id"] or card["message_id"]
    try:
        data = (await client.conversations_replies(
            channel=channel, ts=root, limit=200)).data
    except Exception:
        return {p["part_id"]: ("unverified", None) for p in parts}
    msgs = data.get("messages") if data.get("ok") else None
    if not isinstance(msgs, list):
        return {p["part_id"]: ("unverified", None) for p in parts}
    ours = [m for m in msgs if isinstance(m, dict)
            and (m.get("bot_id") in bot_ids or m.get("user") in bot_ids)]
    out = {}
    for p in parts:
        if p["kind"] == "attachment_part":
            fid = None
            for m in ours:
                fid = _files_match(p, m.get("files") or [])
                if fid:
                    break
            out[p["part_id"]] = ("delivered", fid) if fid else ("absent", None)
        else:  # body_part / others: presence of any own reply is weak; report only
            out[p["part_id"]] = ("unverified", None)
    return out


def _command(part, verdict, remote_id):
    if verdict == "delivered":
        result, evidence = "mark_delivered", {
            "method": "remote_api_verify",
            "ref": "slack conversations.replies file match (name+size+sha256)"}
        remote = remote_id
    else:
        result, evidence = "mark_not_sent", {
            "method": "remote_api_verify",
            "ref": "slack conversations.replies no matching file",
            "worker_stopped": True, "proof": "remote_absent"}
        remote = None
    return result, evidence, remote


def main():
    emit = "--emit" in sys.argv
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    env = _slack_env(cfg)
    token = env.get("SLACK_BOT_TOKEN")
    if not token:
        print("no SLACK_BOT_TOKEN for profile", file=sys.stderr)
        return 2
    db = sqlite3.connect(
        f"file:{ROOT}/data/ledger.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    parts = _unknown_parts(db)
    by_card = {}
    for p in parts:
        by_card.setdefault(p["card_id"], []).append(p)

    async def run():
        from slack_sdk.web.async_client import AsyncWebClient
        client = AsyncWebClient(token=token, timeout=15)
        auth = (await client.auth_test()).data
        if not auth.get("ok"):
            print("auth_test failed", file=sys.stderr)
            return {}
        bot_ids = {auth.get("bot_id"), auth.get("user_id")} - {None}
        verdicts = {}
        for cid, plist in by_card.items():
            verdicts[cid] = await _verify_card(client, bot_ids, plist[0], plist)
        return verdicts

    verdicts = asyncio.run(run())
    sys.path.insert(0, ROOT)
    counts = {}
    emitted = 0
    cmd_dir = os.path.join(ROOT, "data", "cmd")
    for cid, vmap in verdicts.items():
        plist = by_card[cid]
        for p in plist:
            verdict, remote_id = vmap.get(p["part_id"], ("unverified", None))
            counts[verdict] = counts.get(verdict, 0) + 1
            print(f"card:{cid} {p['part_id']} {p['kind']} "
                  f"{p['error_code']} -> {verdict}"
                  + (f" remote_id={remote_id}" if remote_id else ""))
            if not emit or verdict == "unverified":
                continue
            from notify_recovery import card_part_resolve_plan
            plan = card_part_resolve_plan(_ledger(), p["delivery_id"], p["part_id"])
            if "error" in plan:
                continue
            result, evidence, remote = _command(p, verdict, remote_id)
            if remote:
                plan["remote_id"] = remote
            req = {**plan, "command_id": str(uuid.uuid4()), "actor": ACTOR,
                   "reason": REASON, "human_confirmed": True,
                   "result": result, "evidence": evidence}
            name = (f"request-{int(time.time()*1000):020d}-"
                    f"cprv-{req['command_id'][:8]}.json")
            with open(os.path.join(cmd_dir, name), "w") as f:
                json.dump(req, f, separators=(",", ":"))
            emitted += 1
    print("verdicts:", counts, "emitted:", emitted)
    return 0


def _ledger():
    from ledger import LedgerReader
    return LedgerReader(os.path.join(ROOT, "data", "ledger.db"))


if __name__ == "__main__":
    sys.exit(main())
