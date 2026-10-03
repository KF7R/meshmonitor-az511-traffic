# Feature audit and verification record

Checked October 2, 2026 against repository commit `176a121f09f3eb1cd8c6853be38ed0cfd439740c`, the additive native timer files included here, and installed MeshMonitor v4.17.0-rc1. This is an audit of Traffic features; unrelated weather helpers in `common.py` are not Traffic features and were not exercised.

## October 3 update

The current deployment runs MeshMonitor 4.17.0-rc2; the original detailed audit below used rc1. Area lookup tests and retired-entry-point tests bring the offline suite to 20 tests. Nogales, Rio Rico and Santa Cruz aliases are supported using approximate rectangles. Scheduled waypoint hop overrides were removed to inherit radio settings. The on-demand workflow now filters receiving sources before executing the script. Both receiving radios had triggered the same command previously; after the Spicy-only filter, the operator confirmed exactly three replies. The portable examples reflect these saved settings and remain disabled. Shared-variable concurrency and northern I-19 overlap remain unresolved.

## Scheduled duplicate correction (October 3)

The earlier two-attempt policy deliberately repeated a successful alert after 30 minutes. Description-only changes could also reset its budget while the displayed alert remained identical. This is superseded by one offer per visible headline/coordinate fingerprint, with per-ID history and quiet adoption of legacy entries. New tests cover long-term suppression, timestamp/prose churn, meaningful updates, reversion and migration. The current suite passes 26 tests. The older retry/fingerprint findings below describe the original implementation and are historical; see the current guide for the active policy. Different event IDs and transport-level duplicates remain possible. No receiver acknowledgment or automatic retry is added.

## Feature inventory

| Feature | Actual behavior | Evidence |
| --- | --- | --- |
| AZ511 fetch | Full event request; two attempts, eight-second per-request timeout and backoff; geographic/active-start filtering | Code, mocked-feed audit, installed read-only fetch succeeded |
| Five-minute polling | Cron `*/5 * * * *` | Saved live configuration; initial scheduled run completed |
| Scheduled coverage | Only first-matching zone tags I19/SCZ | Code and synthetic geographic checks; overlap limitation below |
| Event types | Scheduled incidents/closures; named-road queries also accept roadwork and hazard records in TAGS | Synthetic tests |
| Active start / age | Future valid starts excluded; timer default 24-hour StartDate window; missing/malformed starts accepted | Synthetic tests |
| Native transport | `action.broadcastWaypoint` plus `action.sendMessage`; producer has no radio sender | Code, dry run and one controlled receiving-app confirmation |
| Scheduled output limit | Up to three incidents; duplicate IDs cannot occupy more than one slot; overflow deferred | Synthetic tests |
| Scheduled order | Feed order; no severity sort in native timer | Code inspection |
| Scheduled offers | Once per visible version, with no timed retry | Regression tests |
| Change detection | Per-ID visible headline and rounded coordinates; history suppresses reversion | Synthetic tests |
| Ledger durability | Atomic replace; exclusive file lock; persisted before output; separate from legacy state | Scratch CLI tests and code inspection |
| On-demand parsing | MESSAGE or older PARAM_*; case-insensitive parser | Synthetic tests |
| Default digest | Metro Phoenix high-impact freeway records, not I-19/Santa Cruz | Synthetic tests |
| Named-road results | Search recognized types within configured zones; fuzzy route matching | Synthetic tests, including known limitations |
| On-demand ranking | Incidents, closures, roadwork, then other types; newest LastUpdated per type; top three | Synthetic tests |
| On-demand output transport | All four message actions use Nogales Spicy and hop limit 3, capped by radio limit | Saved installed JSON reopened and verified; no additional RF query during audit |
| On-demand state isolation | Responder does not mark scheduled incidents handled | Code inspection |
| Classification | Ordered keyword rules; subtype precedence; full-closure override | Every keyword rule group exercised synthetically |
| Text formatting | Direction, zone tag, local update time, Maps link with `%2C`; byte/character clamp | Synthetic tests |
| Native icon | Full classifier emoji passed to MeshMonitor icon field | Code and synthetic checks; live controlled test used 🧪 |
| Stable native key | SHA1 of full external ID, independent of slot; namespace includes source and automation | Synthetic key checks and native service inspection |
| Pin expiry | Incidents two-hour cap/fallback; closures planned end up to seven days, 12-hour fallback | Synthetic tests |
| Skip/failure routing | Waypoint errors/skips do not prevent following text automatically in rc1 | Release evaluator inspection and prior isolated native harness |
| Legacy compatibility | Deprecated entry point re-exports native helpers for responder imports; original transmitter removed | Imports and code inspection |

## Coverage table

All coordinates are decimal degrees. First matching row wins; bounds are inclusive. Only EV/CV/WV have the `metro` flag.

| Tag | Area | Latitude | Longitude | Road restriction |
| --- | --- | --- | --- | --- |
| EV | East Valley | 33.270107–33.682147 | -111.998348–-111.493658 | None |
| CV | Central Valley | 33.240569–33.686600 | -112.200652–-111.876859 | None |
| WV | West Valley | 33.406478–33.881875 | -112.665736–-112.124652 | None |
| I17S | I-17 Valley–Cordes Junction | 33.651269–34.358054 | -112.240066–-112.003610 | None |
| I17N | I-17 Cordes–Rim Country | 34.273238–34.943821 | -112.549486–-111.486109 | None |
| FLG | Flagstaff/Williams/I-40 | 34.9–35.3 | -112.6–-111.4 | None |
| SR87 | Valley–Payson | 33.430891–34.291454 | -112.003610–-111.261702 | None |
| US60 | Valley–Globe | 33.233331–33.430890 | -111.622454–-110.696897 | None |
| I10CG | I-10/SR-347 Casa Grande | 32.781226–33.313537 | -112.121039–-111.596782 | None |
| PINAL | Pinal state routes | 32.715314–33.398183 | -111.884583–-111.273850 | None |
| TUS | I-10 Casa Grande–Tucson | 32.179406–33.0 | -111.767030–-110.954972 | None |
| I19 | Tucson–Nogales | 31.33–32.22 | -111.12–-110.90 | I-19 fuzzy match |
| SCZ | Santa Cruz approximate box | 31.33–31.80 | -111.08–-110.43 | None |
| I10W | Valley–California | 33.388030–33.721330 | -114.561269–-112.362901 | None |

Coverage gaps outside these rectangles are intentional in the original helpers; examples include portions of SR-85, northwest US-60 and eastern Tucson. A box can include locations outside its named jurisdiction. No point-in-county polygon test exists.

## Known limitations

1. **Northern I-19 overlap.** A synthetic I-19 incident at `32.2, -110.99` matches the earlier TUS rectangle. Its tag is TUS, so the scheduled I19/SCZ filter rejects it. The timer is accurately described as accepting I19/SCZ-tagged events, not guaranteeing every I-19 event. A coverage change requires reviewing the helper and deploying a code change; this documentation update does not change live coverage.
2. **Loop spelling.** `loop202` fails because the roadway normalizes to `L-202`, but the query is only stripped of punctuation. Use `L202`, `L-202` or `202`.
3. **Named-road substring matches.** `i19` also matches `I-190` in a synthetic example; bare `19` uses a boundary-aware route pattern. The I19 zone's road restriction uses the same matcher.
4. **Fingerprint scope.** Coordinate/type/update-time-only changes do not produce a new fingerprint. The helper's older comment claiming all location/type changes retrigger is broader than the implementation. Description changes and sanitized location text changes can retrigger.
5. **No receipt-driven retry.** Saving an attempt does not establish delivery. Two failures exhaust the budget; a first success may be duplicated once. Reverting description versions can refresh the budget because the ledger keeps only the current fingerprint.
6. **Native result gap.** A throttled, disconnected or TX-disabled waypoint can be followed by a text attempt. JSON action ordering is not success gating.
7. **Shared-variable concurrency.** The separate scheduled variable avoids collisions with on-demand data, but doesn't isolate simultaneous runs of the same workflow. The ledger lock doesn't protect downstream sends. Reset plus script failure can still leave stale state if reset itself fails. The preserved on-demand rule has no reset; simultaneous requests/script failures deserve separate validation.
8. **No automatic incident cancellation.** Removed records don't delete existing pins. TTL, retries and clock skew affect how long a pin stays visible.
9. **Native ID namespace.** Keys are stable within the same source and automation, not across copied/recreated automations or the old numeric-ID path.
10. **UI limitations.** The Test panel stubs scripts; Variables shows defaults rather than the latest useful output. “Completed” means evaluated/dispatched, not received on all radios.
11. **Input assumptions.** Normal feed values are expected: malformed coordinates or invalid LastUpdated values may fail processing. Missing IDs fall back to `0` and can collapse records. Error handling fails the script rather than validating every external field independently.
12. **Text and description differences.** UTF-8 truncation can cut long Maps links. Waypoint description currently keeps raw feed prose, including possible CAD detail. Time display uses the container's timezone.

These are recorded findings, not resolved defects. The live settings were changed only for the requested on-demand source/hop limit during this review.

## Installer verification

The interactive installer adds five offline tests: configured examples, private-file permissions/symlink rejection, preserving existing API objects, disabled-only creation with remote HTTPS checks, and a full prepare-only CLI run in a temporary directory. Together the suite now has 25 tests. Docker recreation and live authenticated API creation were not exercised; see [installer limitations](INSTALLER.md).

## Verification completed

### Automated feature audit

`python3 -m unittest discover -s tests -v` passes **26 tests** with subtests over every classifier keyword group. It covers:

- zone inventory, southern coverage and the northern overlap;
- broad event type, future start, age and geographic filtering;
- route normalization, digit boundaries and known alias/substring behavior;
- classifier ordering, full closures, direction and UTF-8 text limits;
- CAD sanitization, description fingerprints and stable native keys;
- incident/closure/planned-end expiry rules;
- first offer, 1799-second suppression, 1800-second retry and exhaustion;
- description updates, input-state immutability, caps and overflow;
- missing key, bad feed shape and mocked active filtering;
- MESSAGE/PARAM parsing, empty/error replies, metro scope and ranked results;
- actual CLI persistence, lock contention and malformed scratch ledgers.

These checks never request the live feed or use production state. The retired timer entry point is checked only for silent output and compatibility imports. They test expected behavior and expose implementation limitations; passing does not certify all external data or hardware conditions.

### Installed deployment

- Operator confirmed `Traffic timer imports OK` inside the container as `node`.
- Read-only AZ511 fetch returned `AZ511 fetch OK: 1780 events` across query coverage at the time checked.
- Scheduled rule was enabled and its initial run completed with three false presence conditions. The latest script payload was not available in the Variables list; this alone does not establish a successfully processed incident.
- A separate disabled manual-test rule dry-ran one waypoint plus one text to channel 3.
- Its single live run completed the native waypoint, pause and message actions.
- Operator reported receiving **both** TEST message and waypoint in the receiving app.
- The saved on-demand rule was reopened and all four send actions verified as Nogales Spicy source + hop limit 3. The later Spicy-only receive filter was confirmed by the operator with three replies; see the October 3 update.

### Still requires operational observation

A real qualifying AZ511 incident traversing the scheduled Python-runner/variable/action chain, concurrent requests, process interruption between ledger save and output, feed disappearance during the retry window, native throttling on a changed real incident, and restart recovery are not established by the controlled fixed-payload radio test. No extra live transmissions were performed for this feature audit.

### Optional reproduction of the native graph audit

The nine-scenario native audit is separate from the Python tests. It validates all three examples with the real release validator and evaluates the scheduled graph with transport/script IO mocked. It does not start a server, connect a radio, or use a database. Regex evaluation is intentionally outside this harness: its RE2 stub throws if used.

With Node.js/npm and Git available, from the repository root:

```bash
git clone --branch v4.17.0-rc1 --depth 1 https://github.com/Yeraze/meshmonitor.git /tmp/meshmonitor-traffic-audit
git -C /tmp/meshmonitor-traffic-audit rev-parse HEAD
# Expected release commit: f1f3fd29848d6d505dad64a0daf01503a7ad53c3
npm install --prefix tests/test-runtime --ignore-scripts --no-audit --no-fund typescript@5.9.3
node tests/compile_native.cjs /tmp/meshmonitor-traffic-audit
node tests/audit_native.mjs tests/native-modules .
```

Confirm the checkout commit before evaluating. `compile_native.cjs` transpiles only the validator/evaluator modules and their local dependencies; it is not a full MeshMonitor build. The injected script uses `native.fixture.output.json`, not the deployed Python script runner. `native-audit-results.json` records the latest isolated results, with no claim of end-to-end delivery.
