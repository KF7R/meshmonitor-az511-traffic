"""Bounded attempt scheduler. Attempts are NOT delivery receipts."""
import argparse
import json
import os
import time
from pathlib import Path
import traffic_native_data as producer

RETRY_SECONDS = 1800
MAX_ATTEMPTS = 2

def plan(events, ledger, now):
    suppressed = {}
    for eid, entry in ledger.items():
        if entry['attempts'] >= MAX_ATTEMPTS or now - entry['last_attempt'] < RETRY_SECONDS:
            suppressed[eid] = {'fp': entry['fp'], 'first_seen': entry['first_attempt'], 'last_seen': now}
    payload = producer.prepare_cycle(events, suppressed, now)
    updated = dict(ledger)
    for incident in payload['incidents']:
        eid, fp = incident['event_id'], incident['fingerprint']
        previous = ledger.get(eid, {})
        same = previous.get('fp') == fp
        updated[eid] = {'fp': fp, 'attempts': previous['attempts'] + 1 if same else 1,
                        'first_attempt': previous['first_attempt'] if same else now,
                        'last_attempt': now}
    payload.pop('maintenance_state', None)
    payload['delivery_policy'] = 'bounded_attempts_without_receipts'
    payload['exhausted_count'] = sum(e['attempts'] >= MAX_ATTEMPTS for e in updated.values())
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
