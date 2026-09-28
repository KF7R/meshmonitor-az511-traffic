#!/usr/bin/env python3
# mm_meta:
#   mode: lib
#   name: Shared library (common)
#   language: Python
"""
common.py — Shared library for MeshMonitor scripts (gen2).

Provides:
  • HTTP fetch with retry + backoff
  • Text formatting and clamping for mesh broadcasts
  • Weather and space-weather emoji mapping
  • Numerical conversions (C/F, m/s/mph, m/mi)
  • State file management (read/write JSON with TTL caching)
  • Smart dedup with numerical bucketing + max-silence backstop
  • Standardized JSON response output
  • Consistent stderr logging

All MM scripts should import from this module rather than duplicating helpers.
Sibling-file import pattern (all scripts live flat at /data/scripts/):

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from common import (...)

Location:  /data/scripts/common.py
Repo:      spartan/mesh-scripts (Forgejo)
State dir: /data/scripts/state/  (override with MM_STATE_DIR env var)
"""
import os, json, re, sys, time, hashlib, sqlite3
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

__version__ = "1.1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration constants
# ─────────────────────────────────────────────────────────────────────────────
USER_AGENT = os.getenv(
    "USER_AGENT",
    ""
)

# The radio's usable text payload is ~200 BYTES, and the byte guard below
# already enforces it exactly. MAX_LEN previously sat at 180 chars as extra
# "headroom", but that char cap was pure loss on top of the byte budget — and
# it's what amputated a live Extreme Heat Warning's expiry line into
# "Exp Mon Jul..." (2026-07-22). The wire is 200: let bytes be the authority.
MAX_LEN = int(os.getenv("MM_MAX_LEN", "200"))
# Meshtastic's text payload limit is BYTE-based (~200 usable). clamp() enforces
# this too, so an emoji-heavy message under the char cap can't overflow the packet.
MAX_BYTES = int(os.getenv("MM_MAX_BYTES", "200"))

STATE_DIR = Path(os.getenv("MM_STATE_DIR", "/data/scripts/state"))

HTTP_TIMEOUT  = 20
HTTP_RETRIES  = 3
HTTP_BACKOFF  = 2.0  # seconds, multiplied by attempt number

NWS_ACCEPT = "application/geo+json"

# ─────────────────────────────────────────────────────────────────────────────
# Logging — consistent format across all scripts
# ─────────────────────────────────────────────────────────────────────────────
def log(tag: str, msg: str) -> None:
    """Log to stderr with consistent [tag] prefix."""
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)

def log_error(tag: str, msg: str) -> None:
    """Log an error to stderr with consistent [tag] ERROR: prefix."""
    print(f"[{tag}] ERROR: {msg}", file=sys.stderr, flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# HTTP fetch with retry + backoff
# ─────────────────────────────────────────────────────────────────────────────
def http_get_json(
    url: str,
    timeout: int = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
    headers: dict | None = None,
    accept: str | None = None,
):
    """
    Fetch JSON from url with up to `retries` attempts and linear backoff.
    Raises the last exception on final failure.
    """
    req_headers = {"User-Agent": USER_AGENT}
    if accept:
        req_headers["Accept"] = accept
    if headers:
        req_headers.update(headers)

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 4xx is a hard answer — retrying a 404 just burns the time budget.
            # 5xx may be transient; let it take the retry path below.
            if e.code < 500:
                raise
            last_exc = e
            if attempt < retries:
                time.sleep(HTTP_BACKOFF * attempt)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(HTTP_BACKOFF * attempt)
    if last_exc:
        raise last_exc
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Text formatting
# ─────────────────────────────────────────────────────────────────────────────
def clamp(s: str, max_len: int = MAX_LEN, max_bytes: int = MAX_BYTES) -> str:
    """
    Strip blank lines, collapse inline whitespace, and truncate to fit BOTH
    max_len characters AND max_bytes UTF-8 bytes (Meshtastic's payload limit is
    byte-based; emoji are 3-4 bytes each). Truncation always lands on a whole
    character — never a partial multi-byte sequence — and appends '...'.
    """
    lines = [re.sub(r"[ \t]+", " ", (ln or "").strip())
             for ln in (s or "").splitlines()]
    s = "\n".join(ln for ln in lines if ln).strip()

    truncated = False
    if len(s) > max_len:
        s = s[:max_len - 3]
        truncated = True
    # Byte-budget guard. Reserve the 3 '...' bytes only once truncation is
    # actually REQUIRED — the old unconditional `> max_bytes - 3` loop
    # truncated messages that already fit exactly, and from the END, which is
    # where composers put the expiry line. Caught live 2026-07-22: a heat
    # warning composed to exactly 200 bytes went OTA as 'Exp Mon Jul...'.
    if len(s.encode("utf-8")) > max_bytes:
        truncated = True
    if truncated:
        while s and len(s.encode("utf-8")) > max_bytes - 3:
            s = s[:-1]
    return (s.rstrip() + "...") if truncated else s

def deg_to_cardinal(deg) -> str:
    """Convert degrees to 16-point compass bearing. Returns 'VRB' for None."""
    if deg is None:
        return "VRB"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    try:
        return dirs[int((float(deg) % 360.0 + 11.25) / 22.5) % 16]
    except (ValueError, TypeError):
        return "VRB"

# ─────────────────────────────────────────────────────────────────────────────
# Unit conversions
# ─────────────────────────────────────────────────────────────────────────────
def c_to_f(c: float) -> float:
    return (c * 9 / 5) + 32

def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9

def ms_to_mph(ms: float) -> float:
    return ms * 2.2369362920544

def m_to_mi(m: float) -> float:
    return m * 0.00062137119

# ─────────────────────────────────────────────────────────────────────────────
# Local time — ONE definition, used by every script
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ NEVER call a bare `.astimezone()` (no argument). It resolves against the
# HOST's timezone, so the same script prints different times on different
# machines — and most servers run UTC, which is 7 hours off Arizona local.
#
# That is not just a formatting nit: `is_night()` below is a 6am/6pm rule, and
# evaluating it in UTC on a UTC-7 mesh does not shift the answer, it INVERTS it
# for 12 of every 24 hours — putting a 🌙 on a clear midday forecast. Verified on
# a UTC host: the old code returned is_night("2026-08-17T19:00:00Z") = True for
# what is noon in Arizona.
#
# Set TZ in the environment to override; otherwise these scripts state Arizona.
def _resolve_local_tz():
    """TZ from the environment, else Arizona, else UTC. Never raises — a bad TZ
    value must not take a broadcast down."""
    for name in (os.getenv("TZ"), "America/Phoenix"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except Exception:
            continue
    return timezone.utc


TZ = _resolve_local_tz()


def now_local() -> datetime:
    """Current time, aware, in the script-local zone."""
    return datetime.now(TZ)


def to_local(dt: datetime) -> datetime:
    """Convert an aware datetime to the script-local zone. A naive input is read
    as UTC — never as host-local, which is the very inheritance this replaces."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


# ─────────────────────────────────────────────────────────────────────────────
# Day/night + weather emoji
# ─────────────────────────────────────────────────────────────────────────────
def is_night(ts_iso: str | None = None) -> bool:
    """
    Heuristic day/night check: 6am-6pm = day.
    Pass ISO timestamp string to use observation time; omit for current local time.
    """
    try:
        if ts_iso:
            dt = to_local(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")))
            return dt.hour < 6 or dt.hour >= 18
    except Exception:
        pass
    h = now_local().hour
    return h < 6 or h >= 18

def wx_emoji(desc: str, *, night: bool | None = None) -> str:
    """Map a weather description string to a representative emoji."""
    d = (desc or "").lower()
    # AZ-specific priority
    if any(k in d for k in ("dust", "sand", "haboob")):
        return "🌫️"
    if "flash flood" in d:
        return "🌊"
    if "heat" in d and ("warning" in d or "advisory" in d):
        return "🌡️"
    # Severe
    if any(k in d for k in ("thunder", "t-storm", "lightning")):
        return "⛈️"
    if "hail" in d:
        return "🌨️"
    if any(k in d for k in ("snow", "blizzard", "sleet", "flurries", "ice")):
        return "❄️"
    if any(k in d for k in ("heavy rain", "downpour")):
        return "🌧️"
    if any(k in d for k in ("rain", "drizzle", "showers", "sprinkles")):
        return "🌦️"
    if "fog" in d:
        return "🌫️"
    if any(k in d for k in ("haze", "smoke")):
        return "🌫️"
    if any(k in d for k in ("wind", "breezy", "gust")):
        return "💨"
    if "overcast" in d:
        return "☁️"
    if any(k in d for k in ("partly cloudy", "mostly cloudy", "cloudy")):
        return "⛅"
    if any(k in d for k in ("clear", "sunny", "fair")):
        if night is None:
            night = is_night()
        return "🌙" if night else "☀️"
    return "ℹ️"

# ─────────────────────────────────────────────────────────────────────────────
# Weather math
# ─────────────────────────────────────────────────────────────────────────────
def heat_index_f(temp_f: float, rh: float):
    """
    Rothfusz heat index regression (NWS standard).
    Only valid when temp >= 80°F AND RH >= 40%. Returns None otherwise.
    """
    if temp_f < 80 or rh < 40:
        return None
    return (
        -42.379
        + 2.04901523  * temp_f
        + 10.14333127 * rh
        - 0.22475541  * temp_f * rh
        - 0.00683783  * temp_f ** 2
        - 0.05481717  * rh ** 2
        + 0.00122874  * temp_f ** 2 * rh
        + 0.00085282  * temp_f * rh ** 2
        - 0.00000199  * temp_f ** 2 * rh ** 2
    )

def kp_status(kp: float) -> str:
    """NOAA G-scale description for a planetary K-index value."""
    if kp >= 9: return "G5 Extreme 🟣"
    if kp >= 8: return "G4 Severe 🔴"
    if kp >= 7: return "G3 Strong 🔴"
    if kp >= 6: return "G2 Moderate 🟠"
    if kp >= 5: return "G1 Minor Storm 🟡"
    if kp >= 4: return "Unsettled ⚠️"
    return "Quiet 🟢"

# ─────────────────────────────────────────────────────────────────────────────
# State files (simple JSON read/write)
# ─────────────────────────────────────────────────────────────────────────────
def _state_path(name: str) -> Path:
    return STATE_DIR / name

# State backend: when Mesh Commander runs a script it injects MM_DB_PATH (its
# SQLite file), so we persist dedup/"already-sent" state in a `script_state`
# table inside the single backed-up DB — surviving reboot, backup/restore, and
# script moves. With no MM_DB_PATH (a script run standalone), we transparently
# fall back to the JSON state files below; the load_state/save_state API is
# identical either way. State is stored as one JSON blob per state name.
MM_DB_PATH = os.getenv("MM_DB_PATH")

def _db_on() -> bool:
    return bool(MM_DB_PATH) and os.path.exists(MM_DB_PATH)

def _db_open():
    c = sqlite3.connect(MM_DB_PATH, timeout=10)
    c.execute("PRAGMA busy_timeout=10000")
    c.execute(
        "CREATE TABLE IF NOT EXISTS script_state ("
        "scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT, "
        "first_seen TEXT NOT NULL, updated TEXT NOT NULL, "
        "PRIMARY KEY (scope, key))")
    return c

def _db_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_state(name: str, default=None) -> dict:
    """Read persisted state. Uses the Mesh Commander DB (script_state) when
    MM_DB_PATH is set — surviving reboot/backup/restore — else a JSON file.
    Returns default (or {}) if missing/malformed."""
    if default is None:
        default = {}
    if _db_on():
        try:
            c = _db_open()
            try:
                row = c.execute(
                    "SELECT value FROM script_state WHERE scope=? AND key='__blob__'",
                    (name,)).fetchone()
            finally:
                c.commit()
                c.close()
            if row and row[0]:
                return json.loads(row[0])
            # No DB row yet — seamless cutover: if a legacy JSON state file exists
            # (pre-v0.10.0), import it into the DB so the script keeps its dedup
            # memory and doesn't re-blast every active alert once on the upgrade.
            p = _state_path(name)
            if p.exists():
                try:
                    legacy = json.loads(p.read_text("utf-8"))
                    save_state(name, legacy)
                    return legacy
                except Exception as e:
                    log_error("common", f"legacy state migrate({name}) failed: {e}")
            return default
        except Exception as e:
            log_error("common", f"DB load_state({name}) failed, trying file: {e}")
    try:
        p = _state_path(name)
        if p.exists():
            return json.loads(p.read_text("utf-8"))
    except Exception as e:
        log_error("common", f"load_state({name}) failed: {e}")
    return default

def _atomic_write_json(path: Path, data) -> None:
    """Write JSON to a temp file then os.replace() into place — atomic, so a
    process killed mid-write (e.g. MM's ~30s timeout SIGKILL) can never leave a
    truncated/corrupt file that would wipe dedup state on the next load."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)

def save_state(name: str, data) -> None:
    """Persist state to the Mesh Commander DB (script_state) when MM_DB_PATH is
    set, else a JSON file (atomic). Same API either way."""
    if _db_on():
        try:
            now = _db_now()
            c = _db_open()
            try:
                c.execute(
                    "INSERT INTO script_state (scope,key,value,first_seen,updated) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(scope,key) DO UPDATE SET "
                    "value=excluded.value, updated=excluded.updated",
                    (name, "__blob__", json.dumps(data), now, now))
            finally:
                c.commit()
                c.close()
            return
        except Exception as e:
            log_error("common", f"DB save_state({name}) failed, trying file: {e}")
    try:
        _atomic_write_json(_state_path(name), data)
    except Exception as e:
        log_error("common", f"save_state({name}) failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# TTL caches (for slow/expensive API responses)
# ─────────────────────────────────────────────────────────────────────────────
def load_cache(name: str, ttl_seconds: int):
    """
    Read a TTL'd cache file. Returns cached `data` if fresh, else None.
    Cache shape: {"data": <anything>, "ts": <epoch_seconds>}.
    """
    try:
        p = _state_path(name)
        if not p.exists():
            return None
        blob = json.loads(p.read_text("utf-8"))
        if time.time() - blob.get("ts", 0) < ttl_seconds:
            return blob.get("data")
    except Exception as e:
        log_error("common", f"load_cache({name}) failed: {e}")
    return None

def save_cache(name: str, data) -> None:
    """Write a TTL'd cache file (atomically) stamped with the current epoch time."""
    try:
        _atomic_write_json(_state_path(name), {"data": data, "ts": time.time()})
    except Exception as e:
        log_error("common", f"save_cache({name}) failed: {e}")

def fetch_with_fallback(url: str, cache_name: str, tag: str = "common", **kwargs):
    """
    Fetch JSON with stale-cache fallback. Tries HTTP first; on success caches
    the result. On HTTP failure, returns the most recent cache regardless of
    age (with a log warning). Returns None only if both fail.
    """
    try:
        data = http_get_json(url, **kwargs)
        save_cache(cache_name, data)
        return data
    except Exception as e:
        log_error(tag, f"HTTP failed ({url}): {e}; trying stale cache")
        try:
            p = _state_path(cache_name)
            if p.exists():
                blob = json.loads(p.read_text("utf-8"))
                age_h = (time.time() - blob.get("ts", 0)) / 3600
                log(tag, f"using stale cache for {cache_name} ({age_h:.1f}h old)")
                return blob.get("data")
        except Exception:
            pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Smart dedup — numerical bucketing + max-silence backstop
# ─────────────────────────────────────────────────────────────────────────────
def bucket(value, step):
    """Snap value to nearest step. None passes through unchanged."""
    if value is None:
        return None
    try:
        return round(float(value) / step) * step
    except (ValueError, TypeError):
        return None

def signature(*items) -> str:
    """SHA256 of a stable repr — use bucketed values to ignore noise-level changes."""
    return hashlib.sha256(repr(items).encode()).hexdigest()

def should_broadcast(
    state_file: str,
    current_sig: str,
    max_silence_hours: float = 6.0,
) -> bool:
    """
    Decide whether to broadcast based on:
      1. Signature changed from last broadcast → YES
      2. Same signature but last broadcast > max_silence_hours ago → YES
      3. Otherwise → NO

    The max-silence backstop guarantees mesh listeners hear from us
    periodically even when conditions are stable.
    """
    state = load_state(state_file)
    last_sig = state.get("sig")
    last_ts  = state.get("ts", 0)
    age_h    = (time.time() - last_ts) / 3600

    if last_sig != current_sig:
        return True
    if age_h >= max_silence_hours:
        return True
    return False

def mark_broadcast(state_file: str, current_sig: str) -> None:
    """Record that we broadcast. Call AFTER printing the response JSON."""
    save_state(state_file, {"sig": current_sig, "ts": time.time()})

# ─────────────────────────────────────────────────────────────────────────────
# Response output
# ─────────────────────────────────────────────────────────────────────────────
def respond(text: str, error: str | None = None) -> None:
    """Print single-message JSON response for MM to broadcast."""
    payload = {"response": text}
    if error:
        payload["error"] = error
    print(json.dumps(payload, ensure_ascii=False))

def respond_multi(texts: list, error: str | None = None,
                  waypoints: list | None = None) -> None:
    """Print multi-message JSON response for Mesh Commander to broadcast
    sequentially. ``waypoints`` (optional) is a list of map-pin dicts
    ``{lat, lon, name?, description?, icon?, expire?, channel?}`` — Mesh
    Commander broadcasts each via its native send_waypoint(), so the pin
    shows on its own map AND rides the keep-alive rebroadcast scheduler."""
    payload = {"responses": texts}
    if waypoints:
        payload["waypoints"] = waypoints
    if error:
        payload["error"] = error
    print(json.dumps(payload, ensure_ascii=False))

def respond_silent(error: str | None = None) -> None:
    """Empty response — script ran but doesn't want MM to broadcast anything."""
    respond("", error=error)
