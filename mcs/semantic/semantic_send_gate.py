"""Send-time semantic gate — last-moment policy re-verification.

Separated from notify_flush: delivery (notify_outbox drain + `hermes
send`) lives there, while the semantic send-time POLICY lives here. The
gates re-verify a frozen intent immediately before each external post
and between chunks (spec §18.4/§22.1):

- ``semantic_gate`` rechecks a semantic_notice's mode, pause state,
  project scope, policy version/fingerprint, source-event eligibility,
  and the current generation fingerprint.
- ``semantic_render_state`` / ``semantic_render_gate`` snapshot and
  re-verify the audited-summary identities attached to a raw
  new_messages notice so a summary change mid-flight freezes the rest.
- ``semantic_summary_block`` renders the audited summary section
  appended to a raw post (enforce mode only).
- ``semantic_chunks`` splits a frozen semantic notice while repeating
  its provenance header/footer on every part (``semantic_chunk_parts``
  also returns each part's body slice for delivery attribution).

The verdict exceptions (DeferredSend / StaleSend / FreezeSend) are the
gate's vocabulary; notify_flush catches them to park, suppress, or
quarantine the intent. This module never imports notify_flush — the
caller passes the resolved config dict (and the text cap for chunks) so
tests keep notify_flush._config as the single patch point.
"""
import json
from mcs_util import loads_dict


class DeferredSend(Exception):
    """Not deliverable under CURRENT policy (e.g. semantic mode left
    enforce) — the committed intent stays pending for a later flush;
    it is not destroyed (INV-23) and never sent past its gate."""


class StaleSend(Exception):
    """The source generation this intent was built for has moved on —
    suppress terminally instead of publishing stale results (§18.4)."""


class FreezeSend(Exception):
    """A semantic intent changed after a chunk was accepted — preserve its
    receipt/progress and quarantine the remaining chunks."""


def _stale(reason: str, in_progress: bool):
    if in_progress:
        raise FreezeSend(reason)
    raise StaleSend(reason)


def _summary_current(ledger, project_id: int, message_id: int,
                     bundle, policy: str):
    """The newest semantic_summary for this message when it is publishable
    on the CURRENT generation (PASS, not stale, enforce, same policy,
    whole-thread fingerprint and target revision), else None. The single
    predicate shared by the send gate, render state and summary block.
    Returns ``{artifact_id, content, meta}``. ``bundle`` may be a dict or
    a zero-arg callable resolved only after the cheap row/meta checks pass
    (thread_bundle reads the whole thread); a raising callable -> None."""
    row = ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts "
        "WHERE kind='semantic_summary' AND project_id=? AND message_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (project_id, message_id)).fetchone()
    if row is None:
        return None
    content, meta = loads_dict(row["content"]), loads_dict(row["meta"])
    if not isinstance(content, dict) or not isinstance(meta, dict):
        return None
    if (meta.get("audit_status") != "PASS"
            or meta.get("stale")
            or meta.get("publication_mode") != "enforce"
            or meta.get("policy_fingerprint") != policy):
        return None
    if callable(bundle):
        try:
            bundle = bundle()
        except Exception:
            return None
    if not isinstance(bundle, dict):
        return None
    target = next((m for m in bundle.get("members", [])
                   if m["message_id"] == message_id), None)
    if (meta.get("fingerprint") == bundle.get("source_fingerprint")
            and target is not None
            and meta.get("target_revision") == target["revision"]):
        return {"artifact_id": row["artifact_id"], "content": content,
                "meta": meta}
    return None


def _source_event(ledger, project_id: int, event_id: int):
    """Load an origin event only inside the semantic notice's project."""
    row = ledger.db.execute(
        "SELECT state,attempts,progress,payload FROM notify_outbox "
        "WHERE event_id=? AND kind='new_messages' AND project_id=?",
        (event_id, project_id)).fetchone()
    if row is None:
        return None, None
    source = loads_dict(row["payload"])
    return row, source if isinstance(source, dict) else None


def semantic_gate(ledger, ev, payload: dict, cfg: dict,
                  in_progress: bool = False):
    """Recheck a semantic intent immediately before every external post."""
    import semantic as _sem
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    scfg, _ = _sem.semantic_config(cfg)
    if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
        if in_progress:
            raise FreezeSend("semantic_not_enforce")
        raise DeferredSend("semantic_not_enforce")
    project_id = ev["project_id"]
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("payload_invalid")
    from mcs_operations import paused
    if paused(ledger.db, project_id):
        raise FreezeSend("semantic_paused") if in_progress \
            else DeferredSend("semantic_paused")
    if scfg["project_ids"] is not None and project_id not in scfg["project_ids"]:
        raise FreezeSend("project_out_of_scope") if in_progress \
            else DeferredSend("project_out_of_scope")
    policy_version = payload.get("policy_version")
    if not isinstance(policy_version, str) \
            or policy_version != _sem.POLICY_VERSION:
        _stale("policy_changed", in_progress)
    degraded = payload.get("degraded", False)
    if type(degraded) is not bool:
        raise ValueError("payload_invalid")
    policy = payload.get("policy_fingerprint")
    if not isinstance(policy, str) \
            or policy != _sem.policy_fingerprint(scfg):
        _stale("policy_changed", in_progress)
    fp = payload.get("fingerprint")
    root = payload.get("root_id")
    src = payload.get("src_event_id")
    if not isinstance(fp, str) or not fp or type(root) is not int \
            or root <= 0 or type(src) is not int or src <= 0:
        raise ValueError("payload_invalid")
    source_row, source = _source_event(ledger, project_id, src)
    if source_row is None or source is None:
        _stale("src_event_ineligible", in_progress)
    try:
        source_ids = source.get("message_ids")
        if (not isinstance(source_ids, list)
                or any(type(mid) is not int or mid <= 0 for mid in source_ids)):
            raise ValueError
        source_ids = set(source_ids)
    except (AttributeError, ValueError):
        _stale("src_event_ineligible", in_progress)
    if source_row["state"] == "suppressed":
        _stale("src_event_ineligible", in_progress)
    if degraded:
        target_ids = payload.get("target_message_ids")
        if (not isinstance(target_ids, list) or not target_ids
                or any(type(mid) is not int or mid <= 0 for mid in target_ids)
                or len(set(target_ids)) != len(target_ids)
                or not set(target_ids).issubset(source_ids)):
            raise ValueError("payload_invalid")
        progress = loads_dict(source_row["progress"] or "{}")
        if (source_row["state"] == "accepted"
                or source_row["attempts"] > 0
                or not isinstance(progress, dict)
                or bool(progress.get("sent"))):
            _stale("base_delivered", in_progress)
        bundle = _sem.thread_bundle(ledger, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _stale("stale_generation", in_progress)
        member_ids = {m["message_id"] for m in bundle["members"]}
        if not set(target_ids).issubset(member_ids):
            _stale("stale_generation", in_progress)
    else:
        target = payload.get("target_message_id")
        target_revision = payload.get("target_revision")
        if (type(target) is not int or target <= 0
                or not isinstance(target_revision, str) or not target_revision
                or target not in source_ids):
            raise ValueError("payload_invalid")
        bundle = _sem.thread_bundle(ledger, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _stale("stale_generation", in_progress)
        member = next((m for m in bundle["members"]
                       if m["message_id"] == target), None)
        if member is None or member["revision"] != target_revision:
            _stale("stale_generation", in_progress)
        if _summary_current(ledger, project_id, target, bundle,
                            policy) is None:
            _stale("summary_stale", in_progress)


def semantic_render_state(ledger, ev, cfg: dict) -> tuple:
    """Current identities of audited summaries attached to a raw notice."""
    import semantic as _sem
    from mcs_operations import paused
    if paused(ledger.db, ev["project_id"]):
        return ()
    scfg, _ = _sem.semantic_config(cfg)
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
        root = row["parent_id"] or row["message_id"]
        cur = _summary_current(
            ledger, project_id, mid,
            lambda r=root, m=mid: _sem.thread_bundle(
                ledger, project_id, r, [m]),
            _sem.policy_fingerprint(scfg))
        if cur is None:
            continue
        state.append((mid, cur["artifact_id"], cur["meta"]["fingerprint"]))
    return tuple(state)


def semantic_render_gate(ledger, ev, initial: tuple, cfg: dict,
                         in_progress: bool = False):
    if not initial:
        return
    sem_cfg = cfg.get("semantic")
    current = semantic_render_state(ledger, ev, cfg)
    if not isinstance(sem_cfg, dict) or sem_cfg.get("mode") != "enforce":
        if in_progress:
            raise FreezeSend("semantic_not_enforce")
        raise DeferredSend("semantic_not_enforce")
    if current != initial:
        raise FreezeSend("semantic_generation_changed") if in_progress \
            else StaleSend("semantic_generation_changed")


def semantic_chunks(content: str, max_len: int) -> list[str]:
    """Split a frozen semantic notice while repeating its provenance."""
    return semantic_chunk_parts(content, max_len)[0]


def semantic_chunk_parts(content: str,
                         max_len: int) -> tuple[list[str], list[str]]:
    """``(parts, bodies)``: the sent parts plus the body slice each part
    carries between its repeated header/marker and footer — the bodies
    concatenate back to the notice body, so readers attribute lines to
    parts without re-parsing the rendered text.

    ``semantic.render_notice`` already puts the patient, coverage/audit
    labels, and stored MCS URL in the frozen payload. A raw character slice
    would leave those lines only in part 1, so each part gets the same frozen
    header and link. The payload is never rebuilt from the ledger here.
    """
    lines = content.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    # Only the renderer's terminal footer is structural. An evidence quote
    # or model claim may contain the same marker/link inside the body.
    if len(lines) < 2 or lines[-2] != "▶ MCSで確認":
        raise ValueError("semantic_provenance_missing")
    footer_index = len(lines) - 2
    link = lines[-1].strip()
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
        capacity = max_len - overhead
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
        if len(part) > max_len:
            raise ValueError("semantic_chunk_limit")
        out.append(part)
    return out, chunks


def semantic_summary_block(ledger, r, cfg: dict) -> str:
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
        scfg, _ = _sem.semantic_config(cfg)
        if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
            return ""
        if scfg["project_ids"] is not None and r["project_id"] not in scfg["project_ids"]:
            return ""
        root = r["parent_id"] or r["message_id"]
        # The audited summary must describe the current whole-thread
        # generation, not only the target's body revision (INV-15).
        cur = _summary_current(
            ledger, r["project_id"], r["message_id"],
            lambda: _sem.thread_bundle(ledger, r["project_id"], root,
                                       [r["message_id"]]),
            _sem.policy_fingerprint(scfg))
        if cur is None:
            return ""
        summ = cur["content"]
        # Keep every audited claim; the send path's chunker is the one
        # place that splits a notification.
        lines = [str(c.get("text", ""))
                 for c in summ.get("claims", [])
                 if isinstance(c, dict) and c.get("text")]
        mfacts = [x for x in (summ.get("mandatory_facts") or [])
                  if isinstance(x, str) and x]
        if not lines and not mfacts:
            return ""
        body = "\n".join(f"・{x}" for x in lines)
        if mfacts:
            if body:
                body += "\n"
            body += "■ 抽出済み事実（監査済み）\n" + "\n".join(f"・{x}" for x in mfacts)
        lims = [str(x) for x in (summ.get("limitations") or [])
                if isinstance(x, str)]
        if lims:
            body += "\n・（原文確認）" + "；".join(lims)
        return f"\n───── 要約（自動検査済） ─────\n{body}"
    except Exception:
        return ""   # never let a summary render break delivery
