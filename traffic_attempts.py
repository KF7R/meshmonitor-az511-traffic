"""Two offers per visible update, at least an hour apart; not delivery receipts."""
import argparse
import copy
import hashlib
import json
import os
import time
from pathlib import Path
import traffic_native_data as producer

MAX_ATTEMPTS = 2
RETRY_SECONDS = 3600

def alert_fingerprint(ev):
    # Ignore timestamp and raw dispatch prose. Match the useful visible facts.
    facts = [producer.event_headline(ev), round(float(ev.get('Latitude', 0)), 4),
             round(float(ev.get('Longitude', 0)), 4)]
    return hashlib.sha256(json.dumps(facts, ensure_ascii=True).encode()).hexdigest()

def plan(events, ledger, now):
    suppressed = {}
    updated = copy.deepcopy(ledger)
    visible = {}
    for ev in events:
        eid = str(ev.get('ID') or ev.get('Id') or '0')
        afp = alert_fingerprint(ev)
        visible[eid] = afp
        entry = updated.get(eid)
        if entry is None:
            continue
        # Quietly adopt existing IDs on the first post-upgrade observation.
        # Resetting history would reannounce every currently active incident.
        if 'alert_fps' not in entry:
            entry['alert_fps'] = [afp]
        if 'alert_attempts' not in entry:
            # Historical versions stay exhausted; the currently visible version
            # gets at most one repeat measured from the prior actual attempt.
            entry['alert_attempts'] = {fp: {'attempts': 2, 'last_attempt': entry['last_attempt']}
                                      for fp in entry['alert_fps']}
            if afp in entry['alert_fps']:
                entry['alert_attempts'][afp] = {'attempts': 1, 'last_attempt': entry['last_attempt']}
        version = entry['alert_attempts'].get(afp)
        if version and (version['attempts'] >= MAX_ATTEMPTS
                or now - version['last_attempt'] < RETRY_SECONDS):
            suppressed[eid] = {'fp': producer.fingerprint(ev),
                'first_seen': entry['first_attempt'], 'last_seen': now}
    payload = producer.prepare_cycle(events, suppressed, now)
    for incident in payload['incidents']:
        eid, fp = incident['event_id'], incident['fingerprint']
        previous = ledger.get(eid, {})
        same = previous.get('fp') == fp
        history = list(updated.get(eid, {}).get('alert_fps', []))
        if visible[eid] not in history:
            history.append(visible[eid])
        versions = copy.deepcopy(updated.get(eid, {}).get('alert_attempts', {}))
        afp = visible[eid]
        versions[afp] = {'attempts': versions.get(afp, {}).get('attempts', 0) + 1,
                         'last_attempt': now}
        updated[eid] = {'fp': fp, 'attempts': previous['attempts'] + 1 if same else 1,
                        'first_attempt': previous['first_attempt'] if same else now,
                        'last_attempt': now, 'alert_fps': history, 'alert_attempts': versions}
    payload.pop('maintenance_state', None)
    payload['delivery_policy'] = 'two_visible_update_offers_one_hour_apart_without_receipts'
    payload['exhausted_count'] = len(updated)
    return payload, updated

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fixture', type=Path)
    mode.add_argument('--prepare', action='store_true')
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--now', type=int)
    args = parser.parse_args()
    if args.now is not None and not args.fixture:
        parser.error('--now requires --fixture')
    lock = args.ledger.with_suffix(args.ledger.suffix + '.lock')
    acquired = False
    temp = args.ledger.with_suffix(args.ledger.suffix + '.tmp')
    try:
        # Missing parent is an error. Never silently create a production path.
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        acquired = True
        document = json.loads(args.ledger.read_text(encoding='utf-8'))
        if document.get('version') != 1 or not isinstance(document.get('attempts'), dict):
            raise ValueError('Expected a version 1 attempt ledger')
        ledger = document['attempts']
        for eid, entry in ledger.items():
            if 'alert_attempts' in entry:
                if not isinstance(entry['alert_attempts'], dict):
                    raise ValueError('Invalid visible attempt history')
                for version in entry['alert_attempts'].values():
                    if (not isinstance(version, dict)
                            or type(version.get('attempts')) is not int
                            or version['attempts'] < 1
                            or not isinstance(version.get('last_attempt'), (int, float))):
                        raise ValueError('Invalid visible attempt entry')
            if 'alert_fps' in entry and (not isinstance(entry['alert_fps'], list)
                    or any(not isinstance(fp, str) for fp in entry['alert_fps'])):
                raise ValueError('Invalid visible-update history')
            if not isinstance(entry, dict) or not isinstance(entry.get('fp'), str):
                raise ValueError('Invalid attempt entry')
            if type(entry.get('attempts')) is not int or entry['attempts'] < 1:
                raise ValueError('Invalid attempt count')
            for key in ('first_attempt', 'last_attempt'):
                if not isinstance(entry.get(key), (int, float)):
                    raise ValueError('Invalid attempt timestamp')
        now = args.now if args.now is not None else int(time.time())
        if args.fixture:
            raw = json.loads(args.fixture.read_text(encoding='utf-8'))
            if not isinstance(raw, list):
                raise ValueError('Expected an event list')
            events = [dict(ev, _zone=producer.match_zone(ev)) for ev in raw
                      if producer.match_zone(ev) is not None and producer.is_active_now(ev, now)]
        else:
            events, error = producer.fetch_traffic_events()
            if error:
                raise ValueError(error)
        payload, updated = plan(events, ledger, now)
        # Persist before handing output to MeshMonitor. A crash may consume an
        # attempt without a send; it cannot generate unlimited duplicate offers.
        with temp.open('w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'attempts': updated}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, args.ledger)
        payload['simulation'] = bool(args.fixture)
        print(json.dumps(payload, ensure_ascii=True))
        return 0
    except Exception as error:
        print(json.dumps({'count': 0, 'response': '', 'error': str(error),
                          **{f'incident{i}': {'present': False} for i in range(3)}}))
        return 1
    finally:
        if acquired:
            lock.unlink(missing_ok=True)

if __name__ == '__main__':
    raise SystemExit(main())
