"""Regression checks for the deprecated timer entry point; no mesh IO."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import traffic_timer
import traffic_native_data
import traffic_responder

class RetiredTimer(unittest.TestCase):
    def test_direct_execution_is_silent(self):
        result = subprocess.run([sys.executable, str(ROOT / 'traffic_timer.py')], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {'response': ''})
        self.assertIn('DEPRECATED', result.stderr)
        self.assertFalse(hasattr(traffic_timer, 'send_waypoint_via_virtual_node'))

    def test_responder_helpers_use_native_producer(self):
        for name in ('fetch_traffic_events', 'normalize_roadway', 'is_freeway', 'matches_highway', 'format_event_text', 'TAGS', 'AUTO_PUSH_TYPES'):
            self.assertIs(getattr(traffic_timer, name), getattr(traffic_native_data, name))
            self.assertIs(getattr(traffic_responder, name), getattr(traffic_native_data, name))
