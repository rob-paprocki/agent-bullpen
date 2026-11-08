## What and why

<!-- What does this change, and what problem does it solve? Link an issue if there is one. -->

## How I tested it

<!-- What you ran, and on what. Say what you could not test, for example "not run on a real Proxmox host". -->

- [ ] `python3 tests/smoke_test.py` passes (needed when `hub/` or `runner/` changed)
- [ ] `python3 tests/test_runner_units.py` and `python3 tests/test_hub_units.py` pass (when `runner/` or `hub/` changed)
- [ ] `python3 tests/check_repo.py` passes
- [ ] Shell scripts pass `shellcheck` (when a script changed)

## Checklist

- [ ] The guides in `docs/` say what changed, in the same plain style (numbered steps, short sentences).
- [ ] `CHANGELOG.md` has a line in its top, unreleased section (`## [Unreleased]`, or `## [X.Y.Z] - Unreleased`; add `## [Unreleased]` only if the top section has a date).
- [ ] No passwords, tokens, keys, personal names or machine names in code, tests or docs.
- [ ] On the dashboard, every string an agent, a runner or a CLI supplies goes through `esc()`. `md()` is only for answer bodies.
- [ ] Nothing chooses, holds back or moves work because of usage figures (plan limits, tokens); they are for display only.
- [ ] Nothing new is sent anywhere without the user asking for it (Agent Bullpen has no telemetry).
