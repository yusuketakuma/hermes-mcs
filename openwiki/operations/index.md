# Files

- [Deployment, LaunchAgents and Self-Update](deployment-and-updates.md) - How hermes-mcs is installed and scheduled on macOS (install.sh, hermes cron, launchd), how the interrupted-update recovery watchdog lives outside the repo, and the mcs_update check/apply/rollback/recover lifecycle.
- [Testing, CI Gates and Safety Rules](testing-and-ci.md) - How to run the isolated test suite, lint and README drift checks, what the static incident gates enforce, and the hard project rules (stdlib-only core, synthetic fixtures only, safety gates that must not be weakened).
