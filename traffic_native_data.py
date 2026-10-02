#!/usr/bin/env python3
# mm_meta:
#   mode: timer
#   name: Traffic native data helpers (not the timer entry point)
#   emoji: 💥
#   language: Python
"""AZ511 data helpers for the native MeshMonitor Traffic workflow.

No radio transport and no state writes. --fixture is an offline simulation.
--prepare reads an explicitly supplied JSON state snapshot and fetches AZ511.
The installed native entry point is traffic_attempts.py, which implements
bounded attempts without pretending they are delivery receipts. This module's
standalone CLI prepares data from a legacy-style snapshot without saving it.
"""
import os, sys, json, re, time, hashlib, argparse, copy
from pathlib import Path
from datetime import datetime

# Sibling-file import of common (flat /data/scripts/ layout)
sys.path.insert(0, str(Path(__file__).parent))
from common import (
    clamp, http_get_json,
    respond_silent, log, log_error,
)

TAG = "traffic"

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
ADOT_API_KEY = os.getenv("ADOT_API_KEY")  # no fallback — fail loud if missing

# Alert zones — each a bounding box (lat_min, lat_max, lon_min, lon_max) with
# a short tag for the text alert. Replaced the single East Valley box.
#
# Geometry rules (verified seam-to-seam when these were drawn):
#   - Adjacent boxes OVERLAP on purpose — no gap a waypoint could fall through.
#     Overlap is free: dedup is by ADOT event ID (see fingerprint/state), so an
#     event sitting in two boxes still broadcasts exactly once.
#   - ORDER MATTERS FOR LABELING ONLY: first matching zone wins, so the metro
#     boxes come first — a crash on the Beeline inside Mesa tags [EV], not
#     [SR87]. Coverage/dedup are unaffected by order.
#   - "metro": True marks the metro-Phoenix boxes; the responder sibling scopes
#     the bare !traffic digest to these (corridor users ask for their road:
#     "!traffic i17"). See traffic_responder.py.
#   - A zone MAY carry "roads": ["I-10", ...] — then events must also fuzzy-
#     match one of them (matches_highway), for corridor boxes that would
#     otherwise sweep in town surface streets. None assigned today: most
#     city/county agencies don't feed ADOT, so plain boxes stay quiet.
#   - Known intentional holes: SR-85 south of Buckeye, Wickenburg / US-60 NW,
#     Tucson east of -110.955, SR-79 south of ~32.71. (Cave Creek/Carefree
#     fall inside the SR-87 rectangle — covered, tagged [SR87].)
ZONES = [
    # Metro Phoenix (bare-!traffic scope)
    {"name": "East Valley",              "tag": "EV",    "metro": True,
     "box": (33.270107, 33.682147, -111.998348, -111.493658)},
    {"name": "Central Valley",           "tag": "CV",    "metro": True,
     "box": (33.240569, 33.686600, -112.200652, -111.876859)},
    {"name": "West Valley",              "tag": "WV",    "metro": True,
     "box": (33.406478, 33.881875, -112.665736, -112.124652)},
    # North — the I-17 chain to Flagstaff (+ Prescott/Sedona/Camp Verde,
    # Williams/I-40 in the top box)
    {"name": "I-17 Valley-Cordes Jct",   "tag": "I17S",
     "box": (33.651269, 34.358054, -112.240066, -112.003610)},
    {"name": "I-17 Cordes-Rim Country",  "tag": "I17N",
     "box": (34.273238, 34.943821, -112.549486, -111.486109)},
    {"name": "Flagstaff/Williams/I-40",  "tag": "FLG",
     "box": (34.900000, 35.300000, -112.600000, -111.400000)},
    # East
    {"name": "SR-87 Valley-Payson",      "tag": "SR87",   # west edge touches I17S
     "box": (33.430891, 34.291454, -112.003610, -111.261702)},
    {"name": "US-60 Valley-Globe",       "tag": "US60",
     "box": (33.233331, 33.430890, -111.622454, -110.696897)},
    # South — Casa Grande / Tucson
    {"name": "I-10/SR-347 Casa Grande",  "tag": "I10CG",
     "box": (32.781226, 33.313537, -112.121039, -111.596782)},
    {"name": "Pinal state routes",       "tag": "PINAL",  # Florence/Coolidge/San Tan
     "box": (32.715314, 33.398183, -111.884583, -111.273850)},
    {"name": "I-10 Casa Grande-Tucson",  "tag": "TUS",
     "box": (32.179406, 33.000000, -111.767030, -110.954972)},

    # Far South — I-19 / Santa Cruz County
    {"name": "I-19 Tucson-Nogales",      "tag": "I19",
     "box": (31.3300, 32.2200, -111.1200, -110.9000),
     "roads": ["I-19"]},

    # Santa Cruz County — catches SR-82, SR-83, SR-189 and other
    # AZ511-reported incidents in the county without relying on roadway naming.
    {"name": "Santa Cruz County",        "tag": "SCZ",
     "box": (31.3300, 31.8000, -111.0800, -110.4300)},

    # West
    {"name": "I-10 Valley-California",   "tag": "I10W",
     "box": (33.388030, 33.721330, -114.561269, -112.362901)},
]

# Only auto-push these high-impact event types
AUTO_PUSH_TYPES = {"accidentsandincidents", "closures"}

# Emoji + short label for each broad ADOT EventType. Used as the LAST-resort
# fallback — EventType is only ever roadwork / closures / accidentsAndIncidents,
# so it's too coarse to label an incident accurately on its own (debris, a
# stall, and a rollover all share "accidentsAndIncidents"). See classify_event.
TAGS = {
    "accidentsandincidents": ("💥", "MVA"),
    "closures":              ("⛔", "CLOSED"),
    "hazard":                ("⚠️",  "HAZARD"),
    "roadwork":              ("🚧", "ROADWORK"),
}

# Incident classifier rules — ordered, first match wins. Scanned against the
# ADOT EventSubType first (the dispatcher's specific call) and then the
# free-text Description, so the header reflects the ACTUAL incident rather than
# the broad EventType bucket. Keep labels short — the waypoint name is capped
# at 30 chars and the road name needs room.
_INCIDENT_RULES = [
    (("rightlane", "right lane"),                              ("🚧", "RIGHT LANE")),
    (("leftlane", "left lane"),                                ("🚧", "LEFT LANE")),
    (("narrowlanes", "narrow lanes"),                          ("🚧", "NARROW LANES")),
    (("debris",),                                              ("🪨", "DEBRIS")),
    (("pothole",),                                             ("🕳️", "POTHOLE")),
    (("dust", "haboob", "blowing dust"),                       ("🌫️", "DUST")),
    (("flood", "high water", "water over"),                    ("🌊", "FLOOD")),
    (("fire",),                                                ("🔥", "FIRE")),
    (("rollover", "roll over"),                                ("💥", "ROLLOVER")),
    (("wrong way", "wrong-way"),                               ("⛔", "WRONG WAY")),
    (("pedestrian",),                                          ("🚶", "PED")),
    (("animal", "livestock", "cattle", "loose horse"),         ("🐄", "ANIMAL")),
    (("disabled", "stalled", "abandoned", "broken down",
      "breakdown"),                                            ("🚗", "DISABLED")),
    (("police", "law enforce", "investigat"),                  ("🚓", "POLICE")),
    (("spill", "hazmat", "hazardous material"),                ("☣️", "HAZMAT")),
    (("crash", "collision", "accident", "mva", "wreck"),       ("💥", "MVA")),
    (("construction", "roadwork", "road work", "maintenance",
      "paving"),                                               ("🚧", "ROADWORK")),
]

STATE_FILE = "traffic_seen.json"

# Channel the map pin broadcasts on (index). Unset (None) = follow the timer's
# own configured channel, so pins and text alerts ride the SAME channel (set
# that to "Traffic" in Automations). Set the env var only to force a different
# channel for the pins (e.g. 0 = PRIMARY).
_wc = os.getenv("WAYPOINT_CHANNEL", "").strip()
WAYPOINT_CHANNEL     = int(_wc) if _wc else None

MAX_MSGS             = int(os.getenv("MAX_MSGS", "3"))
STATE_TTL_HOURS      = float(os.getenv("STATE_TTL_HOURS", "6"))
# Push-path recency bound. The timer only broadcasts events that STARTED within
# this window — a long-running closure that began days/months ago is real but
# not "new", and on a cold start (empty seen-state) it would otherwise blast as
# if it just happened. The on-demand responder is unaffected (it shares
# fetch_traffic_events, which does NOT apply this) so !traffic still shows every
# currently-active incident.
TRAFFIC_MAX_AGE_HOURS = float(os.getenv("TRAFFIC_MAX_AGE_HOURS", "24"))

# Pin lifetime. The ADOT feed's PlannedEndDate is reliable (a live probe found
# ~77% usable future dates, sentinels rare), and it IS the incident's real end —
# so we trust it up to a 7-DAY cap. That lets a multi-day closure live its full
# duration from a single broadcast (no re-TX/keep-alive), expiring on every node's
# map at the real end. Far-future sentinels (year ~3099, which overflow the
# uint32 Waypoint.expire field) clamp to the cap. When PlannedEndDate is missing
# or already past, fall back by event type — accidents are short-lived/volatile,
# closures run hours.
WAYPOINT_TTL_MAX      = int(os.getenv("WAYPOINT_TTL_MAX", str(7 * 86400)))  # 7 days
# Per-type ceiling on the pin lifetime, applied EVEN when PlannedEndDate is longer.
# Accidents are volatile and clear in hours — ADOT routinely stamps them with an
# optimistic multi-hour (even multi-day) PlannedEnd, which would otherwise pin a
# long-cleared crash. Cap them at 2h (matches the fallback) so an MVA never lingers
# on the map; anything not listed uses the 7-day max (closures can legitimately
# run for days).
WAYPOINT_TTL_TYPE_MAX = {"accidentsandincidents": 7200}                     # 2h
WAYPOINT_TTL_FALLBACK = {"accidentsandincidents": 7200, "closures": 43200}  # 2h / 12h
WAYPOINT_TTL_DEFAULT  = 7200   # generic fallback (2h) when type is unknown
UINT32_MAX            = 4294967295

# ADOT DirectionOfTravel -> compass abbreviation for the alert headline.
_DIR_ABBR = {
    "eastbound": "EB", "westbound": "WB", "northbound": "NB", "southbound": "SB",
    "east": "EB", "west": "WB", "north": "NB", "south": "SB",
    "both": "BOTH", "all": "ALL",
}

# ADOT dispatch noise tokens baked into RoadwayName — strip these out
_ADOT_NOISE_RE = re.compile(
    r',\s*(?:SCT|TMP|CHA|MES|GIL|CHN|QCK|AHW|PHX|TPK|SRS)\b'
    r'|ACCIDENT[/\s]*(?:INCIDENTS?|MVA)?'
    r'|ACCIDENTINCIDENTS?'
    r'|\bMVA\b',
    re.IGNORECASE
)

# Freeway prefixes after normalize_roadway — I-XX, L-XX (loops), US-XX
_FREEWAY_RE = re.compile(r'^(I-\d+|L-\d+|US-\d+)\b')


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — pure, importable by trigger
# ─────────────────────────────────────────────────────────────────────────────
# Tucson-area agencies (Tucson PD, Pima County SO, Pima CC DPS, …) export their
# CAD dispatch record into the ADOT feed verbatim: KEY: VALUE lines. The only
# field with traveler value is the location; AGENCY/BEAT/STATUS/OPEN TIME are
# dispatch-internal. Real example that went over the air:
#   MVA CROSS STREETS: N GREASEWOOD RD AND W DRACHMAN ST
#   OPEN TIME: 16:10:49 07/21/2026
#   UNIT ON SCENE: 17:32:34 07/21/2026
# — verbose enough to truncate the map link, which is the line that matters.
_DISPATCH_KEY_RE = re.compile(
    r"^\s*(CROSS STREETS|LOCATION|AGENCY|BEAT|STATUS|OPEN TIME|CLOSE TIME|"
    r"UNIT ON SCENE|CASE|INCIDENT|DISPOSITION)\s*:\s*(.*?)\s*$",
    re.I | re.M)
_DISPATCH_LOC_KEYS = ("CROSS STREETS", "LOCATION")


def sanitize_dispatch(text):
    """Reduce an agency CAD dispatch blob to its location, uniformly formatted
    ('A / B', sub-detail after '·'). Text without a KEY: VALUE dispatch block
    passes through untouched — ADOT's own descriptions never match."""
    if not text or ":" not in text:
        return text
    matches = _DISPATCH_KEY_RE.findall(text)
    if not matches:
        return text
    loc = next((v for k, v in matches
                if k.upper() in _DISPATCH_LOC_KEYS and v.strip()), "")
    if not loc:
        # Dispatch block without a location key — drop the block lines, keep
        # whatever prose remains (or the original if that leaves nothing).
        rest = _DISPATCH_KEY_RE.sub("", text).strip()
        return rest or text
    loc = re.sub(r"\s+AND\s+", " / ", loc, flags=re.I)
    loc = re.sub(r"\s*&\s*", " / ", loc)
    loc = re.sub(r"\s*;\s*", " · ", loc)
    return loc.strip()


def normalize_roadway(text):
    """Normalize ADOT RoadwayName: fix highway abbreviations, strip dispatch noise."""
    if not text:
        return "Road"
    text = sanitize_dispatch(str(text).strip())
    text = _ADOT_NOISE_RE.sub("", text).strip().strip(",").strip()
    text = text.upper()
    text = re.sub(r'\bI[- ]?(\d+)\b',          r'I-\1',  text)
    text = re.sub(r'\bL(?:OOP)?[- ]?(\d+)\b',  r'L-\1',  text)
    text = re.sub(r'\bUS[- ]?(\d+)\b',         r'US-\1', text)
    text = re.sub(r'\b(?:SR|STATE ROUTE|AZ)[- ]?(\d+)\b', r'SR-\1', text)
    text = re.sub(r'  +', ' ', text).strip()
    return text

def is_freeway(normalized_roadway):
    """True if the normalized roadway is a freeway (I-XX, L-XX, US-XX)."""
    return bool(_FREEWAY_RE.match(normalized_roadway or ""))

def matches_highway(roadway, query):
    """
    Fuzzy match: True if the normalized roadway contains the query's
    alphanumerics. Used by the trigger so users can type "!traffic 60"
    or "!traffic I-10" or "!traffic loop202" and get the right results.
    """
    if not query:
        return False
    norm = normalize_roadway(roadway)
    raw_query = str(query).strip().upper()

    # A bare route number should match a highway route, not an ordinary
    # street containing the same number (e.g. "60" must not match "60TH ST").
    if raw_query.isdigit():
        return bool(re.match(
            rf"^(?:I|US|SR|L)-?{re.escape(raw_query)}(?:\D|$)",
            norm,
        ))

    q = re.sub(r"[^A-Z0-9]", "", raw_query)
    n = re.sub(r"[^A-Z0-9]", "", norm)
    return bool(q) and q in n

def match_zone(ev):
    """First ZONES entry containing the event, or None (outside all zones).
    A zone with a "roads" list additionally requires the event's RoadwayName
    to fuzzy-match one of them. First match wins — that only decides the zone
    TAG; dedup by event ID means overlapping zones never double-broadcast."""
    lat = ev.get("Latitude")
    lon = ev.get("Longitude")
    if not (lat and lon):
        return None
    for zone in ZONES:
        lat_min, lat_max, lon_min, lon_max = zone["box"]
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            continue
        roads = zone.get("roads")
        if roads and not any(matches_highway(ev.get("RoadwayName", ""), r) for r in roads):
            continue
        return zone
    return None

def fingerprint(ev):
    """Dedup fingerprint: event ID + SANITIZED description.
    Catches genuine re-issues (description changed = ADOT thinks it's news)
    while collapsing identical re-polls. Sanitized because the Tucson agencies'
    CAD STATUS churn (RCVD → DEPUTY EN → DEPUTY ON → DOING PAPERWORK) edits the
    description at every dispatch step — the same crash re-broadcast up to
    4x/day as 'news'. Location/type changes still re-fire."""
    eid  = str(ev.get("ID") or ev.get("Id") or "0")
    desc = sanitize_dispatch(str(ev.get("Description") or ""))
    return hashlib.md5(f"{eid}{desc}".encode()).hexdigest()

def stable_wp_id(eid):
    """Derive a stable uint32 waypoint ID from the ADOT event ID. Re-broadcasts
    of the same event UPDATE the existing pin instead of creating duplicates."""
    return int(hashlib.sha1(str(eid).encode()).hexdigest()[:8], 16)

def fmt_unix_ts(raw):
    try:
        return datetime.fromtimestamp(int(raw)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "??-??-??"

def is_active_now(ev, now=None):
    """True only if the event has already started — filters scheduled future maintenance."""
    start = ev.get("StartDate")
    if start is None:
        return True
    try:
        return int(start) <= (time.time() if now is None else now)
    except Exception:
        return True

def started_recently(ev, now):
    """True if the event's StartDate is within TRAFFIC_MAX_AGE_HOURS of ``now``.
    Push-path gate so the timer doesn't re-broadcast long-running closures that
    began long ago. Missing/unparseable StartDate → True (fail-safe to show)."""
    start = ev.get("StartDate")
    if start is None:
        return True
    try:
        return int(start) >= now - TRAFFIC_MAX_AGE_HOURS * 3600
    except Exception:
        return True

def classify_event(ev):
    """
    Pick (emoji, short_label) from the ACTUAL incident detail, not just ADOT's
    broad EventType bucket (which is only roadwork/closures/accidentsAndIncidents
    and would stamp debris, stalls, and rollovers all as "MVA").

    Priority:
      1. Keyword match on EventSubType — the dispatcher's specific classification.
      2. Keyword match on the free-text Description.
      3. EventSubType verbatim (uppercased, trimmed) — accurate even when we
         have no keyword rule for it, which beats a wrong generic label.
      4. EventType TAGS fallback (last resort).

    Shared by the text message and the waypoint so the header and the pin agree.
    """
    subtype = str(ev.get("EventSubType") or "").strip()
    desc    = str(ev.get("Description") or "")

    # 1. Trust the structured subtype first.
    sub_l = subtype.lower()
    for keywords, tag in _INCIDENT_RULES:
        if any(k in sub_l for k in keywords):
            return tag

    # 2. Then scan the description prose.
    desc_l = desc.lower()
    for keywords, tag in _INCIDENT_RULES:
        if any(k in desc_l for k in keywords):
            return tag

    # 3/4. No keyword hit — prefer ADOT's own subtype text over the broad bucket.
    e_type = str(ev.get("EventType", "")).lower()
    base_emoji, base_label = TAGS.get(e_type, ("ℹ️", "INFO"))
    if subtype:
        return base_emoji, subtype.upper()[:14]
    return base_emoji, base_label


def format_event_text(ev):
    """Build the lean 3-line mesh text message for one ADOT event. The zone
    tag pins the area — road + direction alone is ambiguous now that three
    separate zones carry I-10. The waypoint stays untagged (a pin locates
    itself)."""
    head = event_headline(ev)
    zone = ev.get("_zone")
    if zone:
        head += f" [{zone['tag']}]"
    ts      = fmt_unix_ts(ev.get("LastUpdated"))
    lat     = ev.get("Latitude",  0.0)
    lon     = ev.get("Longitude", 0.0)
    # %2C (encoded comma), not a literal ',': the Meshtastic Android app's URL
    # linkifier regex has no comma in its char class and truncates the link at
    # the first ',' (dropping the longitude). %2C survives + Maps decodes it.
    map_url = f"https://www.google.com/maps?q={lat:.4f}%2C{lon:.4f}"
    return clamp(f"{head}\n⌚ {ts}\n📍 {map_url}")


def direction_abbr(ev):
    """ADOT DirectionOfTravel -> EB/WB/NB/SB (empty if unknown/none)."""
    return _DIR_ABBR.get(str(ev.get("DirectionOfTravel") or "").strip().lower(), "")


def headline_parts(ev):
    """(emoji, label, road, direction) with the full-closure override applied.
    A full closure is the most actionable fact for a traveler, so it overrides
    the incident-type label (a crash that fully closes a freeway reads
    'FULL CLOSURE', not 'MVA')."""
    emoji, label = classify_event(ev)
    if ev.get("IsFullClosure"):
        emoji, label = "⛔", "FULL CLOSURE"
    road = normalize_roadway(ev.get("RoadwayName", ""))
    direction = direction_abbr(ev)
    return emoji, label, road, direction


def event_headline(ev):
    """One-line incident headline for TEXT alerts: emoji + label + road +
    direction. The waypoint carries the emoji in Waypoint.icon instead."""
    emoji, label, road, direction = headline_parts(ev)
    head = f"{emoji} {label} {road}"
    if direction:
        head += f" {direction}"
    return head


# ─────────────────────────────────────────────────────────────────────────────
# Shared fetch — used by both timer (this file) and trigger sibling
# ─────────────────────────────────────────────────────────────────────────────
def fetch_traffic_events():
    """
    Fetch the full ADOT event list and apply the two filters both callers want:
      - Drop events outside every alert zone (ZONES; see match_zone)
      - Drop events with a future StartDate (not yet active)

    Each surviving event is annotated with its zone under ``ev["_zone"]`` —
    format_event_text reads it for the [TAG] and the responder sibling scopes
    the bare !traffic digest to zones flagged metro.

    Returns:
        (events, None) on success — events is the filtered list. Caller is
            responsible for any further filtering (event type, dedup, etc.).
        ([], None) on success with zero matches.
        (None, error_str) on fetch failure or bad payload.

    Requires ADOT_API_KEY in environment.
    """
    if not ADOT_API_KEY:
        log_error(TAG, "ADOT_API_KEY not set in environment")
        return None, "ADOT_API_KEY missing"

    try:
        url = f"https://az511.com/api/v2/get/event?key={ADOT_API_KEY}&format=json"
        # Bounded retry budget: a slow ADOT must not eat the script timeout.
        events = http_get_json(url, timeout=8, retries=2)
    except Exception as e:
        log_error(TAG, f"ADOT poll failed: {e}")
        return None, f"ADOT fetch failed: {e}"

    if not isinstance(events, list):
        log_error(TAG, f"ADOT returned unexpected payload type: {type(events).__name__}")
        return None, "ADOT bad payload"

    filtered = []
    for ev in events:
        # Geographic filter — must land in one of the alert zones
        zone = match_zone(ev)
        if zone is None:
            continue

        # Skip future-scheduled events
        if not is_active_now(ev):
            continue

        ev["_zone"] = zone
        filtered.append(ev)

    return filtered, None


def build_waypoint_dict(ev, now=None):
    """Preserve legacy waypoint data; native action mapping is separate."""
    eid  = ev.get("ID") or ev.get("Id") or "0"
    lat  = ev.get("Latitude",  0.0)
    lon  = ev.get("Longitude", 0.0)

    # The classified emoji rides Waypoint.icon (a unicode codepoint — Meshtastic
    # apps render it as the map pin), NOT the name; emoji-in-the-name showed as
    # a default pin with the emoji in the title on receiving apps.
    emoji, label, road, direction = headline_parts(ev)
    wp_name = f"{label} {road}" + (f" {direction}" if direction else "")
    wp_name = wp_name[:30]
    adot_desc = str(ev.get("Description") or "").strip()
    wp_desc   = adot_desc[:100] if adot_desc else event_headline(ev)

    # Pin lifetime: trust ADOT's PlannedEndDate (the incident's real end) when it's
    # a future date, but clamp to a per-type ceiling so a far-future PlannedEnd or a
    # year-3099 sentinel can't pin a short-lived accident for days. Missing/past
    # PlannedEnd falls back by event type. One broadcast keeps the pin alive on
    # every map until expiry — no re-TX. Final min() guards the uint32 field.
    e_type  = str(ev.get("EventType", "")).lower()
    type_max = WAYPOINT_TTL_TYPE_MAX.get(e_type, WAYPOINT_TTL_MAX)
    planned = ev.get("PlannedEndDate")
    now     = int(time.time() if now is None else now)
    try:
        planned_i = int(planned) if planned is not None else 0
    except (TypeError, ValueError):
        planned_i = 0
    if now < planned_i:
        expire = min(planned_i, now + type_max)
    else:
        expire = now + WAYPOINT_TTL_FALLBACK.get(e_type, WAYPOINT_TTL_DEFAULT)
    expire = min(expire, UINT32_MAX)

    return {
        "waypoint_id": stable_wp_id(eid),
        "name":        wp_name,
        "description": wp_desc,
        "icon":        ord(emoji[0]) if emoji else 0,
        "lat":         float(lat),
        "lon":         float(lon),
        "expire":      int(expire),
        "channel":     WAYPOINT_CHANNEL,
    }


# ─────────────────────────────────────────────────────────────────────────────
# State management — TTL'd dict of {event_id: {first_seen, last_seen, fp}}
# ─────────────────────────────────────────────────────────────────────────────
def prune_state(state, now=None):
    """Drop entries whose last_seen is older than STATE_TTL_HOURS."""
    cutoff = (time.time() if now is None else now) - (STATE_TTL_HOURS * 3600)
    return {
        eid: entry for eid, entry in state.items()
        if isinstance(entry, dict) and entry.get("last_seen", 0) >= cutoff
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def prepare_cycle(events, state, now):
    """Select up to three incidents without mutating state or transmitting.

    Input events must already have geographic/future-start filtering applied.
    maintenance_state refreshes unchanged entries; selected fingerprints are
    NOT committed until an external, verified delivery receipt exists.
    """
    state = prune_state(copy.deepcopy(state if isinstance(state, dict) else {}), now)
    selected = []
    selected_ids = set()
    for ev in events:
        e_type = str(ev.get("EventType", "")).lower()
        if e_type not in AUTO_PUSH_TYPES or e_type not in TAGS:
            continue
        if (ev.get("_zone") or {}).get("tag") not in {"I19", "SCZ"}:
            continue
        if not started_recently(ev, now):
            continue
        eid = str(ev.get("ID") or ev.get("Id") or "0")
        fp = fingerprint(ev)
        prior = state.get(eid)
        if isinstance(prior, dict) and prior.get("fp") == fp:
            prior["last_seen"] = now
            continue
        if eid in selected_ids or len(selected) >= max(0, min(MAX_MSGS, 3)):
            continue
        wp = build_waypoint_dict(ev, now)
        selected_ids.add(eid)
        selected.append({
            "event_id": eid, "fingerprint": fp,
            "text": format_event_text(ev), "waypoint": wp,
            # Full SHA1 avoids truncating long external IDs into colliding keys.
            "waypoint_key": "az511-" + hashlib.sha1(eid.encode()).hexdigest(),
            "latitude": wp["lat"], "longitude": wp["lon"],
            "name": wp["name"], "description": wp["description"],
            "icon": headline_parts(ev)[0],
            "expire_hours": max(0, wp["expire"] - int(now)) / 3600,
        })
    slots = {f"incident{i}": {"present": False} for i in range(3)}
    for i, incident in enumerate(selected):
        slots[f"incident{i}"] = {"present": True, **incident}
    return {"response": "", "count": len(selected), "generated_at": int(now),
            "incidents": selected, **slots, "maintenance_state": state}


def acknowledge_delivery(state, incident, now, *, waypoint_sent, text_sent):
    """Pure receipt reducer for a future delivery adapter; performs no IO.

    Both results must be literal True (not a skip, queue acceptance, or a dry
    run). A missing receipt leaves the event retryable. This is deliberately
    not called by the draft automation: rc1 exposes no receipt to runScript.
    """
    result = copy.deepcopy(state)
    if waypoint_sent is not True or text_sent is not True:
        return result
    eid = incident["event_id"]
    prior = result.get(eid)
    result[eid] = {
        "first_seen": prior.get("first_seen", now) if isinstance(prior, dict) else now,
        "last_seen": now, "fp": incident["fingerprint"],
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", type=Path, help="offline raw AZ511 JSON")
    mode.add_argument("--prepare", action="store_true", help="fetch AZ511; never send or save")
    parser.add_argument("--state-file", type=Path, required=True, help="explicit JSON state snapshot")
    parser.add_argument("--now", type=int, help="frozen epoch; fixture mode only")
    args = parser.parse_args()
    if args.now is not None and not args.fixture:
        parser.error("--now requires --fixture")
    now = args.now if args.now is not None else time.time()
    try:
        # Direct read avoids common.load_state's implicit SQLite creation/migration.
        state = json.loads(args.state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state snapshot must be a JSON object")
        if args.fixture:
            events = json.loads(args.fixture.read_text(encoding="utf-8"))
            if not isinstance(events, list):
                raise ValueError("fixture must be an AZ511 event list")
            events = [dict(ev, _zone=match_zone(ev)) for ev in events
                      if match_zone(ev) is not None and is_active_now(ev, now)]
        else:
            events, err = fetch_traffic_events()
            if err:
                raise ValueError(err)
        payload = prepare_cycle(events, state, now)
        payload["simulation"] = bool(args.fixture)
        print(json.dumps(payload, ensure_ascii=True))
    except Exception as exc:
        # Nonzero exit plus empty slots avoids old output reuse when guarded.
        print(json.dumps({"response": "", "count": 0, "error": str(exc),
                          **{f"incident{i}": {"present": False} for i in range(3)}}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
