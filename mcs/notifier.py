"""Discord notifier — drains notify_outbox to a Discord channel.

Credentials: bot token is read at send time; it is never printed, logged,
or stored elsewhere. When ~/.mcs/config.json sets "notify_bot_profile"
(e.g. "cco"), the token comes from ~/.hermes/profiles/<name>/.env so posts
arrive under that bot's identity; otherwise ~/.hermes/.env is used.
Channel id comes from ~/.mcs/config.json {discord_channel_id} falling
back to DISCORD_HOME_CHANNEL in ~/.hermes/.env.

Events carry message_ids; message content is looked up in the local ledger
at send time so the outbox payload itself stays tiny.
"""
import hashlib
import json
import math
import mimetypes
import os
import re
import time
import urllib.request
import urllib.error
import uuid

from mcs_util import (NoRedirect, env_value, html_to_text, load_config,
                      no_proxy_opener)

ENV_PATH = os.path.expanduser("~/.hermes/.env")
CONF_PATH = os.path.expanduser("~/.mcs/config.json")
API = "https://discord.com/api/v10"
_MAX_LEN = 1900
# Discord per-message file caps; oversized files stay local-only and are
# noted in the text instead of failing the whole send.
_MAX_FILES = 10
_MAX_FILE_BYTES = 24 * 1024 * 1024
_MAX_FILES_BYTES = 23 * 1024 * 1024  # leaves room for JSON + multipart framing
_MAX_REQUEST_BYTES = 24 * 1024 * 1024


def _env(key: str, path: str = ENV_PATH) -> str | None:
    # File-pinned lookup: a stray shell DISCORD_BOT_TOKEN must never
    # override the profile's own .env — env_value(check_env=False).
    return env_value(key, paths=(path,), check_env=False)


_PROFILE_ENV = os.path.expanduser("~/.hermes/profiles/{}/.env")


class _DeferredSend(Exception):
    """Not deliverable under CURRENT policy (e.g. semantic mode left
    enforce) — the committed intent stays pending for a later flush;
    it is not destroyed (INV-23) and never sent past its gate."""


class _StaleSend(Exception):
    """The source generation this intent was built for has moved on —
    suppress terminally instead of publishing stale results (§18.4)."""


class _FreezeSend(Exception):
    """A semantic intent changed after a chunk was accepted — preserve its
    receipt/progress and quarantine the remaining chunks."""


def _config() -> dict:
    return load_config(CONF_PATH)


def _token() -> str | None:
    """Bot token for sends. `notify_bot_profile` in config.json pins a
    dedicated bot identity (e.g. "cco"); when set, its token MUST resolve —
    falling back to another bot would post under the wrong identity."""
    profile = _config().get("notify_bot_profile")
    if isinstance(profile, str) and profile:
        if not re.fullmatch(r"[a-z0-9_-]+", profile):
            return None
        return _env("DISCORD_BOT_TOKEN", _PROFILE_ENV.format(profile))
    return _env("DISCORD_BOT_TOKEN")


def _channel_id(kind: str) -> str | None:
    """Patient-content events ONLY go to the explicitly configured MCS
    channel — a missing config must never spill bodies into a fallback
    channel (Oracle B15). System alerts may use the home channel."""
    cid = _config().get("discord_channel_id")
    if cid and str(cid).isdigit():
        return str(cid)
    if kind in ("session_expired", "run_failed"):
        cid = _env("DISCORD_HOME_CHANNEL")
        return cid if cid and cid.isdigit() else None
    return None


def _artifact(ledger, kind: str, mid: int) -> dict | None:
    r = ledger.db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND a.message_id=? "
        "AND CASE WHEN json_valid(a.meta) THEN "
        "json_extract(a.meta,'$.error') IS NOT 1 AND "
        "json_extract(a.meta,'$.hash')=m.content_hash ELSE 0 END "
        "AND CASE WHEN json_valid(a.content) THEN "
        "json_type(a.content)='object' ELSE 0 END "
        "ORDER BY a.artifact_id DESC LIMIT 1", (kind, mid)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) else None


_RX_LABEL = {"start": "開始", "stop": "中止", "change": "変更",
             "no_change": "変更なし", "increase": "増量", "decrease": "減量"}
_REQ_LABEL = {"confirm": "確認", "contact": "連絡", "share": "共有",
              "ask": "質問", "request": "依頼", "report": "報告"}
_EVT_LABEL = {"admission": "入院", "discharge": "退院", "exam": "受診/検査",
              "visit": "訪問", "medication": "投薬", "adherence": "服薬",
              "care": "介護", "eol": "看取り", "media_ref": "添付"}


def _structured_lines(ledger, mid: int) -> list[str]:
    """Compact structured summary from extract_v1/extract_llm artifacts.
    Returns [] when nothing usable exists (caller falls back to raw only)."""
    v1 = _artifact(ledger, "extract_v1", mid) or {}
    llm = _artifact(ledger, "extract_llm", mid) or {}
    if not v1 and not llm:
        return []
    lines: list[str] = []
    if (llm.get("summary") or "").strip():
        lines.append(f"要約: {llm['summary'].strip()[:80]}")
    pts = [str(p).strip() for p in (llm.get("points") or [])
           if isinstance(p, str) and p.strip()]
    if pts:
        lines.append("要点: " + " / ".join(p[:40] for p in pts[:3]))
    evs = [e for e in (llm.get("events") or v1.get("events") or [])
           if e in _EVT_LABEL]
    if evs:
        lines.append("区分: " + "・".join(_EVT_LABEL[e] for e in evs[:5]))
    vit = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else None
    if not vit and isinstance(v1.get("vitals"), dict):
        vit = v1["vitals"]
    if vit:
        parts = []
        if vit.get("sbp") is not None:
            parts.append(f"BP {vit['sbp']}/{vit.get('dbp')}")
        for k, lab in (("bt", "BT"), ("hr", "HR"), ("rr", "RR"),
                       ("spo2", "SpO2"), ("bs", "BS")):
            if vit.get(k) is not None:
                parts.append(f"{lab} {vit[k]}")
        if parts:
            lines.append("バイタル: " + "  ".join(parts))
    syms, neg, seen, neg_seen = [], [], set(), set()
    for s in llm.get("symptoms") or []:
        if isinstance(s, dict) and s.get("text"):
            if s.get("negated"):
                if s["text"] not in neg_seen:
                    neg_seen.add(s["text"])
                    neg.append(s["text"])
            elif s["text"] not in seen:
                seen.add(s["text"])
                syms.append(s["text"])
    for s in v1.get("symptoms") or []:
        contradicted = any(n in s or s in n for n in neg_seen)
        if s and s not in seen and not contradicted:
            syms.append(s)
    if syms or neg:
        line = "症状: " + "、".join(syms[:6])
        if neg:
            line += ("　" if syms else "") + "、".join(
                f"{n}なし" for n in neg[:4])
        lines.append(line)
    meds = []
    for m in llm.get("meds") or []:
        if isinstance(m, dict) and m.get("name"):
            d = str(m["name"]) + (f" {m['dose']}" if m.get("dose") else "")
            if m.get("action") in _RX_LABEL:
                d += f"[{_RX_LABEL[m['action']]}]"
            meds.append(d)
    if not meds:
        for m in v1.get("medications") or []:
            if isinstance(m, dict) and m.get("name"):
                meds.append(str(m["name"]) +
                            (f" {m['dose']}" if m.get("dose") else ""))
    rx = [f"{_RX_LABEL[a['action']]}:{a['ctx'][:18]}"
          for a in v1.get("rx_actions") or []
          if isinstance(a, dict) and a.get("action") in _RX_LABEL
          and a.get("ctx")]
    if meds or rx:
        lines.append("薬剤: " + "、".join((meds + rx)[:6]))
    reqs = []
    for r in llm.get("requests") or []:
        if isinstance(r, dict) and (r.get("to") or r.get("action")):
            to = str(r.get("to") or "")
            to = "" if to in ("不明", "unknown", "-") else f"{to}へ"
            reqs.append(to + str(r.get("action") or "")[:30])
    if not reqs:
        for r in v1.get("requests") or []:
            if isinstance(r, dict) and r.get("ctx"):
                reqs.append(f"{_REQ_LABEL.get(r.get('kind'), '依頼')}:"
                            f"{r['ctx'][:24]}")
    if reqs:
        lines.append("依頼: " + " / ".join(reqs[:3]))
    if v1.get("med_periods"):
        mp = v1["med_periods"][0]
        if isinstance(mp, dict) and mp.get("start"):
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    return lines


def _urgency(ledger, mid: int) -> str | None:
    for kind in ("extract_llm", "extract_v1"):
        d = _artifact(ledger, kind, mid)
        if d and d.get("urgency") == "high":
            return "high"
    return None


def _attachments_map(ledger, mids: list[int]) -> dict:
    """message_id -> attachment rows (all states) for 📎 markers + files."""
    if not mids:
        return {}
    q = ("SELECT message_id,file_id,name,state,local_path,bytes,sha256 "
         f"FROM attachments WHERE message_id IN ({','.join('?' * len(mids))}) "
         "ORDER BY attachment_id")
    out: dict[int, list] = {}
    for r in ledger.db.execute(q, mids):
        out.setdefault(r["message_id"], []).append(r)
    return out


def _collect_files(att_map: dict, order: list[int]) -> list[tuple[str, str]]:
    """Downloaded attachment (filename, local_path) pairs for upload —
    message order, max _MAX_FILES, each <= _MAX_FILE_BYTES."""
    files = []
    total = 0
    for mid in order:
        for a in att_map.get(mid, []):
            if len(files) >= _MAX_FILES:
                return files
            if a["state"] != "downloaded" or not a["local_path"]:
                continue
            if not os.path.exists(a["local_path"]):
                continue
            try:
                size = os.path.getsize(a["local_path"])
            except OSError:
                continue
            if size > _MAX_FILE_BYTES or total + size > _MAX_FILES_BYTES:
                continue
            if not a["sha256"]:
                continue
            h = hashlib.sha256()
            try:
                with open(a["local_path"], "rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(block)
            except OSError:
                continue
            if h.hexdigest() != a["sha256"]:
                continue
            fname = os.path.basename(str(a["name"] or a["file_id"] or "file"))
            fname = re.sub(r'["\\\r\n]', "_", fname) or "file"
            files.append((fname[:150], a["local_path"]))
            total += size
    return files


def _format_event(ledger, ev) -> tuple[str, list[tuple[str, str]]]:
    """Returns (content, files). files = [(filename, local_path)] to upload."""
    try:
        payload = json.loads(ev["payload"])
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("payload_invalid") from e
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    if ev["kind"] == "session_expired":
        return ("[MCS] セッション失効 — 手動再ログインが必要です\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "run_failed":
        return ("[MCS] チェック失敗 — アダプタを確認してください\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "semantic_notice":
        # audited/degraded semantic notice — the payload carries the
        # final text frozen at enqueue time; the sender never re-derives
        # it (INV-14). Two gates are re-checked at the last moment
        # (§18.4/§22.1): mode must still be enforce, and the audited
        # notice's input fingerprint must still be live — a degraded
        # notice is instead suppressed if its base event has since
        # delivered.
        _semantic_gate(ledger, ev, payload)
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "signal":
        # review-candidate notice — frozen text + ids at enqueue time.
        # Last-moment gates like semantic_notice: the flag may have been
        # turned off, or the signal may have resolved while queued —
        # both are terminal drops, not retries.
        sig_cfg = _config().get("signals")
        if not (isinstance(sig_cfg, dict)
                and sig_cfg.get("notify") is True):
            raise _StaleSend("signals_notify_disabled")
        skey = payload.get("signal_key")
        row = skey and ledger.db.execute(
            """SELECT content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_valid(content)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (skey,)).fetchone()
        latest = (json.loads(row["content"])
                  if row and row["content"] else {})
        if not isinstance(latest, dict) or latest.get("state") != "open":
            raise _StaleSend("signal_not_open")
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    ids = payload.get("message_ids") or []
    if (not isinstance(ids, list)
            or any(type(mid) is not int or mid <= 0 for mid in ids)):
        raise ValueError("payload_invalid")
    rows = []
    for mid in ids:
        r = ledger.db.execute("""
          SELECT m.*, p.patient_name FROM messages m
          LEFT JOIN patients p ON p.project_id = m.project_id
          WHERE m.message_id=?""", (mid,)).fetchone()
        if r:
            rows.append(r)
    src = payload.get("source", "unread")
    if not rows:
        return f"[MCS] 新着 {len(ids)} 件 (project {ev['project_id']})", []
    att_map = _attachments_map(ledger, [r["message_id"] for r in rows])
    # group replies under their parent when both are new in this event
    by_id = {r["message_id"]: r for r in rows}
    parents = [r for r in rows if not r["parent_id"]
               or r["parent_id"] not in by_id]
    kids = {}
    for r in rows:
        if r["parent_id"] and r["parent_id"] in by_id:
            kids.setdefault(r["parent_id"], []).append(r)

    def _fmt(r, indent=""):
        s_lines = _structured_lines(ledger, r["message_id"])
        urg = _urgency(ledger, r["message_id"])
        body = html_to_text(r["body_html"])
        cap = 500 if s_lines else 600
        if len(body) > cap:
            body = body[:cap] + "…"
        who = r["sender_name"] or "不明"
        meta = " / ".join(x for x in (r["profession"], r["organization"]) if x)
        parent = " (返信)" if r["parent_id"] and not indent else ""
        state = "" if r["body_state"] == "full" else f" [{r['body_state']}]"
        warn = " ⚠️" if urg == "high" else ""
        head = (f"{indent}**{r['patient_name'] or ev['project_id']}**"
                f"{parent}{state}{warn}\n"
                f"{indent}{who}{f' ({meta})' if meta else ''} — "
                f"{r['posted_at'][:16]}")
        atts = att_map.get(r["message_id"], [])
        att_line = ""
        if atts:
            marks = []
            for a in atts:
                nm = str(a["name"] or a["file_id"] or "file")
                if a["state"] == "downloaded":
                    if (a["bytes"] or 0) > _MAX_FILE_BYTES:
                        nm += " (25MB超·未送信)"
                else:
                    nm += " (未取得)"
                marks.append(nm)
            att_line = f"\n{indent}📎 {'、'.join(marks[:5])}"
        if s_lines:
            struct = "\n".join(f"{indent}・{ln}" for ln in s_lines)
            return (f"{head}\n{indent}📋 構造化\n{struct}\n"
                    f"{indent}───── 原文 ─────\n"
                    f"{indent}{body or '(本文なし)'}{att_line}")
        return f"{head}\n{indent}{body or '(本文なし)'}{att_line}"

    def _sem_block(r):
        """Enforce-mode: audited summary section appended to this post.
        Only the newest semantic_summary with audit_status PASS on the
        CURRENT generation qualifies — stale/failed/unaudited ones are
        silently absent, and in shadow/assist/off the notification stays
        byte-identical to pre-Phase-J (INV-16, spec §20.2)."""
        try:
            import semantic as _sem
            from mcs_operations import paused
            if paused(ledger.db, r["project_id"]):
                return ""
            scfg, _ = _sem.semantic_config(_config())
            if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
                return ""
            if scfg["project_ids"] is not None and r["project_id"] not in scfg["project_ids"]:
                return ""
            art = ledger.db.execute(
                "SELECT content,meta FROM artifacts "
                "WHERE kind='semantic_summary' AND message_id=? "
                "ORDER BY artifact_id DESC LIMIT 1",
                (r["message_id"],)).fetchone()
            if not art:
                return ""
            meta = json.loads(art["meta"] or "{}")
            if (meta.get("audit_status") != "PASS" or meta.get("stale")
                    or meta.get("publication_mode") != "enforce"
                    or meta.get("policy_fingerprint") != _sem.policy_fingerprint(scfg)):
                return ""
            try:
                import semantic as _sem
                root = r["parent_id"] or r["message_id"]
                bundle = _sem.thread_bundle(ledger, r["project_id"], root,
                                             [r["message_id"]])
            except Exception:
                return ""
            # The audited summary must describe the current whole-thread
            # generation, not only the target's body revision (INV-15).
            if bundle is None or meta.get("fingerprint") != \
                    bundle["source_fingerprint"]:
                return ""
            target = next((m for m in bundle["members"]
                           if m["message_id"] == r["message_id"]), None)
            if target is None or meta.get("target_revision") != \
                    target["revision"]:
                return ""
            summ = json.loads(art["content"])
            # Keep every audited claim; the existing notifier chunker is the
            # one place that splits a notification.
            lines = [str(c.get("text", ""))
                     for c in summ.get("claims", [])
                     if isinstance(c, dict) and c.get("text")]
            if not lines:
                return ""
            body = "\n".join(f"・{x}" for x in lines)
            lims = [str(x) for x in (summ.get("limitations") or [])
                    if isinstance(x, str)]
            if lims:
                body += "\n・（原文確認）" + "；".join(lims)
            return f"\n───── 要約（自動検査済） ─────\n{body}"
        except Exception:
            return ""   # never let a summary render break delivery

    out = []
    order = []
    for r in parents:
        out.append(_fmt(r) + _sem_block(r))
        order.append(r["message_id"])
        for k in sorted(kids.get(r["message_id"], []),
                        key=lambda x: x["posted_at"]):
            out.append(_fmt(k, "↳ "))
            order.append(k["message_id"])
    head = f"[MCS {src}] 新着 {len(rows)} 件"
    return (head + "\n\n" + "\n\n".join(out),
            _collect_files(att_map, order))


def _semantic_stale(reason: str, in_progress: bool):
    if in_progress:
        raise _FreezeSend(reason)
    raise _StaleSend(reason)


def _semantic_summary_current(ledger, project_id: int, message_id: int,
                              bundle: dict,
                              fingerprint: str, policy: str) -> bool:
    """Require the frozen notice to still have its current PASS summary."""
    row = ledger.db.execute(
        "SELECT content,meta FROM artifacts "
        "WHERE kind='semantic_summary' AND project_id=? AND message_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (project_id, message_id)).fetchone()
    if row is None:
        return False
    try:
        content = json.loads(row["content"] or "null")
        meta = json.loads(row["meta"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(content, dict) or not isinstance(meta, dict):
        return False
    target = next((m for m in bundle.get("members", [])
                   if m["message_id"] == message_id), None)
    return (meta.get("audit_status") == "PASS"
            and not meta.get("stale")
            and meta.get("publication_mode") == "enforce"
            and meta.get("policy_fingerprint") == policy
            and meta.get("fingerprint") == fingerprint
            and target is not None
            and meta.get("target_revision") == target["revision"])


def _semantic_source_event(ledger, project_id: int, event_id: int):
    """Load an origin event only inside the semantic notice's project."""
    row = ledger.db.execute(
        "SELECT state,attempts,progress,payload FROM notify_outbox "
        "WHERE event_id=? AND kind='new_messages' AND project_id=?",
        (event_id, project_id)).fetchone()
    if row is None:
        return None, None
    try:
        source = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return row, None
    return row, source if isinstance(source, dict) else None


def _semantic_gate(ledger, ev, payload: dict, in_progress: bool = False):
    """Recheck a semantic intent immediately before every external post."""
    import semantic as _sem
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    scfg, _ = _sem.semantic_config(_config())
    if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
        if in_progress:
            raise _FreezeSend("semantic_not_enforce")
        raise _DeferredSend("semantic_not_enforce")
    project_id = ev["project_id"]
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("payload_invalid")
    from mcs_operations import paused
    if paused(ledger.db, project_id):
        raise _FreezeSend("semantic_paused") if in_progress else _DeferredSend("semantic_paused")
    if scfg["project_ids"] is not None and project_id not in scfg["project_ids"]:
        raise _FreezeSend("project_out_of_scope") if in_progress \
            else _DeferredSend("project_out_of_scope")
    policy_version = payload.get("policy_version")
    if not isinstance(policy_version, str) \
            or policy_version != _sem.POLICY_VERSION:
        _semantic_stale("policy_changed", in_progress)
    degraded = payload.get("degraded", False)
    if type(degraded) is not bool:
        raise ValueError("payload_invalid")
    policy = payload.get("policy_fingerprint")
    if not isinstance(policy, str) \
            or policy != _sem.policy_fingerprint(scfg):
        _semantic_stale("policy_changed", in_progress)
    fp = payload.get("fingerprint")
    root = payload.get("root_id")
    src = payload.get("src_event_id")
    if not isinstance(fp, str) or not fp or type(root) is not int \
            or root <= 0 or type(src) is not int or src <= 0:
        raise ValueError("payload_invalid")
    source_row, source = _semantic_source_event(ledger, project_id, src)
    if source_row is None or source is None:
        _semantic_stale("src_event_ineligible", in_progress)
    try:
        source_ids = source.get("message_ids")
        if (not isinstance(source_ids, list)
                or any(type(mid) is not int or mid <= 0 for mid in source_ids)):
            raise ValueError
        source_ids = set(source_ids)
    except (AttributeError, ValueError):
        _semantic_stale("src_event_ineligible", in_progress)
    if source_row["state"] == "suppressed":
        _semantic_stale("src_event_ineligible", in_progress)
    if degraded:
        target_ids = payload.get("target_message_ids")
        if (not isinstance(target_ids, list) or not target_ids
                or any(type(mid) is not int or mid <= 0 for mid in target_ids)
                or len(set(target_ids)) != len(target_ids)
                or not set(target_ids).issubset(source_ids)):
            raise ValueError("payload_invalid")
        try:
            progress = json.loads(source_row["progress"] or "{}")
        except (json.JSONDecodeError, TypeError):
            progress = None
        if (source_row["state"] == "accepted"
                or source_row["attempts"] > 0
                or not isinstance(progress, dict)
                or bool(progress.get("sent"))):
            _semantic_stale("base_delivered", in_progress)
        bundle = _sem.thread_bundle(ledger, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _semantic_stale("stale_generation", in_progress)
        member_ids = {m["message_id"] for m in bundle["members"]}
        if not set(target_ids).issubset(member_ids):
            _semantic_stale("stale_generation", in_progress)
    else:
        target = payload.get("target_message_id")
        target_revision = payload.get("target_revision")
        if (type(target) is not int or target <= 0
                or not isinstance(target_revision, str) or not target_revision
                or target not in source_ids):
            raise ValueError("payload_invalid")
        bundle = _sem.thread_bundle(ledger, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _semantic_stale("stale_generation", in_progress)
        member = next((m for m in bundle["members"]
                       if m["message_id"] == target), None)
        if member is None or member["revision"] != target_revision:
            _semantic_stale("stale_generation", in_progress)
        if not _semantic_summary_current(ledger, project_id, target, bundle,
                                         fp, policy):
            _semantic_stale("summary_stale", in_progress)


def _semantic_render_state(ledger, ev) -> tuple:
    """Current identities of audited summaries attached to a raw notice."""
    import semantic as _sem
    from mcs_operations import paused
    if paused(ledger.db, ev["project_id"]):
        return ()
    scfg, _ = _sem.semantic_config(_config())
    if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
        return ()
    if scfg["project_ids"] is not None and ev["project_id"] not in scfg["project_ids"]:
        return ()
    project_id = ev["project_id"]
    if type(project_id) is not int or project_id <= 0:
        return ()
    try:
        payload = json.loads(ev["payload"])
        ids = payload.get("message_ids") or []
        import semantic as _sem
    except Exception:
        return ()
    state = []
    for mid in ids if isinstance(ids, list) else []:
        if type(mid) is not int:
            continue
        row = ledger.db.execute(
            "SELECT project_id,message_id,parent_id,content_hash "
            "FROM messages WHERE project_id=? AND message_id=?",
            (project_id, mid)).fetchone()
        if row is None:
            continue
        art = ledger.db.execute(
            "SELECT artifact_id,content,meta FROM artifacts "
            "WHERE kind='semantic_summary' AND project_id=? "
            "AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
            (project_id, mid)).fetchone()
        if art is None:
            continue
        try:
            meta = json.loads(art["meta"] or "{}")
            root = row["parent_id"] or row["message_id"]
            bundle = _sem.thread_bundle(ledger, row["project_id"], root, [mid])
        except Exception:
            continue
        target = next((m for m in (bundle or {}).get("members", [])
                       if m["message_id"] == mid), None)
        if (bundle is None or meta.get("audit_status") != "PASS"
                or meta.get("stale")
                or meta.get("publication_mode") != "enforce"
                or meta.get("policy_fingerprint") != _sem.policy_fingerprint(scfg)
                or meta.get("fingerprint") != bundle["source_fingerprint"]
                or target is None
                or meta.get("target_revision") != target["revision"]):
            continue
        state.append((mid, art["artifact_id"], meta["fingerprint"]))
    return tuple(state)


def _semantic_render_gate(ledger, ev, initial: tuple,
                          in_progress: bool = False):
    if not initial:
        return
    cfg = _config().get("semantic")
    current = _semantic_render_state(ledger, ev)
    if not isinstance(cfg, dict) or cfg.get("mode") != "enforce":
        if in_progress:
            raise _FreezeSend("semantic_not_enforce")
        raise _DeferredSend("semantic_not_enforce")
    if current != initial:
        raise _FreezeSend("semantic_generation_changed") if in_progress \
            else _StaleSend("semantic_generation_changed")


def _semantic_chunks(content: str) -> list[str]:
    """Split a frozen semantic notice while repeating its provenance.

    ``semantic.render_notice`` already puts the patient, coverage/audit
    labels, and stored MCS URL in the frozen payload. A raw character slice
    would leave those lines only in part 1, so each part gets the same frozen
    header and link. The payload is never rebuilt from the ledger here.
    """
    lines = content.splitlines()
    footer_index = next(
        (i for i, line in enumerate(lines) if line == "▶ MCSで確認"),
        None,
    )
    link_index = next(
        (i for i, line in enumerate(lines[footer_index + 1:], footer_index + 1)
         if line.startswith("https://")),
        None,
    ) if footer_index is not None else None
    if link_index is None or footer_index is None:
        raise ValueError("semantic_provenance_missing")
    link = lines[link_index].strip()
    if not link.startswith("https://"):
        raise ValueError("semantic_provenance_missing")

    # The renderer's leading block is stable and ends at the first blank line
    # or section. Keep every line in that block so target time and any
    # degraded-notice explanation remain visible on every part.
    header_end = len(lines)
    for index, line in enumerate(lines[:footer_index]):
        if not line.strip() or line.startswith("■"):
            header_end = index
            break
    header = [line for line in lines[:header_end] if line.strip()]
    if not any(line.startswith("取得：") for line in header):
        raise ValueError("semantic_coverage_missing")
    if not any(line.startswith("要約：") for line in header):
        raise ValueError("semantic_audit_missing")

    # Remove the original header/footer before reattaching them to every
    # chunk. This keeps the frozen claims intact without duplicating the
    # first block inside part 1.
    body_start = header_end
    while body_start < footer_index and not lines[body_start].strip():
        body_start += 1
    body_lines = lines[body_start:footer_index]
    body = "\n".join(body_lines).strip()

    footer = ["▶ MCSで確認", link]
    static = "\n".join(header + footer)
    count = 1
    chunks = []
    for _ in range(20):
        marker = f"part {count}/{count}"
        overhead = len(static) + len(marker) + 3
        capacity = _MAX_LEN - overhead
        if capacity < 1:
            raise ValueError("semantic_provenance_too_long")
        chunks = [body[i:i + capacity] for i in range(0, len(body), capacity)]
        if not chunks:
            chunks = [""]
        next_count = len(chunks)
        if next_count == count:
            break
        count = next_count
    else:
        raise ValueError("semantic_chunking_failed")

    out = []
    for index, chunk in enumerate(chunks, 1):
        marker = f"part {index}/{count}"
        part = "\n".join(header + [marker, chunk, *footer])
        if len(part) > _MAX_LEN:
            raise ValueError("semantic_chunk_limit")
        out.append(part)
    return out


def _has_sent_progress(ev) -> bool:
    try:
        progress = json.loads(ev["progress"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return type(progress.get("next")) is int and progress["next"] > 0


# The bot token rides the Authorization header — a redirect anywhere
# (even same-host path change is unneeded for this API) must never be
# followed with credentials attached (Oracle B16). NoRedirect + the
# no-proxy opener come from mcs_util.
_OPENER = no_proxy_opener(NoRedirect)


def _multipart(payload: dict, files: list[tuple[str, str]]) -> tuple[bytes, str]:
    """multipart/form-data body for Discord file upload. payload_json carries
    the JSON body; files[i] parts carry raw bytes. Original (possibly
    non-ASCII) filename goes in payload_json attachments; the Content-
    Disposition filename is an ASCII-safe stand-in."""
    boundary = f"mcs-{uuid.uuid4().hex}"
    out = bytearray()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; "
            f"name=\"payload_json\"\r\nContent-Type: application/json"
            f"\r\n\r\n").encode()
    out += json.dumps(payload).encode() + b"\r\n"
    for i, (fname, path) in enumerate(files):
        ext = os.path.splitext(fname)[1] or ".bin"
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"files[{i}]\"; filename=\"file{i}{ext}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode()
        with open(path, "rb") as f:
            data = f.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError("file_too_large")
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    if len(out) > _MAX_REQUEST_BYTES:
        raise ValueError("multipart_too_large")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _post(token: str, channel: str, content: str,
          files: list[tuple[str, str]] | None = None) -> str:
    """Returns Discord message id. Raises on failure."""
    payload = {"content": content,
               # message text must never ping @everyone/users
               "allowed_mentions": {"parse": []}}
    if files:
        payload["attachments"] = [{"id": i, "filename": fn}
                                  for i, (fn, _) in enumerate(files)]
        body, ctype = _multipart(payload, files)
        headers = {"Authorization": f"Bot {token}",
                   "User-Agent": "DiscordBot (mcs-adapter, 1.0)",
                   "Content-Type": ctype}
        timeout = 60
    else:
        body = json.dumps(payload).encode()
        headers = {"Authorization": f"Bot {token}",
                   "User-Agent": "DiscordBot (mcs-adapter, 1.0)",
                   "Content-Type": "application/json"}
        timeout = 15
    req = urllib.request.Request(
        f"{API}/channels/{channel}/messages",
        data=body, headers=headers, method="POST")
    with _OPENER.open(req, timeout=timeout) as res:
        reply = json.load(res)
    mid = reply.get("id") if isinstance(reply, dict) else None
    if ((type(mid) is int and mid > 0)
            or (isinstance(mid, str) and mid.isdigit())):
        return str(mid)
    raise ValueError("discord_receipt_invalid")


def _retry_after(e: urllib.error.HTTPError) -> float:
    """Discord 429 carries the real wait; default bounded backoff."""
    try:
        d = json.loads(e.read() or b"{}")
        wait = float(d.get("retry_after", 0))
        return wait + 1 if math.isfinite(wait) and wait >= 0 else 60.0
    except (ValueError, TypeError):
        return 60.0


def _delivery_fingerprint(channel: str, chunks: list[str],
                          files: list[tuple[str, str]]) -> str:
    h = hashlib.sha256(channel.encode())
    for chunk in chunks:
        h.update(b"\0text\0")
        h.update(chunk.encode())
    for name, path in files:
        h.update(b"\0file\0")
        h.update(name.encode())
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    return h.hexdigest()


def _progress(raw: str, count: int) -> tuple[int, list[str], str | None]:
    try:
        d = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise ValueError("progress_invalid") from e
    if not isinstance(d, dict) or type(d.get("next", 0)) is not int:
        raise ValueError("progress_invalid")
    next_chunk = d.get("next", 0)
    sent = d.get("sent", [])
    fingerprint = d.get("fingerprint")
    if (next_chunk < 0 or next_chunk > count or not isinstance(sent, list)
            or len(sent) != next_chunk
            or any(not isinstance(x, str) or not x.isdigit() for x in sent)
            or (fingerprint is not None and not isinstance(fingerprint, str))):
        raise ValueError("progress_invalid")
    return next_chunk, sent, fingerprint


def flush(ledger, limit: int = 10, deadline: float | None = None) -> dict:
    token = _token()
    res = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0,
           "parked": 0}
    due = ledger.outbox_due(limit)
    if not token:
        res["skipped"] = len(due)
        return res
    for event_index, ev in enumerate(due):
        if deadline is not None and time.monotonic() >= deadline:
            res["skipped"] += len(due) - event_index
            break
        if ev["project_id"] and ledger.is_archived(ev["project_id"]):
            # queued before the patient was archived — archived patient
            # events must never reach Discord; drop terminally, do not
            # count as sent OR as a retryable failure (Oracle F2)
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
            continue
        channel = _channel_id(ev["kind"])
        if not channel:
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
            res["failed"] += 1
            continue
        try:
            content, files = _format_event(ledger, ev)
            render_state = (_semantic_render_state(ledger, ev)
                            if ev["kind"] == "new_messages" else ())
            chunks = (_semantic_chunks(content)
                      if ev["kind"] == "semantic_notice" else
                      [content[i:i + _MAX_LEN]
                       for i in range(0, len(content), _MAX_LEN)])
            # resume at the first unacknowledged chunk — a crash after a
            # partial send must not duplicate accepted chunks (B25)
            start, sent_ids, previous = _progress(ev["progress"], len(chunks))
            fingerprint = _delivery_fingerprint(channel, chunks, files)
            if start and previous != fingerprint:
                ledger.outbox_hold(ev["event_id"])
                res["failed"] += 1
                continue
            if not start:
                ledger.outbox_progress(ev["event_id"], 0, [], fingerprint)
            mid = ""
            post_files = files
            for i in range(start, len(chunks)):
                if deadline is not None and time.monotonic() >= deadline:
                    res["skipped"] += len(due) - event_index
                    return res
                if ev["kind"] == "semantic_notice":
                    try:
                        payload = json.loads(ev["payload"])
                    except (json.JSONDecodeError, TypeError) as e:
                        raise ValueError("payload_invalid") from e
                    _semantic_gate(ledger, ev, payload,
                                   in_progress=bool(start or i))
                elif render_state:
                    _semantic_render_gate(ledger, ev, render_state,
                                          in_progress=bool(start or i))
                # files ride the FIRST post only; on resume (start>0) they
                # were already delivered with chunk 0. A definitive
                # rejection of the file-bearing post (local size/format
                # error, or Discord 4xx — never 429/5xx/network where
                # acceptance is unknown) drops the files and retries
                # text-only so a bad attachment can never sink the
                # notification itself
                try:
                    mid = _post(token, channel, chunks[i],
                                post_files if i == 0 else None)
                except ValueError as e:
                    # only errors raised while BUILDING the body — a
                    # post-send ValueError (receipt_invalid) may mean
                    # the message already reached Discord
                    if (i != 0 or not post_files or str(e) not in
                            ("file_too_large", "multipart_too_large")):
                        raise
                    post_files = None
                    if ev["kind"] == "semantic_notice":
                        _semantic_gate(ledger, ev, payload,
                                       in_progress=bool(start or i))
                    elif render_state:
                        _semantic_render_gate(ledger, ev, render_state,
                                              in_progress=bool(start or i))
                    mid = _post(token, channel, chunks[i])
                except urllib.error.HTTPError as e:
                    if (i != 0 or not post_files or e.code == 429
                            or e.code < 400 or e.code >= 500):
                        raise
                    post_files = None
                    if ev["kind"] == "semantic_notice":
                        _semantic_gate(ledger, ev, payload,
                                       in_progress=bool(start or i))
                    elif render_state:
                        _semantic_render_gate(ledger, ev, render_state,
                                              in_progress=bool(start or i))
                    mid = _post(token, channel, chunks[i])
                sent_ids.append(mid)
                ledger.outbox_progress(ev["event_id"], i + 1, sent_ids,
                                       fingerprint)
            ledger.outbox_mark(ev["event_id"], "accepted",
                               sent_ids[-1] if sent_ids else "")
            res["sent"] += 1
        except _DeferredSend:
            # stays pending but re-checks hourly, not every flush. A
            # parked enforce intent is policy-blocked, not incomplete —
            # count it separately so run_check's notify_incomplete signal
            # doesn't flag every other tick while the mode gate is down
            if _has_sent_progress(ev):
                ledger.outbox_hold(ev["event_id"])
                res["failed"] += 1
            else:
                ledger.db.execute(
                    "UPDATE notify_outbox SET next_try=?,updated_at=? "
                    "WHERE event_id=?",
                    (time.time() + 3600, time.time(), ev["event_id"]))
                ledger.db.commit()
                res["parked"] += 1
        except _FreezeSend:
            # An accepted chunk is immutable. Keep its receipt/progress and
            # freeze the remaining chunks until an explicit retry decision.
            ledger.outbox_hold(ev["event_id"])
            res["failed"] += 1
        except _StaleSend:
            if _has_sent_progress(ev):
                ledger.outbox_hold(ev["event_id"])
                res["failed"] += 1
            else:
                ledger.outbox_suppress(ev["event_id"])
                res["suppressed"] += 1
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = _retry_after(e)
            elif e.code >= 500:
                retry = 60
            elif e.code in (401, 403, 404):
                retry = 3600  # config/permission issue — slow retry
            else:
                retry = min(3600, 60 * (2 ** ev["attempts"]))
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=retry)
            res["failed"] += 1
            if e.code == 429:
                break
        except ValueError:
            ledger.outbox_hold(ev["event_id"])
            res["failed"] += 1
        except (urllib.error.URLError, TimeoutError, KeyError, OSError):
            backoff = min(3600, 60 * (2 ** ev["attempts"]))
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=backoff)
            res["failed"] += 1
    return res
