#!/usr/bin/env python3
"""Deprecated Virtual Node timer; compatibility imports only.

Scheduled traffic is handled by traffic_attempts.py and the Automation Engine's
native broadcastWaypoint/sendMessage actions through Nogales Spicy.
Executing this old entry point intentionally returns no mesh output.
"""
import json
import sys
from traffic_native_data import (
    fetch_traffic_events, normalize_roadway, is_freeway, matches_highway,
    format_event_text, TAGS, AUTO_PUSH_TYPES,
)

if __name__ == '__main__':
    print('DEPRECATED: use the Traffic Alerts Automation Engine workflow.', file=sys.stderr)
    print(json.dumps({'response': ''}))
