# Third-party notices

This plugin is an original implementation for Omarchy (Python + Quickshell QML),
**inspired by** [Codenotch](https://github.com/vinzdg/codenotch) by Vinz
(MIT License). No Swift/ObjC code was copied.

What was reused as documented behaviour (not code):

- The provider semantics: which local credential each coding tool owns and
  which usage endpoint it answers (`Sources/Providers/`, `docs/specs/`).
- The Windows port's wire documentation in `windows/codenotch/src/usage.rs`
  (Claude: `GET https://api.anthropic.com/api/oauth/usage`, reply shape
  `{limits, five_hour, seven_day}`) and `windows/codenotch/src/codex.rs`
  (Codex: `GET https://chatgpt.com/backend-api/wham/usage`, reply shape
  `rate_limit.{primary_window, secondary_window}`), both MIT.
- The design language (edge pill, colour-graded rings, hover card with
  per-window bars) and the polling/backoff discipline (60 s active / 5 min
  idle, 429 backoff 60 s × 2^n capped at 15 min, stale-not-guessed).

The Codenotch design and name belong to the upstream author; this plugin is
called "Usage Notch" and claims no affiliation.
