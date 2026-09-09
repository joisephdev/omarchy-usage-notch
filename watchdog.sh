#!/bin/bash
# usage-notch watchdog (opt-in): revive la pill si su capa layer-shell
# desapareció con el plugin habilitado (p. ej. tras suspender o un hotplug
# de monitores que la mató en silencio).
#
# No hace nada si el plugin está deshabilitado. Para no entrar en un bucle
# de restarts si el QML está roto, deja pasar 10 min entre restarts.
# Ver systemd/usage-notch-watchdog.{service,timer} e instalación en README.
set -u
ID="synapsync.usage-notch"
STATE_DIR="${HOME}/.local/state/synapsync-usage-notch"
STAMP="${STATE_DIR}/last-watchdog-restart"
COOLDOWN=600

enabled=$(omarchy plugin list 2>/dev/null | awk -v id="$ID" '$1 == id {print $2}')
[ "$enabled" = "enabled" ] || exit 0

hyprctl layers 2>/dev/null | grep -q "namespace: ${ID}" && exit 0

now=$(date +%s)
if [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  [ $((now - last)) -lt "$COOLDOWN" ] && exit 0
fi

mkdir -p "$STATE_DIR"
echo "$now" > "$STAMP"
notify-send "Usage Notch" "Pill perdida — reiniciando la shell…" 2>/dev/null || true
omarchy restart shell
