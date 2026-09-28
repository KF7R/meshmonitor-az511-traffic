#!/usr/bin/env python3
# mm_meta:
#   mode: timer
#   name: Traffic Alerts (Scheduled)
#   emoji: 💥
#   language: Python
"""
traffic_timer.py — gen2

Polls ADOT AZ511 API for active traffic events within the configured alert
ZONES — the three metro-Phoenix valley boxes plus the highway corridors
radiating out of them (I-17 to Flagstaff, I-10 to Tucson and to California,
SR-87 to Payson, US-60 to Globe, the Casa Grande routes) — and broadcasts
NEW high-impact events (accidents + closures) to the mesh — as BOTH a text
alert AND a map pin. Text alerts carry a short zone tag ([EV], [TUS], …)
since road + direction alone is ambiguous across three separate I-10 zones.

────────────────────────────────────────────────────────────────────────────
HOW TX WORKS (Mesh Commander native — no self-opened TCP link)
────────────────────────────────────────────────────────────────────────────

This script does NOT open its own TCPInterface. It simply prints a JSON
result and lets Mesh Commander do all the transmitting:

    {"responses": [text, …], "waypoints": [{lat, lon, name, …}, …]}

  • responses  → broadcast as text on the timer's configured channel
                 (set this to your "Traffic" channel in Automations).
  • waypoints  → each broadcast via MC's native send_waypoint(), which
                 transmits the pin on the mesh AND logs it locally so it
                 shows on MC's own map and rides the keep-alive rebroadcast
                 scheduler. Each pin carries a STABLE id (sha1 of the ADOT
                 event id) so a re-issued event updates its existing pin
                 instead of cluttering the map with duplicates.

The old gen1/MeshMonitor design opened a second TCPInterface to a virtual
node and hand-sequenced WAYPOINT→text with airtime delays inside a 30s
budget. On Mesh Commander that virtual host isn't there, so it failed and
fell back to text-only (no pins). The directive removes all of that: one
authoritative broadcast path, pins land on our map, and there is no 30s
self-managed TX window to juggle.

Trigger sibling: see traffic_responder.py. Shares fetch_traffic_events(),
normalize_roadway(), format_event_text(), is_freeway(), matches_highway().

────────────────────────────────────────────────────────────────────────────
Env vars (all optional except the key, sensible defaults)
────────────────────────────────────────────────────────────────────────────
  ADOT_API_KEY          REQUIRED — loaded from data/secrets.env
  WAYPOINT_CHANNEL      unset (channel index the map pin broadcasts on;
                             unset = the pins ride the timer's own configured
                             channel, same as the text alerts — set an index
                             only to force a different channel, e.g. 0 so
                             every PRIMARY-channel node renders the pins)
  MAX_MSGS              3    (per cycle cap on new events broadcast)
  STATE_TTL_HOURS       6    (drop state entries unseen for this long)
  TRAFFIC_MAX_AGE_HOURS 24   (push only events that STARTED within this window)
"""
import os, sys, json, re, time, hashlib
from pathlib import Path
from datetime import datetime

# Sibling-file import of common (flat /data/scripts/ layout)
sys.path.insert(0, str(Path(__file__).parent))
from common import (
    clamp, http_get_json, load_state, save_state,
    respond_silent, respond_multi, log, log_error,
)

TAG = "traffic"

# ─────────────────────────────────────────────────────────────────────────────
# Waypoint TX — MeshMonitor Virtual Node
# ─────────────────────────────────────────────────────────────────────────────
# Uses the same proven WAYPOINT_APP transport as the Nogales border waypoint
# scripts.  The timer calls this ONLY for events already selected as new/changed.
sys.path.insert(0, "/data/python-packages")

VN_HOST = "localhost"
VN_PORT = int(os.environ.get("MESHMONITOR_VN_PORT", "4405"))
HOP_LIMIT = int(os.environ.get("MESHMONITOR_WAYPOINT_HOP_LIMIT", "3"))
TRAFFIC_CHANNEL = int(os.environ.get("WAYPOINT_CHANNEL", "3") or "3")


def send_waypoint_via_virtual_node(wp):
    """Transmit one Meshtastic WAYPOINT_APP packet through MeshMonitor's
    Virtual Node. Raises on failure so the caller can leave the event unseen
    and retry it on the next timer cycle."""
    import meshtastic.tcp_interface
    from meshtastic.protobuf.mesh_pb2 import Waypoint
    from meshtastic.protobuf import portnums_pb2

    interface = meshtastic.tcp_interface.TCPInterface(
        hostname=VN_HOST,
        portNumber=VN_PORT,
        timeout=30,
    )

    try:
        packet = Waypoint()
        packet.id = int(wp["waypoint_id"])
        packet.name = str(wp["name"])[:30]
        packet.description = str(wp["description"])[:100]
        packet.latitude_i = int(float(wp["lat"]) * 1e7)
        packet.longitude_i = int(float(wp["lon"]) * 1e7)
        packet.expire = int(wp["expire"])
        packet.icon = int(wp["icon"])

        interface.sendData(
            packet,
            portNum=portnums_pb2.PortNum.WAYPOINT_APP,
            wantAck=True,
            channelIndex=int(wp.get("channel") if wp.get("channel") is not None
                             else TRAFFIC_CHANNEL),
            hopLimit=HOP_LIMIT,
        )

        # Proven border-waypoint scripts allow the VN time to complete TX.
        time.sleep(5.0)

    finally:
        interface.close()


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

def is_active_now(ev):
    """True only if the event has already started — filters scheduled future maintenance."""
    start = ev.get("StartDate")
    if start is None:
        return True
    try:
        return int(start) <= time.time()
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


def build_waypoint_dict(ev):
    """Build a {"waypoints":[...]} directive entry for one event — Mesh Commander
    broadcasts it via send_waypoint() (mesh + our own map). Mirrors the old proto
    builder: stable id (re-issue updates the pin), classified name, ADOT-derived
    expire clamped into the uint32 Waypoint.expire field."""
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
    now     = int(time.time())
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
def prune_state(state):
    """Drop entries whose last_seen is older than STATE_TTL_HOURS."""
    cutoff = time.time() - (STATE_TTL_HOURS * 3600)
    return {
        eid: entry for eid, entry in state.items()
        if isinstance(entry, dict) and entry.get("last_seen", 0) >= cutoff
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    events, err = fetch_traffic_events()
    if err is not None:
        respond_silent(error=err)
        return

    state = load_state(STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    state = prune_state(state)

    now        = time.time()
    candidates = []   # new/changed events, NOT yet marked seen

    for ev in events:
        # Type filter — timer only auto-pushes high-impact types
        e_type = str(ev.get("EventType", "")).lower()
        if e_type not in TAGS or e_type not in AUTO_PUSH_TYPES:
            continue

        # Automatic scheduled broadcasts are local to the Nogales deployment.
        # Keep all configured ZONES available for queries/labeling, but only
        # auto-push I-19 Tucson-Nogales and Santa Cruz County events.
        zone = ev.get("_zone") or {}
        if zone.get("tag") not in {"I19", "SCZ"}:
            continue

        # Recency filter (push path only) — skip events that started longer ago
        # than the window so old/long-running closures don't broadcast as new.
        if not started_recently(ev, now):
            continue

        eid = str(ev.get("ID") or ev.get("Id") or "0")
        fp  = fingerprint(ev)

        prior = state.get(eid)
        if isinstance(prior, dict) and prior.get("fp") == fp:
            # Seen and unchanged — just refresh last_seen
            prior["last_seen"] = now
            state[eid] = prior
            continue

        # New event OR fingerprint changed — defer the "seen" mark until AFTER
        # the MAX_MSGS cap, so capped-overflow events aren't silently swallowed.
        candidates.append((eid, fp, ev))

    if not candidates:
        save_state(STATE_FILE, state)
        respond_silent()
        return

    # Cap to prevent mesh flooding. Overflow events are intentionally left
    # UNMARKED so the next cycle re-offers them instead of dropping them.
    if len(candidates) > MAX_MSGS:
        log(TAG, f"capping {len(candidates)} new events at {MAX_MSGS} (rest deferred to next cycle)")
        candidates = candidates[:MAX_MSGS]

    # Send each selected NEW/CHANGED event's waypoint first.  Only events whose
    # waypoint transmission succeeds are marked seen.  A failed waypoint stays
    # eligible for retry on the next timer cycle.
    successful = []

    for eid, fp, ev in candidates:
        wp = build_waypoint_dict(ev)

        try:
            log(TAG, f"sending waypoint for AZ511 event {eid} via Virtual Node")
            send_waypoint_via_virtual_node(wp)
        except Exception as e:
            log_error(TAG, f"waypoint TX failed for AZ511 event {eid}: {e}")
            continue

        prior = state.get(eid)
        state[eid] = {
            "first_seen": prior.get("first_seen", now) if isinstance(prior, dict) else now,
            "last_seen":  now,
            "fp":         fp,
        }
        successful.append(ev)

    save_state(STATE_FILE, state)

    if not successful:
        respond_silent(error="Waypoint transmission failed; event(s) left eligible for retry")
        return

    log(TAG, f"broadcasting text for {len(successful)} event(s); waypoint TX complete")

    # MeshMonitor Automation handles the returned text responses. Waypoints have
    # already been transmitted above through the Virtual Node.
    texts = [format_event_text(ev) for ev in successful]
    respond_multi(texts)


if __name__ == "__main__":
    main()
