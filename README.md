# MeshMonitor AZ511 Traffic

Arizona traffic alerts and on-demand traffic lookups for Meshtastic / MeshMonitor using the Arizona ADOT 511 API.

## Origin and attribution

This repository is a customized deployment derived from the **Arizona Meshtastic Community** traffic scripts in:

https://github.com/ArizonaMeshtasticCommunity/community-scripts/tree/main/traffic

The upstream project contains `traffic_timer.py`, `traffic_responder.py`, and `common.py`, and describes the original traffic-alert architecture, AZ511 integration, deduplication, coverage zones, and Meshtastic traffic commands.

The upstream project also credits **Ted Malone's ADOT-511 project** as an inspiration for bringing Arizona traffic data onto the mesh:

https://github.com/temalo/ADOT-511

This KF7R repository preserves that attribution and contains the production-customized versions used on the Nogales MeshMonitor installation.

## KF7R production customizations

The production version includes additional work and deployment-specific changes, including:

- Expanded southern Arizona coverage, including the I-19 Tucson–Nogales corridor and Santa Cruz County.
- Traffic event filtering and formatting refinements.
- Roadway normalization and matching improvements.
- New/changed-event state and retry handling.
- Stable waypoint IDs derived from AZ511 event IDs.
- Direct Meshtastic `WAYPOINT_APP` transmission through the MeshMonitor Virtual Node.
- Virtual Node defaults to `localhost:4405`.
- Traffic waypoints default to Meshtastic channel index 3 (Traffic).
- Waypoint transmission must succeed before an event is marked seen.
- Failed waypoint transmissions remain eligible for retry.
- Existing MeshMonitor automation continues to transmit the returned text alerts.
- On-demand `!traffic` responder remains separate from scheduled automatic alerts.

## Files

- `traffic_timer.py` — scheduled AZ511 polling, new/changed-event detection, automatic text alerts, and waypoint transmission.
- `traffic_responder.py` — on-demand `!traffic` queries.
- `common.py` — shared MeshMonitor utility functions.

## Production timer

The scheduled MeshMonitor timer is configured to run `traffic_timer.py` every 5 minutes on the Traffic channel.

Example cron:

```
*/5 * * * *
```

## Required environment

```
ADOT_API_KEY=<your AZ511 API key>
```

Waypoint transport defaults:

```
MESHMONITOR_VN_PORT=4405
MESHMONITOR_WAYPOINT_HOP_LIMIT=3
WAYPOINT_CHANNEL=3
```

Do **not** commit API keys, MeshMonitor tokens, or other secrets to this repository.

## License / upstream terms

This repository is a derivative/customized deployment. Refer to the upstream repositories for their applicable licensing and attribution terms.
