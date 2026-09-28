#!/usr/bin/env python3
# mm_meta:
#   mode: responder
#   name: Traffic (On-Demand)
#   emoji: 💥
#   language: Python
"""
On-demand traffic report (metro Phoenix digest / per-road lookup).

Two distinct modes, decided by whether MeshMonitor passes a highway
parameter from the trigger regex:

  !traffic                → "freeway accidents/incidents only" digest,
                             METRO ZONES ONLY (East/Central/West Valley —
                             the ZONES entries flagged metro)

  !traffic 60             → "everything on this road" digest across ALL
  !traffic I-10             zones (all event types — accidents, closures,
  !traffic loop202          hazards, roadwork — on the matched roadway)

Why the asymmetry: the parameterless ask is "give me the local situation
report" — same lens as the timer, freeways and high-impact events only,
since that's what a SAR/comms operator cares about at a glance. It stays
metro-only ON PURPOSE: the corridor zones (I-17 to Flagstaff, I-10 to
Tucson/California, SR-87, US-60, Casa Grande) would balloon the digest,
and a corridor user should ask for THEIR road ("!traffic i17") — which
searches every zone and returns everything on it, freeway or surface,
accident or roadwork.

MM passes regex captures as PARAM_1, PARAM_2, ... env vars. We scan
os.environ for the first non-empty PARAM_* match (regex group order
varies by keyword config and we'd rather be robust than coupled to it).

Top 3 events per response — beyond that, mesh users should pull up
maps.skynet2.net. The format_event_text() helper produces 3 lines per
event (emoji label + road, timestamp, Google Maps link) so the cap
keeps response payload sane.

Shares with traffic_timer.py:
  - fetch_traffic_events()  (ZONES + active-now filter; annotates ev["_zone"])
  - normalize_roadway(), is_freeway(), matches_highway()
  - format_event_text(), TAGS

No state changes here — the trigger never affects the timer's seen-dedup
dict. Asking for traffic doesn't suppress the next auto-broadcast.

Env vars: ADOT_API_KEY required (same as timer). PARAM_* read from MM.

Suggested MeshMonitor keywords:
  !traffic               (no capture group)
  !traffic ([\\w-]+)      (highway as capture group → PARAM_1)
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import respond, respond_multi, log, log_error
from traffic_timer import (
    fetch_traffic_events,
    normalize_roadway,
    is_freeway,
    matches_highway,
    format_event_text,
    TAGS,
    AUTO_PUSH_TYPES,
)

TAG = "traffic_trigger"
MAX_RESULTS = 3

def get_highway_param():
    """
    Return the optional roadway requested with !traffic.

    Older script runners may expose regex captures as PARAM_* variables.
    MeshMonitor 4.16.1 Automation Engine exposes the received message as
    MESSAGE instead, so parse the roadway directly from that text.
    """
    # Backward compatibility with runners that provide regex capture groups.
    for key in sorted(os.environ.keys()):
        if not key.startswith("PARAM_"):
            continue
        val = os.environ[key].strip()
        if val:
            return val

    # MeshMonitor Automation Engine: MESSAGE contains the received text.
    message = os.environ.get("MESSAGE", "").strip()
    match = re.match(r"^!traffic(?:\s+(.+?))?\s*$", message, re.IGNORECASE)
    if match:
        highway = (match.group(1) or "").strip()
        return highway or None

    return None

def main():
    events, err = fetch_traffic_events()
    if err is not None:
        respond("⚠️ ADOT traffic data unavailable")
        log_error(TAG, f"fetch failed: {err}")
        return

    highway = get_highway_param()

    if highway:
        # Specific-road mode: all event types on the matched roadway
        matches = [
            ev for ev in events
            if matches_highway(ev.get("RoadwayName", ""), highway)
            and str(ev.get("EventType", "")).lower() in TAGS
        ]
        log(TAG, f"highway query '{highway}' matched {len(matches)} event(s)")
        if not matches:
            respond(f"✅ No active events for {highway.upper()}")
            return
    else:
        # Default mode: freeway accidents/closures in the METRO zones only.
        # Corridor zones are excluded on purpose — ask per-road for those.
        matches = [
            ev for ev in events
            if str(ev.get("EventType", "")).lower() in AUTO_PUSH_TYPES
            and is_freeway(normalize_roadway(ev.get("RoadwayName", "")))
            and (ev.get("_zone") or {}).get("metro")
        ]
        log(TAG, f"metro freeway digest matched {len(matches)} event(s)")
        if not matches:
            respond("✅ Metro Phoenix freeways clear of incidents")
            return

    # Prioritize the most actionable events before applying MAX_RESULTS.
    # Within each event type, newest ADOT update comes first so a fresh crash
    # cannot be hidden behind months-old roadwork simply because of feed order.
    _priority = {
        "accidentsandincidents": 0,
        "closures": 1,
        "roadwork": 2,
    }
    matches.sort(
        key=lambda ev: (
            _priority.get(str(ev.get("EventType", "")).lower(), 99),
            -int(ev.get("LastUpdated") or 0),
        )
    )

    capped = matches[:MAX_RESULTS]
    if len(matches) > MAX_RESULTS:
        log(TAG, f"capping {len(matches)} matches at {MAX_RESULTS}")

    msgs = [format_event_text(ev) for ev in capped]
    respond_multi(msgs)

if __name__ == "__main__":
    main()
