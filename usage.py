#!/usr/bin/env python3
"""usage.py — backend del Usage Notch (Opción A: Python + Quickshell QML).

Lee los límites de uso de los asistentes de código instalados y los imprime
como JSON para que Notch.qml los dibuje. Nunca inicia sesión en ningún sitio:
toma prestadas las credenciales que las propias herramientas ya guardaron en
este equipo (solo lectura, nunca las escribe ni las refresca).

Comandos (mismo patrón que otros plugins: one-shot + watch):
    usage.py poll      Una lectura completa -> JSON array por stdout (exit 0)
    usage.py watch     poll inicial + una línea JSON por cada re-lectura (60 s)
    usage.py doctor    Diagnóstico legible: credenciales, fuentes, endpoints

Proveedores v1: Claude Code, Codex (con multi-perfil ~/.claude-<slug> y
~/.codex-<slug>, default primero y resto alfabético, como el original).

Formato de cada proveedor:
    {"id": "claude", "displayName": "Claude", "status": "ok|stale|needsAuth|error",
     "windows": [{"id": "five_hour", "label": "5h limit", "usedFraction": 0.42,
                  "resetsAt": 1725... (ms epoch o null)}],
     "fetchedAt": ms, "error": null | "texto corto"}

Inspirado en https://github.com/vinzdg/codenotch (MIT) y en su port de
Windows en Rust (windows/codenotch/src/{usage,codex}.rs), de donde salen los
endpoints y formatos documentados abajo. Ver NOTICE.md.
"""

import glob
import http.client
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

VERSION = "0.1.0"

# Disciplina del original: deadline end-to-end, tope de lectura incremental,
# sin URLs controladas por el usuario (endpoints constantes https).
HTTP_TIMEOUT = 15
MAX_BODY_BYTES = 256 * 1024
CHUNK_BYTES = 16 * 1024
POLL_IDLE_SECS = 300      # sin actividad conocida: cada 5 min
POLL_ACTIVE_SECS = 60     # con sesiones activas (v2): cada 1 min
BACKOFF_BASE_SECS = 60    # ante 429: 60s * 2^n, tope 15 min
BACKOFF_CAP_SECS = 900

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".local", "state", "synapsync-usage-notch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
    "synapsync-usage-notch")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Familias con interruptor en Ajustes. Un perfil multi-cuenta
# (claude-work) hereda la familia (claude).
FAMILIES = ("claude", "codex", "cursor", "opencode", "gemini-api",
            "grok", "pi")
FAMILY_NAMES = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor",
                "opencode": "OpenCode", "gemini-api": "Gemini API",
                "grok": "Grok", "pi": "Pi spend"}
FAMILY_GLYPHS = {"claude": "claude", "codex": "codex", "cursor": "cursor",
                 "opencode": "opencode", "gemini-api": "gemini",
                 "grok": "grok", "pi": "pi"}


def now_ms():
    return int(time.time() * 1000)


# ------------------------------------------------------------- HTTP seguro
# Conexiones verificadas a IPs globales con SNI (anti SSRF/DNS-rebinding):
# los endpoints son constantes, pero resolvemos y validamos igual.


class _VerifiedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        last = None
        for family, socktype, proto, _canon, sockaddr in infos:
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if not ip.is_global:
                last = ValueError("non-global IP %s for %s" % (ip, self.host))
                continue
            try:
                s = socket.socket(family, socktype, proto)
                s.settimeout(self.timeout if self.timeout is not None else HTTP_TIMEOUT)
                s.connect(sockaddr)  # sockaddr completo: soporta tupla IPv6
                self.sock = s
                if self._tunnel_host:
                    self._tunnel()
                self.sock = self._context.wrap_socket(
                    self.sock, server_hostname=self.host)
                return
            except Exception as e:  # noqa: BLE001 - probar siguiente IP
                last = e
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
        raise last if last else socket.gaierror("no usable address for %s" % self.host)


class _VerifiedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_VerifiedHTTPSConnection, req)


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Solo permite redirects https al mismo host (los endpoints responden
    directo; un redirect a otro host se reporta como error, no se sigue)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlparse

        old, new = urlparse(req.full_url), urlparse(
            urllib.request.urljoin(req.full_url, newurl))
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.URLError(
                "refusing cross-host redirect %s -> %s" % (old.hostname, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(
    _VerifiedHTTPSHandler, _SameHostRedirectHandler)


def http_get_json(url, headers):
    """GET con deadline total, tope de cuerpo y parse incremental acotado."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.status
            retry_after = resp.headers.get("Retry-After")
            chunks, total = [], 0
            while True:
                buf = resp.read(CHUNK_BYTES)
                if not buf:
                    break
                total += len(buf)
                if total > MAX_BODY_BYTES:
                    raise ValueError("response body exceeds %d bytes" % MAX_BODY_BYTES)
                chunks.append(buf)
            return status, retry_after, json.loads(b"".join(chunks).decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _HttpStatus(e.code, e.headers.get("Retry-After"), str(e))


class _HttpStatus(Exception):
    def __init__(self, status, retry_after, msg):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


def num(value):
    return value if isinstance(value, (int, float)) and value == value else None


# ------------------------------------------------------------- estado local
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass  # el caché es best-effort; el poll sigue sirviendo lo fresco


def backoff_wait(state, provider_id):
    until = num((state.get("backoff", {}).get(provider_id) or {}).get("until"))
    if until and until > now_ms():
        return (until - now_ms()) / 1000.0
    return 0.0


def record_rate_limit(state, provider_id, retry_after_header, consecutive):
    try:
        wait = float(retry_after_header) if retry_after_header else 0.0
    except (TypeError, ValueError):
        wait = 0.0
    wait = max(wait, min(BACKOFF_BASE_SECS * (2 ** min(consecutive, 4)), BACKOFF_CAP_SECS))
    state.setdefault("backoff", {})[provider_id] = {
        "until": now_ms() + int(wait * 1000), "consecutive": consecutive + 1}
    save_state(state)
    return wait


def clear_backoff(state, provider_id):
    if provider_id in state.get("backoff", {}):
        del state["backoff"][provider_id]
        save_state(state)


def last_good(state, provider_id):
    snap = (state.get("snapshots", {}) or {}).get(provider_id)
    return snap if isinstance(snap, dict) else None


def store_good(state, provider_id, snapshot):
    state.setdefault("snapshots", {})[provider_id] = snapshot
    save_state(state)


# ------------------------------------------------------------- perfiles
def discover_claude_profiles():
    """~/.claude primero, luego ~/.claude-<slug> alfabético (regla upstream)."""
    found = []
    default = os.path.join(HOME, ".claude")
    if os.path.isdir(default):
        found.append(("claude", "Claude", default))
    for path in sorted(glob.glob(os.path.join(HOME, ".claude-*"))):
        if not os.path.isdir(path):
            continue
        slug = os.path.basename(path)[len(".claude-"):] or "extra"
        found.append(("claude-" + slug, "Claude (%s)" % slug, path))
    return found


def discover_codex_profiles():
    found = []
    default = os.path.join(HOME, ".codex")
    if os.path.isdir(default):
        found.append(("codex", "Codex", default))
    for path in sorted(glob.glob(os.path.join(HOME, ".codex-*"))):
        if not os.path.isdir(path):
            continue
        slug = os.path.basename(path)[len(".codex-"):] or "extra"
        found.append(("codex-" + slug, "Codex (%s)" % slug, path))
    return found


# ------------------------------------------------------------- Claude
CLAUDE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
# Respuesta (snake_case): {limits:[{kind,percent,resets_at}],
# five_hour:{utilization,resets_at}, seven_day:{...}}. `limits` manda;
# five_hour/seven_day entran como fallback (una ventana recién rotada
# desaparece de limits). Fuente: windows/codenotch/src/usage.rs.


def read_claude_token(profile_dir):
    for name in (".credentials.json", "credentials.json"):
        path = os.path.join(profile_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            oauth = data if isinstance(data, dict) else {}
        token = oauth.get("accessToken")
        if isinstance(token, str) and token.strip():
            expires_at = num(oauth.get("expiresAt"))
            expired = bool(expires_at and expires_at <= now_ms())
            return token.strip(), expired, path
    return None, False, None


def _claude_windows(payload):
    windows = []
    seen = set()
    limits = payload.get("limits") if isinstance(payload, dict) else None
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or entry.get("id") or "limit")
            pct = num(entry.get("percent"))
            if pct is None:
                pct = num(entry.get("utilization"))
            if pct is None:
                continue
            resets = entry.get("resets_at") or entry.get("resetsAt")
            windows.append({
                "id": kind, "label": _claude_label(kind),
                "usedFraction": _clamp01(pct / 100.0 if pct > 1 else pct),
                "resetsAt": _to_ms_epoch(resets)})
            seen.add(kind)
    # Fallback five_hour / seven_day (y variantes con guión).
    for key, label in (("five_hour", "5h limit"), ("five-hour", "5h limit"),
                       ("seven_day", "Weekly"), ("seven-day", "Weekly"),
                       ("session", "Session")):
        entry = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(entry, dict) or key in seen:
            continue
        pct = num(entry.get("utilization"))
        if pct is None:
            pct = num(entry.get("percent"))
        if pct is None:
            continue
        windows.append({
            "id": key, "label": label,
            "usedFraction": _clamp01(pct / 100.0 if pct > 1 else pct),
            "resetsAt": _to_ms_epoch(entry.get("resets_at") or entry.get("resetsAt"))})
    return windows


def _claude_label(kind):
    # Etiquetas canónicas del original (UsageResponse.label): una lectura
    # archivada bajo una fuente debe casar cuando la otra toma el relevo.
    table = {
        "session": "Current session",
        "five_hour": "Current session",
        "five-hour": "Current session",
        "weekly_all": "All models",
        "seven_day": "All models",
        "seven-day": "All models",
        "weekly_opus": "Opus",
        "weekly_sonnet": "Sonnet",
        "weekly_scoped": "Scoped",
        "scoped": "Scoped",
    }
    if kind in table:
        return table[kind]
    k = kind.lower()
    if k.startswith("weekly_"):
        return k[len("weekly_"):].replace("_", " ")
    return kind.replace("_", " ")


def fetch_claude(profile_id, display_name, profile_dir, state):
    wait = backoff_wait(state, profile_id)
    if wait > 0:
        snap = last_good(state, profile_id)
        if snap:
            snap = dict(snap); snap["status"] = "stale"
            return snap
        return _err(profile_id, display_name, "stale",
                    "rate limited, retry in %dm" % max(1, int(wait // 60)))
    token, expired, _path = read_claude_token(profile_dir)
    if not token:
        return _err(profile_id, display_name, "needsAuth",
                    "sign in with Claude Code CLI once")
    headers = {"Authorization": "Bearer " + token,
               "anthropic-beta": "oauth-2025-04-20",
               "Accept": "application/json"}
    try:
        status, retry_after, payload = http_get_json(CLAUDE_ENDPOINT, headers)
    except _HttpStatus as e:
        if e.status in (401, 403):
            # El token pudo rotar: re-leer una vez y reintentar (disciplina upstream).
            token2, _, _ = read_claude_token(profile_dir)
            if token2 and token2 != token:
                try:
                    headers["Authorization"] = "Bearer " + token2
                    _s, _r, payload = http_get_json(CLAUDE_ENDPOINT, headers)
                    return _ok_claude(profile_id, display_name, payload, state)
                except Exception:  # noqa: BLE001 - cae al needsAuth de abajo
                    pass
            return _err(profile_id, display_name, "needsAuth",
                        "token rejected — open Claude Code to refresh it")
        if e.status == 429:
            consec = ((state.get("backoff", {}).get(profile_id) or {})
                      .get("consecutive", 0))
            wait = record_rate_limit(state, profile_id, e.retry_after, consec)
            snap = last_good(state, profile_id)
            if snap:
                snap = dict(snap); snap["status"] = "stale"
                return snap
            return _err(profile_id, display_name, "stale",
                        "rate limited, retry in %dm" % max(1, int(wait // 60)))
        return _stale_or_err(state, profile_id, display_name,
                             "HTTP %s" % e.status)
    except Exception as e:  # noqa: BLE001 - red/network/file: degradar a stale
        return _stale_or_err(state, profile_id, display_name, _short(e))
    return _ok_claude(profile_id, display_name, payload, state)


def _ok_claude(profile_id, display_name, payload, state):
    clear_backoff(state, profile_id)
    snap = {"id": profile_id, "displayName": display_name, "glyph": "claude",
            "status": "ok", "windows": _claude_windows(payload),
            "fetchedAt": now_ms(), "error": None}
    store_good(state, profile_id, snap)
    return snap


# ------------------------------------------------------------- Codex
CODEX_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
# Respuesta: rate_limit.{primary_window,secondary_window} con
# used_percent / limit_window_seconds / reset_at (s) | reset_after_seconds,
# más plan_type arriba. Etiqueta por duración ("5h limit" > "primary").
# Fuente: windows/codenotch/src/codex.rs.


def read_codex_credential(profile_dir):
    path = os.path.join(profile_dir, "auth.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None, path
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        tokens = data if isinstance(data, dict) else {}
    token = tokens.get("access_token")
    account = tokens.get("account_id")
    if isinstance(token, str) and token.strip():
        return token.strip(), account, path
    return None, None, path


def _codex_label(window_minutes, wid):
    if window_minutes is None:
        return "5h limit" if wid == "primary" else "Longer window"
    mins = float(window_minutes)
    if mins <= 60 * 8:
        if abs(mins - 300) < 1:
            return "5h limit"
        h = mins / 60.0
        return ("%g" % h) + "h limit"
    if mins <= 60 * 24 * 8:
        return "Weekly" if abs(mins - 10080) < 1 else "%gd limit" % (mins / 1440.0)
    return "Monthly"


def _codex_windows(payload):
    windows = []
    rate = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate, dict):
        return windows
    for wid, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        w = rate.get(key)
        if not isinstance(w, dict):
            continue
        pct = num(w.get("used_percent"))
        if pct is None:
            continue
        limit_secs = num(w.get("limit_window_seconds"))
        resets = num(w.get("reset_at"))
        if resets is None:
            after = num(w.get("reset_after_seconds"))
            resets = (time.time() + after) if after else None
        windows.append({
            "id": wid,
            "label": _codex_label(limit_secs / 60.0 if limit_secs else None, wid),
            "usedFraction": _clamp01(pct / 100.0 if pct > 1 else pct),
            "resetsAt": int(resets * 1000) if resets else None})
    return windows


def fetch_codex(profile_id, display_name, profile_dir, state):
    wait = backoff_wait(state, profile_id)
    if wait > 0:
        snap = last_good(state, profile_id)
        if snap:
            snap = dict(snap); snap["status"] = "stale"
            return snap
        return _err(profile_id, display_name, "stale",
                    "rate limited, retry in %dm" % max(1, int(wait // 60)))
    token, _account, _path = read_codex_credential(profile_dir)
    if not token:
        return _err(profile_id, display_name, "needsAuth",
                    "sign in with Codex CLI once")
    try:
        _status, _retry, payload = http_get_json(
            CODEX_ENDPOINT,
            {"Authorization": "Bearer " + token, "Accept": "application/json"})
    except _HttpStatus as e:
        if e.status in (401, 403):
            return _err(profile_id, display_name, "needsAuth",
                        "sign-in expired — open Codex once to refresh it")
        if e.status == 429:
            consec = ((state.get("backoff", {}).get(profile_id) or {})
                      .get("consecutive", 0))
            wait = record_rate_limit(state, profile_id, e.retry_after, consec)
            snap = last_good(state, profile_id)
            if snap:
                snap = dict(snap); snap["status"] = "stale"
                return snap
            return _err(profile_id, display_name, "stale",
                        "rate limited, retry in %dm" % max(1, int(wait // 60)))
        return _stale_or_err(state, profile_id, display_name,
                             "HTTP %s" % e.status)
    except Exception as e:  # noqa: BLE001 - degradar a stale
        return _stale_or_err(state, profile_id, display_name, _short(e))
    clear_backoff(state, profile_id)
    snap = {"id": profile_id, "displayName": display_name, "glyph": "codex",
            "status": "ok", "windows": _codex_windows(payload),
            "fetchedAt": now_ms(), "error": None}
    store_good(state, profile_id, snap)
    return snap


# ------------------------------------------------------------- Cursor
CURSOR_ENDPOINT = "https://cursor.com/api/usage-summary"
# Sesión prestada del propio editor: state.vscdb (SQLite, tabla
# ItemTable(key,value)) -> cursorAuth/accessToken +
# cursorAuth/stripeMembershipAuthId, unidos en la cookie
# WorkosCursorSessionToken=<authId>::<token>.
# Respuesta: {billingCycleEnd, membershipType, isUnlimited,
#  individualUsage: {plan: {totalPercentUsed, apiPercentUsed, ...},
#   onDemand: {enabled, used, limit}}}. Cursor mide % del allowance, no
# requests: totalPercentUsed es el número del dashboard (0 también es lectura).
# Fuente: windows/codenotch/src/cursor.rs (+ CursorCredentials.swift).
# v1: solo SQLite del editor. Fallback `cursor-agent login` (keychain en
# macOS) pendiente — ver docs/PLAN.md.
CURSOR_VSCDB = os.path.join(
    HOME, ".config", "Cursor", "User", "globalStorage", "state.vscdb")


def _cursor_unquote(value):
    """VS Code serializa valores a JSON: si viene entrecomillado, pelar."""
    if isinstance(value, str) and len(value) >= 2 and value.startswith('"'):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str) and parsed:
                return parsed
        except ValueError:
            pass
    return value


def read_cursor_session(vscdb_path=CURSOR_VSCDB):
    """Devuelve (cookie, plan) o (None, plan|None). Solo lectura."""
    import sqlite3
    from urllib.parse import quote

    if not os.path.isfile(vscdb_path):
        return None, None
    conn = None
    for query in ("mode=ro", "immutable=1"):
        try:
            conn = sqlite3.connect(
                "file:%s?%s" % (quote(vscdb_path), query),
                uri=True, timeout=5)
            conn.execute("SELECT 1 FROM ItemTable LIMIT 1").fetchone()
            break  # abre Y lee (sin -shm el open cuela pero el SELECT falla)
        except Exception:  # noqa: BLE001 - probar siguiente modo
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
    if conn is None:
        return None, None
    try:
        rows = dict(conn.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%'"
        ).fetchall())
    except Exception:  # noqa: BLE001
        return None, None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    token = _cursor_unquote(rows.get("cursorAuth/accessToken") or "")
    auth_id = _cursor_unquote(rows.get("cursorAuth/stripeMembershipAuthId") or "")
    plan = _cursor_unquote(rows.get("cursorAuth/stripeMembershipType") or "") or None
    if token and auth_id:
        return ("WorkosCursorSessionToken=%s::%s" % (auth_id, token)), plan
    return None, plan


def _cursor_windows(payload):
    resets = _to_ms_epoch(payload.get("billingCycleEnd"))
    usage = payload.get("individualUsage") or {}
    plan = usage.get("plan") or {}
    windows = []
    total = num(plan.get("totalPercentUsed"))
    if total is not None:  # 0 es lectura, no hueco (lección upstream)
        windows.append({"id": "included", "label": "Included usage",
                        "usedFraction": _clamp01(total / 100.0),
                        "resetsAt": resets})
    api = num(plan.get("apiPercentUsed"))
    if api is not None and api > 0:
        windows.append({"id": "api", "label": "API usage",
                        "usedFraction": _clamp01(api / 100.0),
                        "resetsAt": resets})
    od = usage.get("onDemand") or {}
    if od.get("enabled") is True:
        limit = num(od.get("limit")) or 0.0
        used = num(od.get("used"))
        if limit > 0 and used is not None:
            windows.append({"id": "on_demand", "label": "On demand",
                            "usedFraction": _clamp01(used / limit),
                            "resetsAt": resets})
    return windows


def fetch_cursor(state):
    pid, name = "cursor", "Cursor"
    if not os.path.isfile(CURSOR_VSCDB):
        return None  # no instalado: sin celda, es lo esperado
    wait = backoff_wait(state, pid)
    if wait > 0:
        snap = last_good(state, pid)
        if snap:
            snap = dict(snap); snap["status"] = "stale"
            return snap
        return _err(pid, name, "stale",
                    "rate limited, retry in %dm" % max(1, int(wait // 60)))
    cookie, _plan = read_cursor_session()  # re-leer siempre: el editor rota
    if not cookie:
        return _err(pid, name, "needsAuth", "sign in to Cursor")
    try:
        _status, _retry, payload = http_get_json(
            CURSOR_ENDPOINT,
            {"Cookie": cookie, "Accept": "application/json"})
    except _HttpStatus as e:
        if e.status in (401, 403):
            return _err(pid, name, "needsAuth",
                        "session rotated — open Cursor once")
        if e.status == 429:
            consec = ((state.get("backoff", {}).get(pid) or {})
                      .get("consecutive", 0))
            wait = record_rate_limit(state, pid, e.retry_after, consec)
            snap = last_good(state, pid)
            if snap:
                snap = dict(snap); snap["status"] = "stale"
                return snap
            return _err(pid, name, "stale",
                        "rate limited, retry in %dm" % max(1, int(wait // 60)))
        return _stale_or_err(state, pid, name, "HTTP %s" % e.status)
    except Exception as e:  # noqa: BLE001 - degradar a stale
        return _stale_or_err(state, pid, name, _short(e))
    clear_backoff(state, pid)
    windows = _cursor_windows(payload if isinstance(payload, dict) else {})
    if not windows:
        # nothingMetered: no es error y no debe mostrarse como tal.
        membership = (payload.get("membershipType")
                      if isinstance(payload, dict) else None) or "this"
        if isinstance(payload, dict) and payload.get("isUnlimited") is True:
            note = "Unlimited on the %s plan — nothing to meter" % membership
        else:
            note = "The %s plan has nothing for Cursor to meter yet" % membership
        snap = {"id": pid, "displayName": name, "glyph": "cursor",
                "status": "ok", "windows": [], "fetchedAt": now_ms(),
                "error": note}
        store_good(state, pid, snap)
        return snap
    snap = {"id": pid, "displayName": name, "glyph": "cursor",
            "status": "ok", "windows": windows, "fetchedAt": now_ms(),
            "error": None}
    store_good(state, pid, snap)
    return snap


# ------------------------------------------------------------- OpenCode (Go plan)
OPENCODE_ENDPOINT = "https://opencode.ai/zen/go/v1/usage"
# Ventanas account-wide del plan Go, las del dashboard:
# {usage: {rolling: {status, percent, resetsAt}, weekly: {...}, monthly: {...}}}
# `percent` es USED ("X% used"), el anillo no invierte. resetsAt ISO8601 con
# ms. Solo la entrada `opencode-go` autentica; otras (`openai`, ...) son keys
# de esos vendors y reclamarlas leería la cuenta equivocada.
# Fuente: OpenCodeCredentials.swift + OpenCodeUsage.swift (+Provider).
OPENCODE_AUTH = os.path.join(HOME, ".local", "share", "opencode", "auth.json")
OPENCODE_DB = os.path.join(HOME, ".local", "share", "opencode", "opencode.db")
# Segunda fuente: la sesión Go del agente pi (misma key del usuario, prestada
# solo-lectura como cualquier otra; del fichero solo se extrae `opencode-go`,
# el resto (codex, xai) ni se mira). Sin esto, el uso vía gateway (pi) es
# invisible para el log local del TUI.
PI_AUTH = os.path.join(HOME, ".pi", "agent", "auth.json")
OPENCODE_WINDOWS = (("rolling", "5h limit"), ("weekly", "Weekly limit"),
                     ("monthly", "Monthly limit"))


def _go_key_from_file(auth_path):
    try:
        with open(auth_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get("opencode-go")
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("key", "apiKey", "api_key", "token", "accessToken"):
            val = entry.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def read_opencode_go_key():
    """(key, fuente) — CLI de OpenCode primero, agente pi después."""
    for path, source in ((OPENCODE_AUTH, "opencode CLI"), (PI_AUTH, "pi agent")):
        key = _go_key_from_file(path)
        if key:
            return key, source
    return None, None


def read_opencode_local(db_path=OPENCODE_DB):
    """Conteo derivado del message log: tokens por provider este mes y hoy.
    Misma aritmética que OpenCodeGeminiUsage (total manda; si falta, suma
    input+output+reasoning+cache.read+cache.write — input excluye la caché).
    Devuelve (mes_total, hoy_total, {provider: (mes, hoy)}) o None si no hay db.
    `opencode stats` es la verdad local contra la que validar estos números."""
    import datetime
    now = datetime.datetime.now()
    _n, month_start_ms, _e, _d = _month_bounds()
    conn = _sqlite_ro(db_path)
    if conn is None:
        return None
    try:
        try:
            rows = conn.execute(
                "SELECT time_created,"
                " json_extract(data, '$.providerID'),"
                " json_extract(data, '$.tokens.total'),"
                " json_extract(data, '$.tokens.input'),"
                " json_extract(data, '$.tokens.output'),"
                " json_extract(data, '$.tokens.reasoning'),"
                " json_extract(data, '$.tokens.cache.read'),"
                " json_extract(data, '$.tokens.cache.write')"
                " FROM message WHERE json_extract(data, '$.role') = 'assistant'"
                " AND time_created >= ?", (month_start_ms,)).fetchall()
            use_cols = True
        except Exception:  # noqa: BLE001 - sin JSON1: filtrar en Python
            rows = conn.execute(
                "SELECT time_created, data FROM message WHERE time_created >= ?",
                (month_start_ms,)).fetchall()
            use_cols = False
    except Exception:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return ({}, 0, 0)
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    today = now.date()
    per = {}
    for row in rows:
        at = num(row[0])
        if at is None:
            continue
        if use_cols:
            provider = row[1] or "unknown"
            parts = row[2:]
            total = num(parts[0]) or 0
            tokens = int(total) if total > 0 else sum(
                int(num(p) or 0) for p in parts[1:])
        else:
            try:
                data = json.loads(row[1] or "{}")
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                continue
            provider = data.get("providerID") or "unknown"
            toks = data.get("tokens") or {}
            total = num(toks.get("total")) or 0
            if total > 0:
                tokens = int(total)
            else:
                cache = toks.get("cache") or {}
                tokens = sum(int(num(toks.get(k)) or 0)
                             for k in ("input", "output", "reasoning"))
                tokens += sum(int(num(cache.get(k)) or 0)
                              for k in ("read", "write"))
        if tokens <= 0:
            continue
        local = datetime.datetime.fromtimestamp(at / 1000.0)
        if (local.year, local.month) != (now.year, now.month):
            continue
        m, t = per.get(provider, (0, 0))
        m += tokens
        if local.date() == today:
            t += tokens
        per[provider] = (m, t)
    t_month = sum(m for m, _t in per.values())
    t_today = sum(t for _m, t in per.values())
    return (per, t_month, t_today)


def fetch_opencode(state):
    pid, name = "opencode", "OpenCode"
    token, source = read_opencode_go_key()
    if not token:
        return fetch_opencode_local_count(state, pid, name)
    if source == "pi agent":
        # La key de pi es de alcance inferencia: el endpoint de cuenta la
        # suele rechazar (401). Se intenta una vez y se cae al conteo local.
        snap = _fetch_opencode_endpoint(state, pid, name, token,
                                        fallback_local=True)
        return snap
    return _fetch_opencode_endpoint(state, pid, name, token,
                                    fallback_local=False)


def _fetch_opencode_endpoint(state, pid, name, token, fallback_local):
    wait = backoff_wait(state, pid)
    if wait > 0:
        snap = last_good(state, pid)
        if snap:
            snap = dict(snap); snap["status"] = "stale"
            return snap
        return _err(pid, name, "stale",
                    "rate limited, retry in %dm" % max(1, int(wait // 60)))
    try:
        _status, _retry, payload = http_get_json(
            OPENCODE_ENDPOINT,
            {"Authorization": "Bearer " + token, "Accept": "application/json"})
    except _HttpStatus as e:
        if e.status in (401, 403):
            if fallback_local:
                return fetch_opencode_local_count(state, pid, name)
            return _err(pid, name, "needsAuth",
                        "Go key rejected — sign in with OpenCode")
        if e.status == 429:
            consec = ((state.get("backoff", {}).get(pid) or {})
                      .get("consecutive", 0))
            wait = record_rate_limit(state, pid, e.retry_after, consec)
            snap = last_good(state, pid)
            if snap:
                snap = dict(snap); snap["status"] = "stale"
                return snap
            return _err(pid, name, "stale",
                        "rate limited, retry in %dm" % max(1, int(wait // 60)))
        return _stale_or_err(state, pid, name, "HTTP %s" % e.status)
    except Exception as e:  # noqa: BLE001 - degradar a stale
        return _stale_or_err(state, pid, name, _short(e))
    clear_backoff(state, pid)
    usage = payload.get("usage") if isinstance(payload, dict) else None
    windows = []
    if isinstance(usage, dict):
        for wid, label in OPENCODE_WINDOWS:
            entry = usage.get(wid)
            if not isinstance(entry, dict):
                continue
            pct = num(entry.get("percent"))
            if pct is None:
                continue
            windows.append({
                "id": wid, "label": label,
                "usedFraction": _clamp01(pct / 100.0 if pct > 1 else pct),
                "resetsAt": _to_ms_epoch(entry.get("resetsAt"))})
    if not windows:
        return _err(pid, name, "error", "unexpected usage shape")
    snap = {"id": pid, "displayName": name, "glyph": "opencode",
            "status": "ok", "windows": windows, "fetchedAt": now_ms(),
            "error": None}
    store_good(state, pid, snap)
    return snap


def fetch_opencode_local_count(state, pid, name):
    """Sin plan Go: anillo derivado del log local (fidelity manual — la
    tarjeta muestra ~conteos, nunca un % de límite)."""
    if not os.path.isfile(OPENCODE_DB):
        return None  # ni instalado: sin celda, es lo esperado
    reading = read_opencode_local()
    if reading is None:
        return None
    per, t_month, t_today = reading
    if not per:
        return None  # db sin mensajes: nada que medir, sin celda
    _now, _mstart, month_end, day_end = _month_bounds()
    windows = [
        {"id": "month", "label": "Month · local count",
         "usedFraction": None, "count": t_month, "resetsAt": month_end},
        {"id": "today", "label": "Today", "usedFraction": None,
         "count": t_today, "resetsAt": day_end},
    ]
    for provider in sorted(per):  # orden fijo: las filas no bailan
        m, _t = per[provider]
        if m > 0:
            windows.append({"id": "prov-" + str(provider),
                            "label": "%s · month" % provider,
                            "usedFraction": None, "count": m,
                            "resetsAt": month_end})
    return {"id": pid, "displayName": name, "glyph": "opencode",
            "status": "ok", "windows": windows, "fetchedAt": now_ms(),
            "error": None}


# ------------------------------------------------------------- Gemini (conteo local)
# Google no publica endpoint de uso para API key: el único número es el que
# cada herramienta anotó tras su llamada. Tres llevan ese log: Gemini CLI
# (~/.gemini/tmp/*/chats/*.jsonl), OpenCode (opencode.db, providerID google)
# y Hermes (~/.hermes/state.db). Sin red, sin secretos: la key nunca se lee.
# Fuente: GeminiAPIProvider.swift + Gemini(TokenUsage|CLIUsage) +
# OpenCodeGeminiUsage.swift + HermesGeminiUsage.swift.
GEMINI_CLI_ROOT = os.path.join(HOME, ".gemini", "tmp")
HERMES_DB = os.path.join(HOME, ".hermes", "state.db")
JSONL_SIZE_CAP = 64 * 1024 * 1024


def _month_bounds():
    import datetime
    now = datetime.datetime.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end = start.replace(year=now.year + 1, month=1)
    else:
        end = start.replace(month=now.month + 1)
    day_end = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return now, int(start.timestamp() * 1000), int(end.timestamp() * 1000), \
        int(day_end.timestamp() * 1000)


def read_gemini_cli(root=GEMINI_CLI_ROOT, now=None):
    """(tokens_mes, tokens_hoy, llamadas_mes) o None si nunca instalado."""
    import datetime
    if not os.path.isdir(root):
        return None
    now = now or datetime.datetime.now()
    month_start_ms = int(now.replace(day=1, hour=0, minute=0, second=0,
                                     microsecond=0).timestamp() * 1000)
    t_month = t_today = calls = 0
    today = now.date()
    try:
        projects = os.listdir(root)
    except OSError:
        return (0, 0, 0)
    for project in projects:
        chats = os.path.join(root, project, "chats")
        if not os.path.isdir(chats):
            continue
        try:
            sessions = [f for f in os.listdir(chats) if f.endswith(".jsonl")]
        except OSError:
            continue
        for session in sessions:
            path = os.path.join(chats, session)
            try:
                # Append-only: nada dentro es más nuevo que el fichero.
                if int(os.path.getmtime(path) * 1000) < month_start_ms:
                    continue
                if os.path.getsize(path) > JSONL_SIZE_CAP:
                    continue
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            answered = {}  # id -> registro: la 2ª escritura trae usageMetadata
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "gemini":
                    continue
                if not isinstance(rec.get("tokens"), dict) or not rec.get("id"):
                    continue
                answered[rec["id"]] = rec
            for rec in answered.values():
                at = _to_ms_epoch(rec.get("timestamp"))
                if at is None:
                    continue
                local = datetime.datetime.fromtimestamp(at / 1000.0)
                if (local.year, local.month) != (now.year, now.month):
                    continue
                total = num((rec.get("tokens") or {}).get("total")) or 0
                if total <= 0:  # abortada antes de responder: cuesta 0
                    continue
                t_month += int(total); calls += 1
                if local.date() == today:
                    t_today += int(total)
    return (t_month, t_today, calls)


def _sqlite_ro(path):
    import sqlite3
    from urllib.parse import quote
    if not os.path.isfile(path):
        return None
    for query in ("mode=ro", "immutable=1"):
        try:
            conn = sqlite3.connect("file:%s?%s" % (quote(path), query),
                                   uri=True, timeout=5)
            conn.execute("SELECT 1").fetchone()
            return conn
        except Exception:  # noqa: BLE001
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return None


def read_opencode_gemini(db_path=OPENCODE_DB, month_start_ms=None):
    """Solo providerID 'google' (bare key); 'google-vertex' es GCP aparte."""
    if month_start_ms is None:
        _n, month_start_ms, _e, _d = _month_bounds()
    import datetime
    conn = _sqlite_ro(db_path)
    if conn is None:
        return None
    try:
        try:
            rows = conn.execute(
                "SELECT time_created,"
                " json_extract(data, '$.tokens.total'),"
                " json_extract(data, '$.tokens.input'),"
                " json_extract(data, '$.tokens.output'),"
                " json_extract(data, '$.tokens.reasoning'),"
                " json_extract(data, '$.tokens.cache.read'),"
                " json_extract(data, '$.tokens.cache.write')"
                " FROM message WHERE json_extract(data, '$.role') = 'assistant'"
                " AND json_extract(data, '$.providerID') = 'google'"
                " AND time_created >= ?", (month_start_ms,)).fetchall()
            use_cols = True
        except Exception:  # noqa: BLE001 - sin JSON1: filtrar en Python
            rows = conn.execute(
                "SELECT time_created, data FROM message WHERE time_created >= ?",
                (month_start_ms,)).fetchall()
            use_cols = False
    except Exception:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return (0, 0, 0)
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    now = datetime.datetime.now()
    today = now.date()
    t_month = t_today = calls = 0
    for row in rows:
        at = num(row[0])
        if at is None:
            continue
        if use_cols:
            _t, parts = row[0], row[1:]
            total = num(parts[0]) or 0
            # input excluye caché: el fallback suma input+output+reasoning+
            # cache.read+cache.write (2834+51+17+93106+0 = total real).
            tokens = int(total) if total > 0 else sum(int(num(p) or 0) for p in parts[1:])
        else:
            try:
                data = json.loads(row[1] or "{}")
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                continue
            if data.get("providerID") != "google":
                continue
            toks = data.get("tokens") or {}
            total = num(toks.get("total")) or 0
            if total > 0:
                tokens = int(total)
            else:
                cache = toks.get("cache") or {}
                tokens = sum(int(num(toks.get(k)) or 0)
                             for k in ("input", "output", "reasoning"))
                tokens += sum(int(num(cache.get(k)) or 0)
                              for k in ("read", "write"))
        if tokens <= 0:
            continue
        local = datetime.datetime.fromtimestamp(at / 1000.0)
        if (local.year, local.month) != (now.year, now.month):
            continue
        t_month += tokens; calls += 1
        if local.date() == today:
            t_today += tokens
    return (t_month, t_today, calls)


def read_hermes_gemini(db_path=HERMES_DB):
    """billing_provider 'gemini' (sobrevive a GEMINI_BASE_URL override).
    reasoning excluido: Hermes ya lo cuenta dentro de output."""
    import datetime, time as _time
    conn = _sqlite_ro(db_path)
    if conn is None:
        return None
    now = datetime.datetime.now()
    month_start_s = int(now.replace(day=1, hour=0, minute=0, second=0,
                                    microsecond=0).timestamp())
    try:
        rows = conn.execute(
            "SELECT last_seen, input_tokens + cache_read_tokens +"
            " cache_write_tokens + output_tokens, api_call_count"
            " FROM session_model_usage WHERE billing_provider = 'gemini'"
            " AND last_seen >= ?", (month_start_s,)).fetchall()
    except Exception:  # noqa: BLE001
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return (0, 0, 0)
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    today = now.date()
    t_month = t_today = calls = 0
    for last_seen, tokens, n_calls in rows:
        at = num(last_seen)
        toks = int(num(tokens) or 0)
        if at is None or toks <= 0:
            continue
        local = datetime.datetime.fromtimestamp(at)  # epoch s, REAL
        if (local.year, local.month) != (now.year, now.month):
            continue
        t_month += toks; calls += int(num(n_calls) or 0)
        if local.date() == today:
            t_today += toks
    return (t_month, t_today, calls)


def fetch_gemini():
    pid, name = "gemini-api", "Gemini API"
    sources = []
    for sid, sname, reading in (
            ("cli", "Gemini CLI", read_gemini_cli()),
            ("opencode", "OpenCode", read_opencode_gemini()),
            ("hermes", "Hermes", read_hermes_gemini())):
        if reading is not None:
            sources.append((sid, sname, reading))
    if not sources:
        return None  # ningún log en disco: nada que medir, sin celda
    _now, _mstart, month_end, day_end = _month_bounds()
    t_month = sum(r[0] for _, _, r in sources)
    t_today = sum(r[1] for _, _, r in sources)
    windows = [
        {"id": "month", "label": "Month · per-token",
         "usedFraction": None, "count": t_month, "resetsAt": month_end},
        {"id": "today", "label": "Today", "usedFraction": None,
         "count": t_today, "resetsAt": day_end},
    ]
    # Filas por fuente solo si aportan: una fuente instalada pero a cero no
    # informa nada y confunde (p. ej. OpenCode usado con vendors no-Google).
    # El orden fijo evita que las filas bailen entre refreshes.
    for sid, sname, reading in sources:  # filas que hacen el total comprobable
        if reading[0] > 0:
            windows.append({"id": sid, "label": "%s · month" % sname,
                            "usedFraction": None, "count": reading[0],
                            "resetsAt": month_end})
    return {"id": pid, "displayName": name, "glyph": "gemini",
            "status": "ok", "windows": windows, "fetchedAt": now_ms(),
            "error": None}


# ------------------------------------------------------------- Grok
GROK_ENDPOINT = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
# La sesión que el CLI firma vía auth.x.ai en ~/.grok/auth.json (solo lectura;
# refrescar es trabajo de Grok). El fichero va keyed por `issuer::client_id`:
# solo vale sesión del propio xAI (un IdP de cliente apuntaría a un proxy
# privado — jamás se manda esa credencial al endpoint público).
# Respuesta: {config: {currentPeriod: {type, start, end}, creditUsagePercent,
#  productUsage: [{product, usagePercent}], billingPeriodEnd}}. El payload de
# créditos es el anillo: allowance semanal de Grok Build.
# Fuente: GrokCredentials.swift + GrokLocalProvider.swift + GrokUsage.swift.
GROK_AUTH = os.path.join(HOME, ".grok", "auth.json")
GROK_ISSUER = "https://auth.x.ai"


def read_grok_credentials(auth_path=GROK_AUTH):
    """(token, expired, email) o (None, False, None)."""
    try:
        with open(auth_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, False, None
    if not isinstance(data, dict):
        return None, False, None
    trusted = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if key.startswith(GROK_ISSUER) or entry.get("oidc_issuer") == GROK_ISSUER:
            trusted.append(entry)
    if not trusted:
        return None, False, None
    now = now_ms()
    live = [e for e in trusted
            if (_to_ms_epoch(e.get("expires_at")) or 0) > now]
    entry = live[0] if live else trusted[0]
    token = entry.get("key")
    if not isinstance(token, str) or not token.strip():
        return None, False, None
    exp = _to_ms_epoch(entry.get("expires_at"))
    expired = bool(exp is not None and exp <= now)
    email = entry.get("email") if isinstance(entry.get("email"), str) else None
    return token.strip(), expired, email


def _grok_humanize(name):
    out = ""
    for ch in str(name):
        if ch.isupper() and out:
            out += " "
        out += ch
    return out or "Usage"


def _grok_windows(payload):
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        return None  # forma inesperada: lo decide el llamador
    period = config.get("currentPeriod")
    period = period if isinstance(period, dict) else {}
    reset = (_to_ms_epoch(period.get("end"))
             or _to_ms_epoch(config.get("billingPeriodEnd")))
    windows = []
    pct = num(config.get("creditUsagePercent"))
    if pct is not None:
        products = config.get("productUsage")
        label = "Grok Build"
        if isinstance(products, list) and products and isinstance(products[0], dict):
            label = _grok_humanize(products[0].get("product") or label)
        windows.append({"id": "credits", "label": label,
                        "usedFraction": _clamp01(pct / 100.0),
                        "resetsAt": reset})
    else:
        products = config.get("productUsage")
        if isinstance(products, list):
            for product in products:
                if not isinstance(product, dict):
                    continue
                frac = num(product.get("usagePercent"))
                if frac is None:
                    continue
                name = product.get("product")
                wid = "credits" if not windows else str(name or len(windows))
                windows.append({"id": wid, "label": _grok_humanize(name),
                                "usedFraction": _clamp01(frac / 100.0),
                                "resetsAt": reset})
    if not windows and "WEEKLY" in str(period.get("type") or ""):
        # Pool semanal sin uso aún: barra 0% con fin de periodo (como /usage).
        windows.append({"id": "credits", "label": "Weekly limit",
                        "usedFraction": 0.0,
                        "resetsAt": _to_ms_epoch(period.get("end")) or reset})
    return windows


def fetch_grok(state):
    pid, name = "grok", "Grok"
    if not os.path.isfile(GROK_AUTH):
        return None  # no instalado: sin celda, es lo esperado
    wait = backoff_wait(state, pid)
    if wait > 0:
        snap = last_good(state, pid)
        if snap:
            snap = dict(snap); snap["status"] = "stale"
            return snap
        return _err(pid, name, "stale",
                    "rate limited, retry in %dm" % max(1, int(wait // 60)))
    token, expired, _email = read_grok_credentials()
    if not token:
        return _err(pid, name, "needsAuth", "run grok login")
    if expired:
        # credentialExpired ≠ signed out: la última lectura sigue valiendo.
        snap = last_good(state, pid)
        if snap:
            snap = dict(snap); snap["status"] = "stale"
            return snap
        return _err(pid, name, "stale",
                    "token expired — Grok refreshes it on next use")
    try:
        _status, _retry, payload = http_get_json(
            GROK_ENDPOINT,
            {"Authorization": "Bearer " + token,
             "X-XAI-Token-Auth": "xai-grok-cli",
             "Accept": "application/json"})
    except _HttpStatus as e:
        if e.status in (401, 403):
            return _err(pid, name, "needsAuth",
                        "session rejected — run grok login")
        if e.status == 429:
            record_rate_limit(state, pid, 60, 0)  # fijo 60 s (upstream)
            snap = last_good(state, pid)
            if snap:
                snap = dict(snap); snap["status"] = "stale"
                return snap
            return _err(pid, name, "stale", "rate limited, retry in 1m")
        return _stale_or_err(state, pid, name, "HTTP %s" % e.status)
    except Exception as e:  # noqa: BLE001 - degradar a stale
        return _stale_or_err(state, pid, name, _short(e))
    clear_backoff(state, pid)
    windows = _grok_windows(payload if isinstance(payload, dict) else {})
    if not windows:
        if windows is None:
            return _stale_or_err(state, pid, name, "unexpected billing shape")
        return _err(pid, name, "error",
                    "Grok has nothing metered on this account yet")
    snap = {"id": pid, "displayName": name, "glyph": "grok",
            "status": "ok", "windows": windows, "fetchedAt": now_ms(),
            "error": None}
    store_good(state, pid, snap)
    return snap


# ------------------------------------------------------------- Pi spend
# Gasto real trabajando en pi, reconstruido de sus propios logs
# (~/.pi/agent/sessions/*/*.jsonl: mensajes assistant con usage{...} +
# cost{total}). Cero red, cero credenciales: es la misma contabilidad que
# muestra la barra de pi. Cubre TODOS los providers (opencode-go, codex,
# xai...) porque cuenta lo facturado, no cuotas. Sin sesiones: sin celda.
PI_SESSIONS = os.path.join(HOME, ".pi", "agent", "sessions")


def read_pi_spend():
    """(mes_usd, hoy_usd, mes_tok, hoy_tok, {provider: (usd_mes,)}) o None."""
    import datetime
    import glob as _glob
    if not os.path.isdir(PI_SESSIONS):
        return None
    now = datetime.datetime.now()
    today = now.date()
    m_usd = t_usd = 0.0
    m_tok = t_tok = 0
    per = {}
    found = False
    for path in _glob.glob(os.path.join(PI_SESSIONS, "*", "*.jsonl")):
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "message":
                    continue
                m = e.get("message") or {}
                if m.get("role") != "assistant":
                    continue
                u = m.get("usage") or {}
                cost = num((u.get("cost") or {}).get("total")) or 0.0
                tok = int(num(u.get("totalTokens")) or 0)
                if not cost and not tok:
                    continue
                found = True
                try:
                    day = datetime.datetime.fromisoformat(
                        str(e.get("timestamp", "")).replace("Z", "+00:00")
                    ).astimezone().date()
                except ValueError:
                    continue
                if (day.year, day.month) != (now.year, now.month):
                    continue
                m_usd += cost; m_tok += tok
                if day == today:
                    t_usd += cost; t_tok += tok
                p = m.get("provider") or "unknown"
                c, t = per.get(p, (0.0, 0))
                per[p] = (c + cost, t + tok)
    if not found:
        return None
    return (m_usd, t_usd, m_tok, t_tok, per)


def fetch_pi_spend():
    pid, name = "pi", "Pi spend"
    reading = read_pi_spend()
    if reading is None:
        return None
    m_usd, t_usd, _m_tok, _t_tok, per = reading
    _now, _mstart, month_end, day_end = _month_bounds()
    windows = [
        {"id": "month", "label": "Month · pi logs",
         "usedFraction": None, "count": round(m_usd, 4), "unit": "usd",
         "resetsAt": month_end},
        {"id": "today", "label": "Today", "usedFraction": None,
         "count": round(t_usd, 4), "unit": "usd", "resetsAt": day_end},
    ]
    for provider in sorted(per):  # orden fijo: las filas no bailan
        cost = per[provider][0]
        if cost > 0:
            windows.append({"id": "prov-" + str(provider),
                            "label": "%s · month" % provider,
                            "usedFraction": None, "count": round(cost, 4),
                            "unit": "usd", "resetsAt": month_end})
    return {"id": pid, "displayName": name, "glyph": "pi",
            "status": "ok", "windows": windows, "fetchedAt": now_ms(),
            "error": None}


# ------------------------------------------------------------- helpers
def _clamp01(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _to_ms_epoch(value):
    """Acepta epoch s/ms o RFC3339; None si no se entiende (nunca inventar)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e14:  # ns
            v /= 1e6
        elif v > 1e11:  # ms
            pass
        else:  # s
            v *= 1000.0
        return int(v)
    if isinstance(value, str):
        try:
            from datetime import datetime, timezone
            text = value.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _short(exc):
    msg = str(exc)
    if len(msg) > 90:
        msg = msg[:87] + "..."
    return msg or exc.__class__.__name__


def family_of(pid):
    if pid in FAMILIES:
        return pid
    for fam in FAMILIES:
        if pid.startswith(fam + "-"):
            return fam
    return pid


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
            if isinstance(cfg, dict):
                return cfg
    except (OSError, ValueError):
        pass
    return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    os.replace(tmp, CONFIG_FILE)
    return cfg


PLACEMENT_DEFAULTS = {"screen": "auto", "edge": "right"}


def get_placement(cfg=None):
    """Placement normalizado: screen es nombre, `primary` o `auto`; edge right|left."""
    cfg = cfg if cfg is not None else load_config()
    raw = cfg.get("placement")
    raw = raw if isinstance(raw, dict) else {}
    screen = raw.get("screen")
    screen = screen.strip() if isinstance(screen, str) and screen.strip() else "auto"
    edge = raw.get("edge")
    edge = edge if edge in ("right", "left") else "right"
    return {"screen": screen, "edge": edge}


def disabled_families(cfg=None):
    raw = (cfg if cfg is not None else load_config()).get("disabled", [])
    return set(family_of(str(x)) for x in raw if isinstance(raw, list))


def _disabled_stub(pid, name, glyph):
    return {"id": pid, "displayName": name, "glyph": glyph,
            "status": "disabled", "windows": [], "fetchedAt": now_ms(),
            "error": "off in settings — enable to resume"}


def _family_available(fam):
    """Instalado/detectado, sin red: solo decide si el stub disabled existe."""
    if fam == "claude":
        return bool(discover_claude_profiles())
    if fam == "codex":
        return bool(discover_codex_profiles())
    if fam == "cursor":
        return os.path.isfile(CURSOR_VSCDB)
    if fam == "opencode":
        return os.path.isfile(OPENCODE_AUTH) or os.path.isfile(OPENCODE_DB)
    if fam == "gemini-api":
        return (os.path.isdir(GEMINI_CLI_ROOT) or os.path.isfile(OPENCODE_DB)
                or os.path.isfile(HERMES_DB))
    if fam == "grok":
        return os.path.isfile(GROK_AUTH)
    if fam == "pi":
        return os.path.isdir(PI_SESSIONS)
    return True


def _err(pid, name, status, error):
    glyph = ("claude" if pid.startswith("claude")
             else "cursor" if pid.startswith("cursor")
             else "opencode" if pid.startswith("opencode") else "codex")
    return {"id": pid, "displayName": name, "glyph": glyph,
            "status": status, "windows": [], "fetchedAt": now_ms(),
            "error": error}


def _stale_or_err(state, pid, name, error):
    snap = last_good(state, pid)
    if snap:
        snap = dict(snap); snap["status"] = "stale"
        return snap
    return _err(pid, name, "error", error)


def poll_all(state):
    """Una lectura de todos los perfiles descubiertos, en orden estable.
    Las familias apagadas emiten stub `disabled` (sin red) para que Ajustes
    pueda volver a encenderlas."""
    off = disabled_families()
    results = []
    for pid, name, path in discover_claude_profiles():
        if "claude" in off:
            results.append(_disabled_stub(pid, name, "claude"))
        else:
            results.append(fetch_claude(pid, name, path, state))
    for pid, name, path in discover_codex_profiles():
        if "codex" in off:
            results.append(_disabled_stub(pid, name, "codex"))
        else:
            results.append(fetch_codex(pid, name, path, state))
    singles = [
        ("cursor", lambda: fetch_cursor(state)),
        ("opencode", lambda: fetch_opencode(state)),
        ("gemini-api", fetch_gemini),
        ("grok", lambda: fetch_grok(state)),
        ("pi", fetch_pi_spend),
    ]
    for fam, fn in singles:
        if fam in off:
            if _family_available(fam):
                results.append(_disabled_stub(
                    fam, FAMILY_NAMES[fam], FAMILY_GLYPHS[fam]))
            continue
        snap = fn()
        if snap is not None:
            results.append(snap)
    return results


# ------------------------------------------------------------- comandos
def cmd_poll():
    print(json.dumps(poll_all(load_state())))
    return 0


def cmd_watch():
    state = load_state()
    print(json.dumps(poll_all(state)), flush=True)
    interval = POLL_IDLE_SECS
    while True:
        time.sleep(interval)
        try:
            print(json.dumps(poll_all(state)), flush=True)
        except BrokenPipeError:
            return 0


def cmd_doctor():
    ok = True
    print("usage-notch doctor v%s\n" % VERSION)
    pl = get_placement()
    print("placement: screen=%s edge=%s (%s)\n" % (
        pl["screen"], pl["edge"], CONFIG_FILE))
    for pid, name, path in discover_claude_profiles():
        token, expired, tpath = read_claude_token(path)
        if token:
            masked = token[:4] + "..." + token[-4:] if len(token) > 12 else "present"
            print("[Claude] %s\n  profile: %s\n  credential: %s (%s, %s)" % (
                name, path, tpath or "?", masked,
                "expired — Claude Code lo refresca al usarse" if expired else "vigente"))
        else:
            ok = False
            print("[Claude] %s\n  profile: %s\n  credential: AUSENTE "
                  "(inicia sesión con el CLI de Claude Code)" % (name, path))
    if not discover_claude_profiles():
        print("[Claude] no instalado (sin ~/.claude) — sin celda, es lo esperado")
    for pid, name, path in discover_codex_profiles():
        token, account, tpath = read_codex_credential(path)
        if token:
            print("[Codex] %s\n  profile: %s\n  credential: %s (account %s)" % (
                name, path, tpath, account or "?"))
        else:
            ok = False
            print("[Codex] %s\n  profile: %s\n  credential: AUSENTE o sin token "
                  "(inicia sesión con el CLI de Codex)" % (name, path))
    if not discover_codex_profiles():
        print("[Codex] no instalado (sin ~/.codex) — sin celda, es lo esperado")
    cookie, plan = read_cursor_session()
    if os.path.isfile(CURSOR_VSCDB):
        if cookie:
            print("[Cursor]\n  store: %s\n  session: prestada (%d chars, plan=%s)" % (
                CURSOR_VSCDB, len(cookie), plan or "?"))
        else:
            ok = False
            print("[Cursor]\n  store: %s\n  session: AUSENTE o ilegible "
                  "(abre Cursor e inicia sesión)" % CURSOR_VSCDB)
    else:
        print("[Cursor] no instalado (sin state.vscdb) — sin celda, es lo esperado")
    key, source = read_opencode_go_key()
    if key:
        extra = (" (alcance inferencia: si el endpoint la rechaza, "
                 "se usa conteo local)" if source == "pi agent" else "")
        print("[OpenCode]\n  Go key vía %s%s" % (source, extra))
    else:
        print("[OpenCode]\n  Go key ausente (celda local derivada)")
    if os.path.isfile(OPENCODE_AUTH):
        print("  vendors en %s: solo-lectura, nunca se reclaman" % OPENCODE_AUTH)
    local = read_opencode_local()
    if local is not None:
        per, t_month, t_today = local
        print("  local: %s" % "; ".join(
            "%s=%s" % (k, v[0]) for k, v in sorted(per.items())))
        print("  local: mes=%d hoy=%d (contrastar: opencode stats)" % (
                    t_month, t_today))
    else:
        print("[OpenCode] no instalado (sin auth.json) — sin celda, es lo esperado")
    token, expired, email = read_grok_credentials()
    if os.path.isfile(GROK_AUTH):
        if token:
            print("[Grok]\n  auth: %s (%s, %s)" % (
                GROK_AUTH, email or "?",
                "expired — Grok lo refresca al usarse" if expired else "vigente"))
        else:
            print("[Grok]\n  auth: %s (sin sesión xAI — `grok login`)" % GROK_AUTH)
    else:
        print("[Grok] no instalado (sin ~/.grok/auth.json) — sin celda")
    spend = fetch_pi_spend()
    if spend is not None:
        print("[Pi spend] logs locales: mes=$%.2f (todas las sesiones)" %
              spend["windows"][0].get("count", 0))
    else:
        print("[Pi spend] sin sesiones — sin celda")
    gem = fetch_gemini()
    if gem is not None:
        print("[Gemini API] conteo local: " + "; ".join(
            "%s=%s" % (w["id"], w.get("count", 0)) for w in gem["windows"]))
    else:
        print("[Gemini API] sin logs (ni CLI, ni OpenCode, ni Hermes) — sin celda")
    print("\nEndpoints (fijos, https):\n  %s\n  %s\n  %s\n  %s\n  %s" % (
        CLAUDE_ENDPOINT, CODEX_ENDPOINT, CURSOR_ENDPOINT, OPENCODE_ENDPOINT,
        GROK_ENDPOINT))
    print("\nEstado: %s" % STATE_FILE)
    try:
        print(json.dumps(poll_all(load_state()), indent=2, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - doctor nunca debe morir
        ok = False
        print("\nERROR en poll: %s" % _short(e))
    return 0 if ok else 1


def cmd_config_set(cfg, argv):
    """config set placement.screen <nombre|primary|auto> |
    config set placement.edge <right|left>."""
    if len(argv) != 2 or not argv[0].startswith("placement."):
        print("uso: usage.py config set placement.screen <nombre|primary|auto>",
              file=sys.stderr)
        print("     usage.py config set placement.edge <right|left>",
              file=sys.stderr)
        return 2
    key = argv[0][len("placement."):]
    val = argv[1]
    pl = cfg.get("placement")
    if not isinstance(pl, dict):
        pl = {}
        cfg["placement"] = pl
    if key == "screen":
        val = val.strip() if isinstance(val, str) else ""
        if not val or len(val) > 64:
            print("screen inválido (1-64 caracteres)", file=sys.stderr)
            return 2
        pl["screen"] = val
    elif key == "edge":
        if val not in ("right", "left"):
            print("edge debe ser right|left", file=sys.stderr)
            return 2
        pl["edge"] = val
    else:
        print("clave desconocida: %s (placement.screen|placement.edge)" % argv[0],
              file=sys.stderr)
        return 2
    save_config(cfg)
    print(json.dumps(cfg))
    return 0


def cmd_config(argv):
    """config | config toggle|enable|disable <id|familia> | config set ... ."""
    cfg = load_config()
    if not isinstance(cfg.get("disabled"), list):
        cfg["disabled"] = []
    if len(argv) >= 1 and argv[0] == "set":
        return cmd_config_set(cfg, argv[1:])
    if len(argv) >= 2:
        action, fam = argv[0], family_of(argv[1])
        if fam not in FAMILIES:
            print("familia desconocida: %s (%s)" % (argv[1], ", ".join(FAMILIES)),
                  file=sys.stderr)
            return 2
        off = [x for x in cfg["disabled"] if family_of(str(x)) != fam]
        if action == "toggle":
            if fam not in disabled_families(cfg):
                off.append(fam)
        elif action == "disable":
            if fam not in disabled_families(cfg):
                off.append(fam)
        elif action == "enable":
            pass  # ya quitado arriba: encender es quitar de la lista
        else:
            print("uso: usage.py config [toggle|enable|disable] <provider>",
                  file=sys.stderr)
            return 2
        cfg["disabled"] = sorted(set(off))
        save_config(cfg)
    print(json.dumps(cfg))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "poll"
    if cmd == "poll":
        return cmd_poll()
    if cmd == "watch":
        return cmd_watch()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "config":
        return cmd_config(argv[2:])
    print("uso: usage.py [poll|watch|doctor|config]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
