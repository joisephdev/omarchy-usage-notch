# Usage Notch for Omarchy

How much of each coding assistant's usage limit have you burned — pinned to
the right screen edge, at a glance. Inspired by
[Codenotch](https://github.com/vinzdg/codenotch) (see `NOTICE.md`).

![status](https://img.shields.io/badge/status-v0.1%20scaffold-yellow)
![license](https://img.shields.io/badge/license-MIT-green)

## What it does (v0.1)

- A small pill on the right edge shows one ring per installed provider
  (Claude Code, Codex — more coming). The ring is the headline limit window;
  the colour grades green → amber → red as it burns.
- Hover (or click) the pill to expand the detail card: every limit window
  with its own bar, percentage and reset countdown.
- Never signs in anywhere. Every reading is borrowed from a credential a tool
  on your machine already holds — install and sign in to any of them and its
  ring appears. Switching providers off is v2; for now uninstalled providers
  simply get no cell.
- Stale readings are labelled stale with their age instead of guessed;
  rate limits (429) back off without hammering the endpoint.

## Install (dev)

```sh
# link the dev folder as a plugin and enable it
omarchy plugin add /home/rperaza/joisephdev/my-projects/plugins-projects/omarchy/omarchy-usage-notch --enable
omarchy restart shell
```

If `plugin add` needs a git URL, symlink instead:

```sh
ln -s /home/rperaza/joisephdev/my-projects/plugins-projects/omarchy/omarchy-usage-notch \
  ~/.config/omarchy/plugins/synapsync.usage-notch
omarchy restart shell
```

## Uninstall

```sh
omarchy plugin disable synapsync.usage-notch   # or: omarchy plugin remove synapsync.usage-notch
```

Optional leftovers (never touched without your consent):

```sh
systemctl --user disable --now usage-notch-watchdog.timer  # only if you enabled it
rm -rf ~/.config/synapsync-usage-notch      # provider toggles + placement
rm -rf ~/.local/state/synapsync-usage-notch # cached readings + backoff
```

## Diagnose

```sh
python3 usage.py doctor   # credentials, data sources, one live poll
python3 usage.py poll     # what the QML sees (JSON array)
python3 usage.py config toggle cursor   # enable/disable a provider family
```

Providers are toggled from the UI too: right-click the pill (or the ⚙ in
the card) for the Providers list — green dot is on, click flips it. Choices
persist in `~/.config/synapsync-usage-notch/config.json`; a disabled
provider is skipped without touching the network and can be re-enabled
from the same list.

## Placement

Screen and edge live in the same `config.json` under `placement` and can be
changed from the ⚙ panel (Screen / Edge rows) or the CLI:

```sh
python3 usage.py config set placement.screen HDMI-A-1  # name, primary, or auto
python3 usage.py config set placement.edge left        # right (default) or left
```

- `auto` leaves the screen to Quickshell; a name pins it (`eDP-1`,
  `HDMI-A-1`…); `primary` snapshots the focused monitor when the setting is
  applied (it never follows focus around — that jumping is the bug this
  fixes). An unplugged name falls back instead of vanishing.
- Top/bottom edges are out of scope: the pill is a vertical design.

`doctor` never writes or refreshes credentials — read-only, like everything
else here. If a login expired, renew it with that provider's own CLI.

## Troubleshooting

- **Pill gone after suspend/wake?** The compositor may not restore the
  layer surface. Revive it with:
  ```sh
  omarchy restart shell
  ```
- **Edited files but nothing changed?** Panels do **not** hot-reload —
  syncing files is not enough. Sync, validate, then restart:
  ```sh
  rsync -av --exclude='.git' --exclude='__pycache__' \
    /path/to/omarchy-usage-notch/ ~/.config/omarchy/plugins/synapsync.usage-notch/
  chmod +x ~/.config/omarchy/plugins/synapsync.usage-notch/*.py
  omarchy plugin validate ~/.config/omarchy/plugins/synapsync.usage-notch
  omarchy restart shell
  ```
  Always pass `-av` to rsync: `-q` without `-a` copies nothing (silently).
  Symlinks are not allowed inside an installed plugin folder — use a real
  copy via rsync.
- **A provider shows `stale` / `rate limited`?** HTTP 429 responses back off
  (60 s × 2^n, capped, persisted across restarts) and retry on their own.
  Don't force it; in dev only you may clear
  `~/.local/state/synapsync-usage-notch/state.json` to retry immediately.

## Files

| File | What |
|---|---|
| `manifest.json` | Plugin manifest (`panel` kind, always loaded) |
| `Notch.qml` | Overlay pill + detail card (Quickshell `PanelWindow`, layer `Overlay`, no keyboard focus, click-through outside the chrome) |
| `usage.py` | Backend, stdlib only: providers, polling, file cache, backoff, `poll`/`watch`/`doctor` |
| `docs/PLAN.md` | Roadmap v0.1 → v1 (Spanish) |

## Revival / watchdog

If the pill ever goes missing (suspend/wake or monitor hotplug can drop
the layer-shell surface silently), one command brings it back:

```sh
omarchy restart shell
```

The QML keeps its screen binding alive by name (a 10 s guard re-attaches
it, falling back to automatic when the pinned monitor is unplugged), so a
restart should be a rare event. For an automatic safety net, enable the
opt-in watchdog (checks every 2 min, respects a 10 min restart cooldown):

```sh
ln -s ~/.config/omarchy/plugins/synapsync.usage-notch/systemd/usage-notch-watchdog.{service,timer} \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usage-notch-watchdog.timer
```

## Privacy / security

- Fixed `https://` endpoints only; redirects are followed solely within the
  same host. Connections resolve and dial literal global IPs with SNI, with a
  15 s end-to-end deadline and a 256 KB body cap.
- Credentials are read from the owning tool's files and never copied,
  refreshed or written. Nothing leaves the machine except the provider's own
  official usage request — the same one its CLI makes.
