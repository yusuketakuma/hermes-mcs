---
type: architecture
title: Structured Extraction and Semantic Pipeline
description: How stored messages become structured data — rule-based extract_v1, local-LLM extract_llm (v4), the semantic drain and its send-time gate, and the RT/BACKLOG admission broker for the shared local LLM.
tags: [extraction, semantic, llm, admission, notify-gate]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-600c5f4ad283cef60ff89dc7
    resource: repo://mcs/core/llm_admission.py
  - id: openwiki-source-af419fe24174cb1e853ad691
    resource: repo://mcs/core/local_llm.py
  - id: openwiki-source-016a755270f707e9bd9ffcf9
    resource: repo://mcs/extract/v1/extract.py
  - id: openwiki-source-9485636b21c8a1307f8e363c
    resource: repo://mcs/extract/v4/extract_llm.py
  - id: openwiki-source-2d05be334ece5a5247d593cb
    resource: repo://mcs/semantic/semantic_drain.py
  - id: openwiki-source-a454f85deb2460d866ec227d
    resource: repo://mcs/semantic/semantic_send_gate.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Structured Extraction and Semantic Pipeline

All extraction runs locally. Nothing in this pipeline sends message content to an external service.

## Layers

| Layer | Module | Output |
|---|---|---|
| Rules | `mcs/extract/v1/extract.py` | artifact kind `extract_v1` |
| Local LLM | `mcs/extract/v4/extract_llm.py` | artifact kind `extract_llm` |
| Semantic drain | `mcs/semantic/semantic_drain.py` | facts, plans, audited summaries |
| Send gate | `mcs/semantic/semantic_send_gate.py` | verdicts applied by `notify_flush` |

**Rule extraction (v1)** parses visit-date headers, medication periods, SOAP sections, vitals and speaker labels into a JSON artifact. It is the cheap baseline that timelines, search and rollups reuse.

**LLM extraction (v4)** adds what rules cannot do: dose-less medication names, negated symptoms, request targets by profession, and a one-line summary. Rule and model outputs are stored side by side in `artifacts`; neither overwrites the other, so provenance is kept. Older generations (`v2`, `v3`) were replaced in place and survive only in git history.

`run_pending()` extracts only messages that lack a current `extract_llm` artifact, tracked by `content_hash`. `run_check` calls it with a time budget on each tick, so a backlog drains gradually. A message is retried when the artifact is thin or when Jev QC flagged it; the QC fix settles once via `meta.qc_fix`. `_llm_up()` probes the endpoint first, so a dead server ends the lane instead of burning the budget per message.

## Local LLM transport

`mcs/core/local_llm.py` is the single stdlib-only transport shared by the semantic path and the legacy `extract_llm` path. The endpoint is loopback (`127.0.0.1:8080`, OpenAI-compatible). The transport has no proxy, no redirects, a bounded response size and absolute deadlines. It returns `finish_reason` and token usage so each caller applies its own acceptance policy: canonical semantic callers reject `length` stops and empty or malformed structured output.

## Semantic drain

`semantic_drain.run_due()` drains durable semantic jobs inside the tick's remaining time budget.

- **Mode gate:** when `mode` is `off` it returns immediately, creating no jobs and making no external calls.
- **Config re-validation:** the config is re-checked between jobs, so switching to `off` mid-run takes effect at the next job boundary.
- **Stability guards:** a nearly-full volume or an open endpoint circuit breaker stops the lane rather than turning every job into an error.
- **Job size:** `max_jobs` must be an int between 1 and 32.

A job goes through staged fact extraction, QC and audit (with Jev as the checker). Results are written under a policy fingerprint, and a result is parked as "needs review" rather than published when audit does not clear it. The module has its own CLI `main()` for standalone drains.

## Send-time gate

Notifications that carry semantic content are re-verified immediately before each external post and between chunks. `semantic_gate` rechecks the mode, pause state, project scope, policy version and fingerprint, source-event eligibility and the current generation. It reports its verdict through three exceptions, which `notify_flush` catches:

- `DeferredSend` — not deliverable under current policy. The intent stays pending for a later flush and is never sent past the gate.
- `StaleSend` — the source generation moved on. The intent is suppressed terminally rather than publishing stale results.
- `FreezeSend` — an intent changed after a chunk was already accepted. Receipt and progress are kept and the remaining chunks are quarantined.

`semantic_chunk_parts` repeats the provenance header and footer on every part of a split notice. `semantic_summary_block` appends an audited summary to a raw post, in enforce mode only. This module never imports `notify_flush`; the caller passes the resolved config.

## LLM admission broker

`mcs/core/llm_admission.py` guards the shared llama.cpp backend so that background work and interactive work never overlap. The invariant is `O_BACKLOG * O_RT = 0`, counting every authorized request from pre-send reservation to confirmed terminal retirement.

- **Class comes from the route.** A client authenticates as a registered route (`mcs.extract`, `mcs.semantic`, `mcs.qc` and `mcs.bench` are BACKLOG; `hermes.interactive` and `gbrain.query` are RT). A caller-supplied class is never trusted.
- **Permit lifecycle:** `reserved → admitted → sent → terminal`, with the honest side states `waiting`, `unknown` and `cancel_pending`. Timeouts, socket drops and idle `/slots` samples are never treated as proof that the backend is free.
- **Durable epoch.** A broker restart with a non-terminal permit rotates the epoch and stays closed until the backend is known empty. Old-epoch permits are fenced.
- **Single-use admission token** (`epoch:permit:nonce`) per admitted→sent permit.
- **Default off.** Call paths use the broker only when `MCS_LLM_ADMISSION` is set. Otherwise the legacy slot-pinning path applies.

## Related

<!-- openwiki: broken internal link [/openwiki/architecture/ledger-storage.md] link "/openwiki/architecture/ledger-storage.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- [Ledger storage](/openwiki/architecture/ledger-storage.md) holds `artifacts` and the semantic stores.
<!-- openwiki: broken internal link [/openwiki/architecture/notification-delivery.md] link "/openwiki/architecture/notification-delivery.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- [Notification and delivery](/openwiki/architecture/notification-delivery.md) consumes the send gate.
