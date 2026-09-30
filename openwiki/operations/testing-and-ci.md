---
type: operations
title: Testing, CI Gates and Safety Rules
description: How to run the isolated test suite, lint and README drift checks, what the static incident gates enforce, and the hard project rules (stdlib-only core, synthetic fixtures only, safety gates that must not be weakened).
tags: [testing, ci, pytest, ruff, gates, safety]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T03:50:50.528Z
sources:
  - id: openwiki-source-3ab7165c9bea6d1669b54c49
    resource: repo://ci/gates.py
  - id: openwiki-source-3285be2fcec132885149c69c
    resource: repo://ci/mine_gates.py
  - id: openwiki-source-5fd879e8b1a9ca380f062f2d
    resource: repo://conftest.py
  - id: openwiki-source-51183125cb4711a6f8997e74
    resource: repo://scripts/run_tests.sh
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
generated: { by: "claude-code", at: "2026-09-29T03:50:50.528Z" }
---

# Testing, CI Gates and Safety Rules

## Running checks

| Command | Purpose |
|---|---|
| `scripts/run_tests.sh [paths]` | Run pytest. It defaults to `tests integration`, and area runs use `tests/<area>/`. |
| `ruff check mcs/ tests/ hermes_plugin/ integration/ ci/ scripts/ deployment/ conftest.py` | Lint, same scope as CI. |
| `python3 scripts/update_readme.py [--check]` | Regenerate the `GENERATED:*` blocks in `docs/DEVELOPMENT.md`. CI detects drift. |
| `python3 ci/gates.py` | Static gates distilled from past incidents. |
| `python3 ci/mine_gates.py --check` | Verify each incident in `docs/dev-records` is covered by a gate or test. |

`make test|lint|readme|check|gates` wrap these and use `uv` when available.

## Isolation

`scripts/run_tests.sh` runs pytest under `env -i` with a fresh temporary `HOME`, `XDG_*`, `TMPDIR`, `TZ=UTC` and `MCS_TEST_SANDBOX=1`. Real credentials, config and data are therefore unreachable. Interpreter choice: `MCS_TEST_PYTHON`, then the active venv, then `.venv` or `venv` in the repo, then `python3`.

`tests/conftest.py` adds a process boundary guard. It registers `mcs/` and every `tests/<area>/` directory as import roots (so shared testkits import from any area), and it fails accidental live network, Keychain and Chrome calls at the boundary. Subprocesses for synthetic CLI tests stay allowed. Tests use temporary databases and stubs and never touch real MCS, Discord, Keychain, the original DB, the local LLM or Jev.

The root `conftest.py` skips `integration/test_hermes_*` when the Hermes SDK modules are not importable, at file scope so pure-synthetic integration narratives still run. New gateway-bound tests must keep the `test_hermes_` prefix. A passing local run does not prove real-SDK integration, which CI checks in a pinned Hermes environment.

## Static gates

`ci/gates.py` is stdlib-only and offline. Each gate encodes a defect class that occurred in this repo and returns violations, and any violation makes it exit 1. Examples include an allowlist of files that may open a write-mode `Ledger` (all others must use the read-only reader). `ci/gates-coverage.json` maps incident IDs to gates or tests. `ci/mine_gates.py` requires that defect-class IDs (FIX, BUG, INCIDENT, REGRESSION) are `covered` by an existing gate or test, and that audit-class IDs at least appear in the manifest.

## Hard rules

- The collection and analysis core depends on the standard library only. Do not add external dependencies. The sole exception is Hermes-bundled `discord.py`, lazily imported inside `hermes_plugin/mcs_discord/{actions,cards}.py`, with no independent bot, credentials or REST connection.
- Fixtures, benchmarks and few-shots are fully synthetic. Anonymized real posts are not allowed. `data/`, `.env`, `config.json` and `chrome-profile/` are git-ignored and must stay out of the repo.
<!-- openwiki: broken internal link [/openwiki/architecture/ingest-and-mcs-adapter.md] link "/openwiki/architecture/ingest-and-mcs-adapter.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/openwiki/architecture/human-approval-and-external-export.md] link "/openwiki/architecture/human-approval-and-external-export.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- Do not break safety gates: snapshot-timestamped read-marking, no redirect or proxy, and human-approved operations only through `--confirm-human` with a reason and receipt. See [Ingest](/openwiki/architecture/ingest-and-mcs-adapter.md) and [Approvals](/openwiki/architecture/human-approval-and-external-export.md).
- Adding or changing `mcs/**/*.py` docstrings, detectors, stats or subcommands requires regenerating the README blocks. The first sentence of a module docstring is published, so keep it a one-line summary.
- Do not mechanically split the large cohesive modules by line count.

<!-- openwiki: broken internal link [/openwiki/quickstart.md] link "/openwiki/quickstart.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`SECURITY.md` lists the data-handling boundaries: no patient content, credentials, or `mcs_view` output in the repo, shared logs or external LLMs. See also [Quickstart](/openwiki/quickstart.md).
