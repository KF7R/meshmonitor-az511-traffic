# MeshMonitor AZ511 Traffic

Arizona traffic alerts and road lookups for Meshtastic, using the ADOT AZ511 feed.

The recommended scheduled workflow uses MeshMonitor's native `action.broadcastWaypoint`: Python fetches and selects incidents, and MeshMonitor broadcasts their map pins and Traffic-channel text. The separate `!traffic` responder provides on-demand reports.

**Verified deployment:** MeshMonitor **4.17.0-rc1**, Docker on Raspberry Pi, October 2, 2026. The five-minute I-19/Santa Cruz timer is installed and enabled. A separate controlled test delivered both its TEST message and waypoint to a receiving app. Automatic delivery confirmation is unavailable in this release; the timer records bounded attempts.

![Scheduled Traffic automation enabled](docs/images/timer-enabled.jpg)

## Start here

- [Detailed installation and operating guide](docs/GUIDE.md)
- [Feature audit, test evidence, and known limitations](docs/FEATURES.md)
- [Disabled scheduled automation example](examples/traffic-scheduled.disabled.json)
- [On-demand automation example](examples/traffic-on-demand.disabled.json)
- [Manual live-test automation example](examples/traffic-test.disabled.json)

## What it does

| Workflow | Behavior |
| --- | --- |
| Scheduled alerts | Poll every five minutes; select new or changed high-impact events labeled I19/SCZ; up to three per cycle; native waypoint and Traffic text |
| Bounded retry | Offer an incident immediately and once more after at least 30 minutes; stop after two attempts for its current fingerprint |
| `!traffic` | Up to three Metro Phoenix freeway incidents/closures; this is **not** a Nogales digest |
| `!traffic I-19` | Up to three matching-road events across configured zones, including roadwork |
| Incident display | Classified emoji, normalized road, direction, zone tag, update time and Google Maps link |

Coverage uses rectangles, not a county boundary polygon. Overlapping zones can exclude northern I-19 from scheduled selection. See the [coverage audit](docs/FEATURES.md#known-limitations).

## Files and compatibility

| File | Purpose |
| --- | --- |
| `traffic_attempts.py` | Recommended native timer entry point; persists a separate attempt ledger |
| `traffic_native_data.py` | AZ511 filtering, classification, formatting, waypoint data and pure selection; no RF transport |
| `traffic_responder.py` | Existing text-only on-demand responder |
| `traffic_timer.py` | Deprecated compatibility module: re-exports native helpers for the responder; direct execution is silent and never transmits |
| `common.py` | Shared HTTP, text, state and output helpers |
| `examples/` | Disabled, portable automation examples; replace source placeholders before use |
| `tests/` | Synthetic feature audit; no live AZ511, RF or production state |

Install all five Python files together. The responder imports helpers through `traffic_timer.py`, which now re-exports them from `traffic_native_data.py`. The old Virtual Node transmitter has been removed. Executing the deprecated entry point prints an empty response and a deprecation warning; scheduled traffic uses the Automation Engine.

## Quick command reference

```text
!traffic
!traffic I-19
!traffic 19
!traffic SR-82
!traffic L202
```

Use `L202` or `202` for Loop 202. The currently implemented matcher does **not** support `loop202`, despite the old script docstring suggesting it. Commands search configured feed coverage, not every Arizona road.

## Run the feature audit

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The audit checks filters, all classifier rule groups, command parsing, responder priority, formatting, expiry, identity, attempts, overflow and scratch-ledger error handling. It also records known coverage and route-matching limitations rather than hiding them.

## Origin and attribution

Customized from the [Arizona Meshtastic Community traffic scripts](https://github.com/ArizonaMeshtasticCommunity/community-scripts/tree/main/traffic). The upstream project credits [Ted Malone's ADOT-511 project](https://github.com/temalo/ADOT-511). Retain these acknowledgments and consult the upstream repositories for applicable licensing terms.

Do not commit API keys, MeshMonitor tokens, production state, or Docker environment files.
