# Repository wiki brief

This is the user-authored source brief, not a generated wiki page. Preserve it
during normal OpenWiki initialization and updates. Ground the wiki in current
code and tests, with `README.md`, `SECURITY.md`, `docs/INSTALLATION.md` and
`docs/SETUP_AGENT.md` as the maintained user-facing documentation. Historical
plans and deployment records describe their stated dates, not current runtime
state. Do not read patient data, credentials, local configuration, exported
patient pages or live service outputs to write this wiki.

For the next update, resolve these known documentation defects and reconcile
the corresponding Claims against current source:

- Distinguish local rule/model extraction from optional external semantic/QC
  requests to TypeSafe Jev. Do not claim that the entire pipeline is local or
  that Hermes notifications and exports are the only external data paths.
  Check `SECURITY.md`, `mcs/semantic/semantic_drain.py` (`run_due`, `_eval_chunked`)
  and `mcs/semantic/semantic_jev.py`, including mode and request-budget gates.
- Cover Slack, Discord and LINE WORKS cards. `notify.interactive` accepts `slack`,
  `discord`, `lineworks` and `off`; `off` disables cards, not text notifications.
  Check `mcs/ops/mcs_setup.py`, `mcs/notify/notify_cards.py` and
  `hermes_plugin/card_workers.py`. Keep Slack as the recommended onboarding
  path and preserve Discord and notification-free configurations.
- Slack/Discord use Hermes-owned native connections. LINE WORKS uses the
  independent `adapters/lineworks/` transport and `lineworks_adapter/` CLI;
  Hermes Agent source is unchanged. Check `docs/LINEWORKS.md`, including
  explicit credentials/scope, public HTTPS Callback, service candidates,
  unknown-send fences and its documented differences from native threads.
  Path B supports this independent delivery; notification-enabled runners
  omit `--no-notify`. Text-only LINE WORKS delivery needs no resident adapter.
- Explain the unread-cap fallback precisely. A positive snapshot timestamp,
  completed collection and committed ledger remain mandatory. Only after
  `_cap_cleared` proves stored coverage may the adapter retry a failed
  unread-filtered GET through the plain message list. The fallback still
  sends `timestamp`, confirms through project detail, and checks
  `_post_ack_gap`; it does not authorize an unconditional mark-as-read.
  Check `mcs/ingest/run_check.py` and `mcs/ingest/mcs_adapter.py`.
- Use page-relative Markdown links, including links from quickstart and
  between architecture/operations pages. `/openwiki/...` is a tool's virtual
  file path, not a portable Markdown hyperlink. Remove broken-link comments
  only after checking their replacement targets. Claims evidence retains
  the `repo://` scheme.

Reflect the current two-stage first install (`install.sh`, then setup `init`)
and its resolved Python path; do not add duplicate mandatory services/check
commands where `init` already performs them. Reuse existing configuration and
approval scope. Keep secret entry in the user's terminal, never in commands,
conversation, wiki or logs. Distinguish local synthetic checks from real SDK,
real model and deployed service verification.
