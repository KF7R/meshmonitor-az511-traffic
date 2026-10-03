"""Feature audit: synthetic data, mocked HTTP, no RF or production state."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import traffic_native_data as data
import traffic_attempts as attempts
import traffic_responder as responder
import common

NOW = 1790848800

def event(eid="audit-1", **changes):
    value = {"ID": eid, "RoadwayName": "I-19", "Latitude": 31.5,
             "Longitude": -110.96, "EventType": "accidentsAndIncidents",
             "Description": "Crash", "StartDate": NOW - 60,
             "LastUpdated": NOW, "DirectionOfTravel": "northbound"}
    value.update(changes)
    value["_zone"] = data.match_zone(value)
    return value

class TrafficFeatures(unittest.TestCase):
    def test_zone_coverage_and_known_overlap(self):
        self.assertEqual(event()["_zone"]["tag"], "I19")
        self.assertEqual(event(RoadwayName="SR-82", Longitude=-110.7)["_zone"]["tag"], "SCZ")
        self.assertIsNone(event(Latitude=40)["_zone"])
        # Earlier Tucson rectangle wins: document this actual coverage hole.
        northern = event(Latitude=32.2, Longitude=-110.99)
        self.assertEqual(northern["_zone"]["tag"], "TUS")
        self.assertEqual(attempts.plan([northern], {}, NOW)[0]["count"], 0)

    def test_all_zone_tags(self):
        self.assertEqual({z["tag"] for z in data.ZONES},
                         {"EV", "CV", "WV", "I17S", "I17N", "FLG", "SR87",
                          "US60", "I10CG", "PINAL", "TUS", "I19", "SCZ", "I10W"})

    def test_type_geography_future_and_age_filters(self):
        selected = [event(), event("scz", RoadwayName="SR-82", Longitude=-110.7),
                    event("work", EventType="roadwork"),
                    event("old", StartDate=NOW - 90000),
                    event("metro", Latitude=33.4, Longitude=-111.9)]
        self.assertEqual(attempts.plan(selected, {}, NOW)[0]["count"], 2)
        self.assertFalse(data.is_active_now(event(StartDate=NOW + 1), NOW))
        self.assertTrue(data.started_recently(event(StartDate="unknown"), NOW))

    def test_road_normalization_and_matching(self):
        for query in ("19", "i19", "I-19"):
            self.assertTrue(data.matches_highway("I 19", query))
        self.assertFalse(data.matches_highway("Loop 202", "loop202"))
        self.assertTrue(data.matches_highway("Loop 202", "L202"))
        self.assertFalse(data.matches_highway("60TH ST", "60"))
        self.assertEqual(data.normalize_roadway("AZ 82"), "SR-82")
        # Named lookup is substring-based, not an exact route parser.
        self.assertTrue(data.matches_highway("I-190", "i19"))

    def test_classifier_all_keyword_rules(self):
        for keywords, expected in data._INCIDENT_RULES:
            with self.subTest(keyword=keywords[0]):
                self.assertEqual(data.classify_event(event(EventSubType=keywords[0])), expected)
        self.assertEqual(data.headline_parts(event(IsFullClosure=True))[0:2], ("⛔", "FULL CLOSURE"))
        self.assertEqual(data.classify_event(event(EventSubType="debris", Description="fire")), ("🪨", "DEBRIS"))

    def test_text_bytes_direction_and_link(self):
        message = data.format_event_text(event())
        self.assertIn("NB [I19]", message)
        self.assertIn("%2C", message)
        self.assertLessEqual(len(message.encode()), 200)
        self.assertLessEqual(len(common.clamp("🔥" * 200).encode()), 200)

    def test_dispatch_fingerprint_and_stable_identity(self):
        one = event(Description="LOCATION: A AND B\nSTATUS: RCVD")
        two = event(Description="LOCATION: A AND B\nSTATUS: ON SCENE")
        self.assertEqual(data.fingerprint(one), data.fingerprint(two))
        changed = event(Description="New location")
        self.assertNotEqual(data.fingerprint(one), data.fingerprint(changed))
        # Coordinates and type alone are not in the fingerprint.
        moved = copy.deepcopy(one)
        moved["Latitude"] += .01
        self.assertEqual(data.fingerprint(one), data.fingerprint(moved))
        keys = [attempts.plan([ev], {}, NOW)[0]["incidents"][0]["waypoint_key"]
                for ev in (one, changed)]
        self.assertEqual(keys[0], keys[1])

    def test_waypoint_lifetimes(self):
        self.assertEqual(data.build_waypoint_dict(event(), NOW)["expire"], NOW + 7200)
        self.assertEqual(data.build_waypoint_dict(event(PlannedEndDate=NOW + 999999), NOW)["expire"], NOW + 7200)
        self.assertEqual(data.build_waypoint_dict(event(EventType="closures"), NOW)["expire"], NOW + 43200)
        self.assertEqual(data.build_waypoint_dict(event(EventType="closures", PlannedEndDate=NOW + 9999999), NOW)["expire"], NOW + 604800)
        self.assertEqual(data.build_waypoint_dict(event(PlannedEndDate=NOW + 600), NOW)["expire"], NOW + 600)

    def test_hourly_repeat_and_visible_updates(self):
        payload, ledger = attempts.plan([event()], {}, NOW)
        before = copy.deepcopy(ledger)
        for seconds in (300, 1800, 3599):
            self.assertEqual(attempts.plan([event()], ledger, NOW + seconds)[0]["count"], 0)
        noise = event(Description="Crash: dispatch updated", LastUpdated=NOW + 1800)
        self.assertEqual(attempts.plan([noise], ledger, NOW + 1800)[0]["count"], 0)
        repeated, twice = attempts.plan([noise], ledger, NOW + 3600)
        self.assertEqual(repeated["count"], 1)
        self.assertEqual(attempts.plan([event()], twice, NOW + 7200)[0]["count"], 0)
        closure = event(IsFullClosure=True)
        changed, updated = attempts.plan([closure], twice, NOW + 7300)
        self.assertEqual(changed["count"], 1)
        self.assertEqual(attempts.plan([event()], updated, NOW + 7400)[0]["count"], 0)
        self.assertEqual(attempts.plan([closure], updated, NOW + 10899)[0]["count"], 0)
        self.assertEqual(attempts.plan([closure], updated, NOW + 10900)[0]["count"], 1)
        self.assertEqual(ledger, before)
        self.assertEqual(payload["delivery_policy"], "two_visible_update_offers_one_hour_apart_without_receipts")

    def test_legacy_ledger_hourly_migration(self):
        ev = event()
        old = {"audit-1": {"fp": data.fingerprint(ev), "attempts": 2,
            "first_attempt": NOW-3600, "last_attempt": NOW-1800}}
        payload, migrated = attempts.plan([ev], old, NOW)
        self.assertEqual(payload["count"], 0)
        self.assertNotIn("alert_attempts",old["audit-1"])
        self.assertEqual(attempts.plan([ev],migrated,NOW+1799)[0]["count"],0)
        self.assertEqual(attempts.plan([ev],migrated,NOW+1800)[0]["count"],1)

    def test_cap_duplicate_ids_and_overflow(self):
        events = [event(str(i)) for i in range(4)]
        first, ledger = attempts.plan(events + [events[0]], {}, NOW)
        self.assertEqual(first["count"], 3)
        next_cycle, _ = attempts.plan(events, ledger, NOW + 300)
        self.assertEqual([i["event_id"] for i in next_cycle["incidents"]], ["3"])
        with patch.object(data, "MAX_MSGS", 1):
            self.assertEqual(attempts.plan(events, {}, NOW)[0]["count"], 1)
        with patch.object(data, "MAX_MSGS", 99):
            self.assertEqual(attempts.plan(events, {}, NOW)[0]["count"], 3)

    def test_fetch_contract(self):
        with patch.object(data, "ADOT_API_KEY", None), patch.object(data, "log_error"):
            self.assertEqual(data.fetch_traffic_events()[1], "ADOT_API_KEY missing")
        with patch.object(data, "ADOT_API_KEY", "synthetic"), patch.object(data, "http_get_json", return_value={}), patch.object(data, "log_error"):
            self.assertEqual(data.fetch_traffic_events()[1], "ADOT bad payload")
        with patch.object(data, "ADOT_API_KEY", "synthetic"), patch.object(data, "http_get_json", return_value=[event(), event("future", StartDate=NOW + 100)]), patch.object(data.time, "time", return_value=NOW):
            self.assertEqual(len(data.fetch_traffic_events()[0]), 1)

    def test_on_demand_parameters(self):
        with patch.dict(os.environ, {"MESSAGE": "!TrAfFiC I-19"}, clear=True):
            self.assertEqual(responder.get_highway_param(), "I-19")
        with patch.dict(os.environ, {"MESSAGE": "!traffic", "PARAM_1": "60"}, clear=True):
            self.assertEqual(responder.get_highway_param(), "60")
        with patch.dict(os.environ, {"MESSAGE": "!traffic"}, clear=True):
            self.assertIsNone(responder.get_highway_param())

    def run_responder(self, events, query=None, error=None):
        with patch.object(responder, "fetch_traffic_events", return_value=(events, error)), patch.object(responder, "get_highway_param", return_value=query), patch.object(responder, "respond") as single, patch.object(responder, "respond_multi") as multi, patch.object(responder, "log"), patch.object(responder, "log_error"):
            responder.main()
            return single.call_args, multi.call_args

    def test_on_demand_empty_error_and_metro_scope(self):
        self.assertIn("unavailable", self.run_responder([], error="offline")[0].args[0])
        self.assertIn("No active events for I-19", self.run_responder([], "I-19")[0].args[0])
        self.assertIn("Metro Phoenix", self.run_responder([event()])[0].args[0])
        metro = event(Latitude=33.4, Longitude=-111.9, RoadwayName="I-10")
        self.assertEqual(len(self.run_responder([metro])[1].args[0]), 1)

    def test_on_demand_priority_cap_and_roadwork(self):
        events = [event("work", EventType="roadwork", Description="Construction"),
                  event("closed", EventType="closures", Description="Closed"),
                  event("crash", LastUpdated=NOW + 1), event("older", LastUpdated=NOW - 1)]
        texts = self.run_responder(events, "19")[1].args[0]
        self.assertEqual(len(texts), 3)
        self.assertIn("MVA", texts[0])
        self.assertIn("CLOSED", texts[2])
        self.assertIn("ROADWORK", self.run_responder(events[:1], "19")[1].args[0][0])

    def test_cli_atomic_state_lock_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "attempts.json"
            ledger.write_text('{"version":1,"attempts":{}}')
            command = [sys.executable, str(ROOT / "traffic_attempts.py"), "--fixture", str(ROOT / "tests/events.fixture.json"), "--ledger", str(ledger), "--now", str(NOW)]
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(run.stdout)["simulation"])
            self.assertEqual(len(json.loads(ledger.read_text())["attempts"]), 3)
            self.assertFalse(ledger.with_suffix(".json.lock").exists())
            baseline = ledger.read_bytes()
            lock = ledger.with_suffix(".json.lock")
            lock.write_text("held")
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 1)
            self.assertEqual(ledger.read_bytes(), baseline)
            self.assertTrue(lock.exists())
            lock.unlink()
            ledger.write_text("bad json")
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 1)
            self.assertEqual(json.loads(run.stdout)["count"], 0)
            self.assertEqual(ledger.read_text(), "bad json")

if __name__ == "__main__":
    unittest.main()
