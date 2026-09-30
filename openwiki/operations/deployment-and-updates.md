---
type: operations
title: Deployment, LaunchAgents and Self-Update
description: How hermes-mcs is installed and scheduled on macOS (install.sh, hermes cron, launchd), how the interrupted-update recovery watchdog lives outside the repo, and the mcs_update check/apply/rollback/recover lifecycle.
tags: [deployment, launchd, hermes-cron, install, self-update, recovery]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-49837f3aa555fc8d2342a72d
    resource: repo://deployment/README.md
  - id: openwiki-source-bb655f2b77b3cbc3bf5f8fe5
    resource: repo://deployment/recovery/mcs_recover.py
  - id: openwiki-source-03ffc32a0ca502ab67c54b25
    resource: repo://install.sh
  - id: openwiki-source-c87c59465e2963429e1874e1
    resource: repo://mcs/ops/mcs_update.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Deployment, LaunchAgents and Self-Update

`deployment/` holds the source of truth for how the system runs on a machine. Editing it changes nothing live: templates carry placeholders (`__PYTHON__`, `__REPO__`, `__DATA__`) that `mcs/ops/mcs_setup.py services` or `install.sh` render into real locations. Hand-editing the rendered copies causes drift from the repo.

## Install

`install.sh` is idempotent and runs in stages: Homebrew packages, the hermes-agent checkout and venv, the Discord command plugin (symlink plus `hermes plugins enable`), the local LLM (llama-server on `127.0.0.1:8080`), scheduled services (delegated to `mcs_setup.py services`), and update recovery. `--no-brew` and `--no-llm` skip stages. With `--no-llm`, an OpenAI-compatible endpoint must still serve on port 8080.

`mcs/ops/mcs_setup.py` has `init` (config, `.env` secrets and the Keychain entry; existing values are merged, never silently overwritten) and `check`, a typed gate for required and optional config keys. `check` is a diagnostic for the real machine and is not a substitute for synthetic tests.

## Schedule

Collection is a hybrid of hermes cron jobs and launchd agents (`deployment/launchagents/README.md` has the full table):

| Job | Cadence | Runner |
|---|---|---|
| Unread check `run_check.py` | every 5 min (thinned inside the script overnight) | hermes cron, `mcs_check.sh` |
| Health watcher | every 5 min | hermes cron, `mcs_health.sh` |
| Durable-job drain `--jobs-only` | `7,37 * * * *` | hermes cron, `mcs_deep.sh` |
| Nightly semantic/QC drain | 22:30 daily | hermes cron, `mcs_llm_catchup.sh` |
| llama-server restart when idle | 04:00 daily | hermes cron |
| Update check | 05:10 daily | hermes cron, `mcs_update.sh` |
| Update recovery watchdog | every 15 min | launchd `org.mcs.recovery` |
| Command ingest | event-driven on `data/cmd/` and `data/cmd_int/` | launchd `local.mcs-cmd`, `local.mcs-int` |
| `extract_llm` drainers | resident, poll every 120 s (two shards, one lends its slot to RT) | launchd |

<!-- openwiki: broken internal link [/openwiki/architecture/extraction-and-semantic.md] link "/openwiki/architecture/extraction-and-semantic.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`llamaserver` is a KeepAlive server, not a job. The two drainers split work by shard and LLM slot, which lines up with the [admission broker](/openwiki/architecture/extraction-and-semantic.md).

## Gateway restart

<!-- openwiki: broken internal link [/openwiki/architecture/notification-delivery.md] link "/openwiki/architecture/notification-delivery.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`hermes_plugin/` is loaded once by the long-lived Hermes gateway. Without `hermes gateway restart`, an old worker can receive a new-format spec, so a card arrives but its companion thread body and attachments are missing. Report a pending restart when deployment scope did not include one. See [Notification and delivery](/openwiki/architecture/notification-delivery.md).

## Self-update lifecycle

`mcs/ops/mcs_update.py` implements `status`, `check`, `apply`, `rollback` and `recover`:

- `check` (daily) detects the newest tag, notifies once, scans the `command_receipts` approval queue, and auto-applies only when mode is `auto`.
- `apply` takes `update.lock`, then `run.lock`, journals every stage, quiesces resident drainers, merges, and runs post-merge steps in a new-code subprocess with the locks held.
<!-- openwiki: broken internal link [/openwiki/architecture/ledger-storage.md] link "/openwiki/architecture/ledger-storage.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- `rollback` restores the last applied entry's `prev_sha`, and the DB as well when the apply carried a schema bump (a pre-update backup is taken, see [Ledger storage](/openwiki/architecture/ledger-storage.md)).
- `recover` finishes an interrupted apply from the journal.

Safety invariants: protected paths (`data/`, `config.json`, `.env`) are never touched, `git clean` is never run, and anything unverifiable counts as failure. On the receipt-driven path the approval boundary is the `command_receipts` commit. A local operator's `apply --command-id` is not looked up in receipts, because the CLI trusts the shell user.

## Recovery watchdog

`deployment/recovery/mcs_recover.py` is copied to `~/.mcs-recovery/` by `install.sh` and run by `org.mcs.recovery`. It lives outside the repo so a broken new release cannot take recovery down. It needs only the stdlib and git, so it works when the new code cannot import, the gateway is dead or the DB will not open. It is driven by the journal in `data/update_state.json` and never trusts `HEAD` alone. It writes `data/recovery_report.json` on every action and exits 0 silently when idle (`--if-stale` acts only on an interrupted or stale apply).
