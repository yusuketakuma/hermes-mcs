#!/usr/bin/env python3
"""Remote-verify unknown notification parts against Slack.

Verify-only by default: queries Slack (conversations.replies) for each
unknown part on a delivered card and reports whether the remote evidence
proves `delivered`, `absent`, or stays `unverified`. With --emit it writes
ops.card_part_resolve commands (real remote evidence, not attested) into
data/cmd so the standard resolve+reissue path can apply them.

Parts flagged `unavailable` or carrying `prior_remote_id` in the sealed
manifest are reported but never emitted. `human_confirmed` and
`worker_stopped` are only written when the operator passes
--confirm-human. Body parts are matched with the same escape+
_remote_match rules as adapters/slack/delivery.py, and only on a
complete reply history: exactly one match -> delivered, zero -> absent,
multiple or part_owner_ambiguous -> unverified.

Run with the hermes venv that has slack_sdk:
    ~/.hermes/hermes-agent/venv/bin/python \
        scripts/diagnostics/notify_remote_verify.py [--emit] [--confirm-human]
"""
import asyncio
import json
import os
import pwd
import sqlite3
import sys
import time
import uuid
from html import escape

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "mcs"))
sys.path.insert(0, ROOT)
import _mcs_path  # noqa: F401,E402  (mcs import path bootstrap)
from adapters.slack.emoji_text import slack_emoji_pattern  # noqa: E402

MAX_PARTS = 500
HISTORY_LIMIT = 200
ACTOR = pwd.getpwuid(os.getuid()).pw_name
REASON = "remote-verified resend+replace of unknown notification parts"


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
        "p.error_code,p.idx,r.transport,r.spec_json,c.card_id,c.channel_id,c.message_id,"
        "c.thread_id,c.root_message_id,c.delivery_state,c.revoked_at "
        "FROM notification_render_parts p "
        "JOIN notification_renders r USING(delivery_id) "
        "JOIN notification_cards c ON c.card_id=r.card_id "
        "WHERE p.state='unknown' AND r.transport='slack' "
        "AND c.revoked_at IS NULL AND c.delivery_state='delivered' "
        "ORDER BY c.card_id LIMIT ?", (MAX_PARTS,)).fetchall()


def _spec_sections(spec_json):
    """(manifest by part_id, spec parts) from a render's sealed spec_json."""
    if not spec_json:
        return {}, {}
    try:
        parts = json.loads(spec_json)["parts"]
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}, {}
    manifest = {e["part_id"]: e for e in parts.get("manifest") or []
                if isinstance(e, dict) and e.get("part_id")}
    return manifest, parts


def _body_text(parts, entry):
    """Wire chunk for a body_part manifest entry (part_id indexes
    thread_body_parts 1-based, same rule as notify_parts receipt lookup)."""
    chunks = parts.get("thread_body_parts")
    try:
        index = int(str(entry["part_id"]).split(":")[1]) - 1
    except (IndexError, ValueError):
        return None
    if isinstance(chunks, list) and 0 <= index < len(chunks) \
            and isinstance(chunks[index], str):
        return chunks[index]
    return None


def _prepare_part(row):
    part = dict(row)
    manifest, parts = _spec_sections(part.get("spec_json"))
    entry = manifest.get(part["part_id"])
    part["manifest"] = entry
    part["body_text"] = _body_text(parts, entry) if entry else None
    return part


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


def _body_matches(msgs, wire, consumed, root_ts):
    """delivery.py _remote_match comparison rules, counting every match:
    authored replies only, exact escaped text first, emoji-pattern fallback,
    each remote ts binds at most once."""
    def eligible(m):
        mid = m.get("ts")
        return (isinstance(mid, str) and mid != root_ts
                and mid not in consumed
                and m.get("thread_ts", root_ts) == root_ts)
    exact = [m for m in msgs if eligible(m) and m.get("text") == wire]
    if exact:
        return exact
    pattern = slack_emoji_pattern(wire)
    if pattern is None:
        return []
    return [m for m in msgs if eligible(m)
            and isinstance(m.get("text"), str) and pattern.fullmatch(m["text"])]


async def _verify_card(client, bot_ids, card, parts):
    """Scan one card's remote thread; return {part_id: (verdict, remote_id)}."""
    channel, root = card["channel_id"], card["thread_id"] or card["message_id"]
    try:
        data = (await client.conversations_replies(
            channel=channel, ts=root, limit=HISTORY_LIMIT)).data
    except Exception:
        return {p["part_id"]: ("unverified", None) for p in parts}
    msgs = data.get("messages") if data.get("ok") else None
    if not isinstance(msgs, list) or data.get("has_more") \
            or len(msgs) >= HISTORY_LIMIT:
        return {p["part_id"]: ("unverified", None) for p in parts}
    ours = [m for m in msgs if isinstance(m, dict)
            and (m.get("bot_id") in bot_ids or m.get("user") in bot_ids)]
    consumed = set()
    out = {}
    for p in parts:
        if p["kind"] == "attachment_part":
            fid = None
            for m in ours:
                fid = _files_match(p, m.get("files") or [])
                if fid:
                    break
            out[p["part_id"]] = ("delivered", fid) if fid else ("absent", None)
        elif p["kind"] == "body_part":
            text = p.get("body_text")
            if text is None or p.get("error_code") == "part_owner_ambiguous":
                out[p["part_id"]] = ("unverified", None)
                continue
            matches = _body_matches(ours, escape(text, quote=False),
                                    consumed, root)
            if len(matches) == 1:
                consumed.add(matches[0]["ts"])
                out[p["part_id"]] = ("delivered", matches[0]["ts"])
            elif not matches:
                out[p["part_id"]] = ("absent", None)
            else:
                out[p["part_id"]] = ("unverified", None)
        else:  # file_group / others: presence of any own reply is weak; report only
            out[p["part_id"]] = ("unverified", None)
    return out


def _command(part, verdict, remote_id, *, attest):
    file_kind = part["kind"] == "attachment_part"
    what = "file" if file_kind else "body text"
    if verdict == "delivered":
        result, evidence = "mark_delivered", {
            "method": "remote_api_verify",
            "ref": f"slack conversations.replies {what} match"}
        remote = remote_id
    else:
        result, evidence = "mark_not_sent", {
            "method": "remote_api_verify",
            "ref": f"slack conversations.replies no matching {what}",
            "proof": "remote_absent"}
        remote = None
        if attest:
            evidence["worker_stopped"] = True
    return result, evidence, remote


def main():
    emit = "--emit" in sys.argv
    attest = "--confirm-human" in sys.argv
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
            prepared = [_prepare_part(p) for p in plist]
            verdicts[cid] = await _verify_card(client, bot_ids, plist[0], prepared)
        return verdicts

    verdicts = asyncio.run(run())
    sys.path.insert(0, ROOT)
    counts = {}
    emitted = 0
    cmd_dir = os.path.join(ROOT, "data", "cmd")
    prepared_by_id = {}
    for cid, plist in by_card.items():
        for p in plist:
            prepared_by_id[(cid, p["part_id"])] = _prepare_part(p)
    for cid, vmap in verdicts.items():
        for p in by_card[cid]:
            part = prepared_by_id[(cid, p["part_id"])]
            verdict, remote_id = vmap.get(p["part_id"], ("unverified", None))
            entry = part.get("manifest") or {}
            if entry.get("unavailable") is True or entry.get("prior_remote_id"):
                verdict = "skipped"
            counts[verdict] = counts.get(verdict, 0) + 1
            print(f"card:{cid} {p['part_id']} {p['kind']} "
                  f"{p['error_code']} -> {verdict}"
                  + (f" remote_id={remote_id}" if remote_id else ""))
            if not emit or verdict in ("unverified", "skipped"):
                continue
            from notify_recovery import card_part_resolve_plan
            plan = card_part_resolve_plan(_ledger(), p["delivery_id"], p["part_id"])
            if "error" in plan:
                continue
            result, evidence, remote = _command(
                part, verdict, remote_id, attest=attest)
            if remote:
                plan["remote_id"] = remote
            req = {**plan, "command_id": str(uuid.uuid4()), "actor": ACTOR,
                   "reason": REASON, "result": result, "evidence": evidence}
            if attest:
                req["human_confirmed"] = True
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
