# Usage Notch — plan de trabajo

Convención: v0.1 esqueleto funcional → v0.x providers → v1.0 paridad razonable
con el concepto. Actualizar este fichero al cerrar cada hito.

## v0.1 — Esqueleto (ESTA VERSIÓN)

- [x] `manifest.json` kind `panel`, `keepLoaded`
- [x] `usage.py`: modelos, Claude + Codex (multi-perfil), `poll`/`watch`/`doctor`,
      caché en `~/.local/state/synapsync-usage-notch/state.json`, backoff 429
- [x] `Notch.qml`: pill borde derecho, anillos Canvas, tooltip con barras y
      resets, poll 60 s + refresh al abrir, `mask: Region { item: hitArea }`.
      UX v4 FINAL (cero timers, cero auto-cierre): click en la pill alterna,
      click en cualquier otro sitio cierra, click en la tarjeta nada. La
      ventana nace pantalla completa y nunca cambia de geometría — colapsada
      la máscara solo deja la franja de 34px — así no hay chicle ni saltos de
      monitor. Lección: las panels NO recargan en caliente (solo restart) y
      los rsync van siempre con -av (un `-q` sin `-a` no copia nada).
- [x] Validar: `omarchy plugin validate`, `usage.py doctor` en máquina con
      sesiones reales, `omarchy restart shell` y comprobar la pill
      (2 anillos en HDMI-A-1, tarjeta con dato vivo de Codex)
- [x] Etiquetas Claude alineadas al original (`session`→Current session,
      `weekly_all`→All models, `weekly_opus/sonnet/scoped`, fallback
      `five_hour`/`seven_day`; kinds `session,weekly_all,weekly_opus,
      weekly_sonnet,weekly_scoped` verificados en `ClaudeOAuthProvider.swift`)
- [ ] Ajustar parse de `oauth/usage` contra respuesta real: el endpoint nos
      puso 429 con Retry-After largo (backoff lo respeta); el parser tolerante
      ya cubre `limits[]` + fallbacks. Reintentar tras el backoff con token
      fresco (`claude /usage` ya lo refrescó).
- [ ] Vía CLI (`claude /usage` + stdin /dev/null) en CLI 2.1.265 solo imprime
      la línea de suscripción, sin las líneas `Current session:` que parsea el
      original → descartada como fuente en esta versión; documentado aquí.

## v0.2 — Más providers (misma interfaz `fetch_*`)

Orden sugerido por valor/esfuerzo en Linux:

1. **Cursor** — HECHO v0.1: sesión de `state.vscdb` (SQLite `mode=ro` →
   `immutable=1`, cookie `WorkosCursorSessionToken`) → `usage-summary`;
   `Included usage` (0% en free también es lectura, no hueco), `API usage`
   si >0, `On demand` si aplica; `nothingMetered` como nota, no error.
   Verificado en vivo (plan free). Pendiente: fallback `cursor-agent login`
   (solo Linux sin editor).
2. **OpenCode** — HECHO x2: (a) plan Go: `auth.json` entrada `opencode-go`
   → `GET opencode.ai/zen/go/v1/usage` Bearer, rolling/weekly/monthly;
   (b) sin Go: anillo derivado del `opencode.db` local (tokens por provider
   este mes/hoy + filas por provider, solo >0). Verificado en vivo vía
   provider `opencode` (modelos Zen); vendors inactivos este mes no salen.
   Las vendor keys jamás se reclaman como cuenta OpenCode.
   OJO pi: este harness factura a Zen con key de alcance inferencia
   (`~/.pi/agent/auth.json`, solo se lee `opencode-go`); el endpoint de
   cuenta la rechaza (401) así que hay fallback automático a conteo local.
   Ni pi-usage puede dar límites oficiales con esa key — solo el dashboard
   web. El uso vía pi, por tanto, NO aparece en el anillo (limitación
   documentada, no bug del conteo).
3. **Gemini API (conteo local)** — HECHO: sin endpoint ni secretos (Google no
   publica uso por key). Tres lectores: Gemini CLI (`~/.gemini/tmp/*/chats`,
   dedup por id, `tokens.total`), OpenCode (`opencode.db`, solo
   `providerID='google'`, `total` o input+output+reasoning+cache.read+write),
   Hermes (`~/.hermes/state.db`, sin reasoning — ya va en output). Ventanas
   `month/today/fuente` con `count` y `usedFraction: null`; el QML muestra
   `~N` compacto y pista vacía (fidelity derived, como el original).
   Verificado: lectura del message log; sin tokens `google` cuando el equipo
   usa otros vendors.
4. **GitHub Copilot** — endpoint de cuota con la sesión de `gh auth login`
   (`GitHubCopilotProvider.swift`).
5. **Grok** — HECHO: sesión xAI de `~/.grok/auth.json` (solo issuer
   `auth.x.ai`; la live manda) → `GET cli-chat-proxy.grok.com/v1/billing
   ?format=credits` (Bearer + `X-XAI-Token-Auth: xai-grok-cli`); ventana
   `credits` (label del primer producto humanizado), fallback por producto
   y `Weekly limit` 0% en periodo fresco; 429 → backoff fijo 60 s.
   Verificado en vivo contra endpoint real.
6. **GLM / Antigravity / Perplexity / Copilot** — según lo que use el equipo.

## v0.3 — Motor de sesiones ("¿sigue trabajando?")

- `AgentSession` + monitores por provider con `inotify` sobre
  `~/.claude/sessions`, `~/.codex/sessions`, etc. (el original usa
  FSEvents/DispatchSource; aquí `inotifywait` o ` Gio.FileMonitor`).
- Hooks de Claude Code (`SessionStart`/`Stop`) como transporte extra.
- Liveness por árbol de pids + `hyprctl clients -json` para el focus.
- Arco giratorio (busy) / ámbar pulsante (waiting) dentro del anillo;
  tooltip lista sesiones vivas por nombre.
- Peek 5 s + sonido al terminar; notificaciones 80%/100% (una vez por cruce).

## Pi spend (09-sep)

- El uso vía gateway (pi, PI_PROVIDER=opencode-go) no aparece en ningún log
  de herramienta ni lo sirve ningún endpoint con las keys disponibles (la key
  de pi es de alcance inferencia: 401 en /zen/go/v1/usage; ni pi-usage puede).
- Solución: provider `pi` que reconstruye gasto de `~/.pi/agent/sessions/`
  (mensajes assistant con usage+cost). Mes/día en USD + filas por provider.
  Verificado en vivo: totales y filas cuadran con la contabilidad de pi.
  Ventanas con `unit: "usd"` → formato $ en QML (`money()`/`countText()`).

## Supervivencia de la pill (lección 09-sep)

- La ventana moría en silencio cuando un reload coincidía con una
  reconfiguración de monitores: `screen: resolveScreen()` one-shot apuntaba
  a un objeto muerto sin reintento. Fix: `targetScreen` por nombre +
  `screenGuard` cada 10 s (solo loguea cambios) + fallback a auto.
- `watchdog.sh` + systemd timer opt-in (cooldown 10 min): revive con
  `omarchy restart shell` si la capa falta y el plugin sigue enabled.
  ACTIVADO en esta máquina 09-sep (tras 3 muertes silenciosas: suspend,
  reload en tormenta de monitores, lock).
- Lock mata la superficie aunque el nombre no cambie: el guardián ahora
  firma la topología (`lastScreenSig`) y re-coloca con su cambio, sin churn
  (solo nombre o topología tocan `panel.screen`).

## Pulido v7 (09-sep)

- Tarjeta 300→360px + etiquetas cortas (Month/Today/X·month): fin de los …
- Pill 34→44px + conteos en tipografía menor (`isCount`): `~6.3M`/`$61.65`
  ya no se salen.
- Scroll con Flickable (clip + StopAtBounds); altura sigue compacta si hay
  poco contenido.
- Stale honesto: edad (`ageCopy`: just now/24m ago) en encabezado y ⚙ +
  sección atenuada (opacity 0.6); los snaps stale conservan su fetchedAt.
- Estabilidad: el guardián reasignaba `panel.screen` cada tick (los wrappers
  Screen mueren entre queries) y ESO mataba la superficie. Ahora solo se
  asigna cuando cambia el NOMBRE (`appliedScreenName`); churn cero.

## v1.0 — Acabado Omarchy

- [x] Ajustes v0.1: interruptor por familia (click derecho en la pill o ⚙),
  `usage.py config toggle|enable|disable`, stubs `disabled` sin red.
- [x] Placement: `placement: {screen, edge}` en config.json (`config set`,
  validado), FileView en vivo, `PanelWindow.screen` bindeada (auto = sin
  fijar; primary = foto del foco al aplicar, jamás lo sigue; nombre
  desenchufado = fallback, nunca se pierde), pill+tarjeta volteadas por
  edge. Top/bottom fuera de scope (pill vertical). Lección QML: asignar
  `undefined` a una anchor line NO la libera (ambos lados quedan fijos y el
  width se ignora → pill gigante de 1920px); bordes condicionales con
  binding `x`, nunca anclas ternarias.
- Settings con schema en `manifest.json` (borde: right/left/top/bottom,
  siempre-visible/auto-hide, orden de providers con drag, mute por provider,
  silencio/peek por separado).
- Multi-monitor (una pill por pantalla o solo primaria, configurable).
- `omarchy plugin validate` limpio + README con preview.png + envío al
  marketplace (issue, **sin** `Fixes #N` en commits — usar `Refs #N`).

## Deuda conocida (no esconder)

- La ventana layer-shell ocupa toda la altura derecha pero con `mask` solo la
  chrome tiene input — verificado el patrón contra `notifications/Service.qml`.
  Si el mask fallara en alguna versión de Quickshell, la franja bloquearía
  clicks: comprobar tras cada update de shell.
- Sin sandbox de red más allá de endpoints fijos + validación de IP global;
  antes de v1.0, revisión de seguridad estilo `audit` skill.
- Parser de Codex no incluye el fallback a rollout logs (`rate_limits` en
  `~/.codex/sessions/*.jsonl`) — el port Windows sí lo tiene; añadir si el
  endpoint deja de responder en algún plan.
