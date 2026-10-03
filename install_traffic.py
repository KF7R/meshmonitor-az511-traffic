#!/usr/bin/env python3
"""Interactive Docker/Linux installer. No dependencies beyond Python 3."""
import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

FILES = ('common.py', 'traffic_timer.py', 'traffic_responder.py',
         'traffic_native_data.py', 'traffic_attempts.py')

def ask(label, default=''):
    value = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return value or str(default)

def yes(label):
    return input(label + ' [y/N]: ').strip().lower() in ('y', 'yes')

def private_write(path, content):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing a symlink output')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)

def configure(example, source, channel, hops):
    doc = json.loads(json.dumps(example))
    doc['enabled'] = False
    for node in doc['config']['nodes']:
        p = node.get('params', {})
        if 'sourceId' in p:
            p['sourceId'] = source
        if 'sourceIds' in p:
            p['sourceIds'] = [source]
        if node['type'] in ('action.broadcastWaypoint', 'action.sendMessage'):
            if 'channel' in p:
                p['channel'] = channel
            if hops is None:
                p.pop('hopLimit', None)
            else:
                p['hopLimit'] = hops
    doc['description'] = 'Installer configured; selected receiver/sender, reviewed channel and hop settings. Initially disabled.'
    return doc

def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        # Compose errors can include expanded secrets. Never echo raw output.
        raise RuntimeError(f'{argv[0]} command failed (exit {result.returncode}); inspect locally and redact secrets.')
    return result.stdout

def api(base, token, method, route, body=None):
    url = base.rstrip('/') + '/api/automations' + route
    request = urllib.request.Request(url, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'MeshMonitor API returned HTTP {error.code}; check token permissions. No credential fallback is attempted.') from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('MeshMonitor API connection failed; check URL and service health.') from None

def setup_api(base, token, documents):
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use an HTTP(S) URL without embedded credentials')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Use HTTPS for remote API connections; HTTP is limited to localhost')
    existing = api(base, token, 'GET', '')
    variables = api(base, token, 'GET', '/variables')
    if not isinstance(existing, list) or not isinstance(variables, list):
        raise ValueError('Unexpected API response; use the generated files instead')
    for name in ('traffic', 'traffic_scheduled'):
        found = [v for v in variables if v['name'] == name]
        if found:
            if found[0]['type'] != 'json' or found[0]['scope'] != 'global' or found[0].get('readonly'):
                raise ValueError(f'Existing {name} variable has incompatible settings; preserved unchanged')
        else:
            api(base, token, 'POST', '/variables', {'name': name, 'type': 'json',
                'scope': 'global', 'readonly': False, 'config': {'defaultValue': {}}})
    for doc in documents:
        if any(a['name'] == doc['name'] for a in existing):
            print('Preserved existing automation:', doc['name'])
        else:
            api(base, token, 'POST', '', doc)
            print('Created disabled automation:', doc['name'])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true',
                        help='Generate configured imports only; no Docker, key or API changes')
    args = parser.parse_args()
    if os.name != 'posix':
        raise ValueError('Run this installer on the Linux Docker host (for example your Pi over SSH)')
    root = Path(__file__).resolve().parent
    for name in FILES:
        if not (root / name).is_file():
            raise ValueError('Run from a complete checkout of this repository')
    print('AZ511 Traffic installer. Obtain a key at https://www.az511.gov/developers/doc')
    print('Existing automations and ledgers are preserved. New automations remain disabled.')
    source = str(uuid.UUID(ask('Radio source UUID (from its MeshMonitor URL)')))
    channel = int(ask('Traffic channel index', '3'))
    if not 0 <= channel <= 7:
        raise ValueError('Channel index must be 0 through 7')
    hop_text = ask('Hop limit: inherit or a number 0–7', 'inherit').lower()
    hops = None if hop_text == 'inherit' else int(hop_text)
    if hops is not None and not 0 <= hops <= 7:
        raise ValueError('Hop limit must be inherit or 0 through 7')
    output = Path(ask('Private setup output directory', str(Path.home() / 'meshmonitor/traffic-setup'))).expanduser().resolve()
    if output == root or root in output.parents:
        raise ValueError('Choose an output directory outside the repository to keep private setup out of Git')
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    docs = []
    for name in ('traffic-scheduled.disabled.json', 'traffic-on-demand.disabled.json', 'traffic-test.disabled.json'):
        doc = configure(json.loads((root / 'examples' / name).read_text()), source, channel, hops)
        private_write(output / name, json.dumps(doc, indent=2) + '\n')
        private_write(output / (name + '.graph.json'), json.dumps(doc['config'], indent=2) + '\n')
        docs.append(doc)
    if args.prepare_only:
        print('Configured disabled imports saved to', output)
        return
    compose_file = Path(ask('Existing Docker Compose file', str(Path.home() / 'meshmonitor/docker-compose.yml'))).expanduser().resolve(strict=True)
    service = ask('Compose MeshMonitor service name', 'meshmonitor')
    container = ask('Running MeshMonitor container name', 'meshmonitor')
    mounts = json.loads(run(['docker', 'inspect', '--format', '{{json .Mounts}}', container]))
    mount = next((m for m in mounts if m['Destination'] == '/data/scripts' and m['Type'] == 'bind'), None)
    if not mount:
        raise ValueError('This installer requires an existing bind mount at /data/scripts. Configure it first using the guide; named volumes are not modified.')
    scripts = Path(mount['Source']).resolve(strict=True)
    node_uid = int(run(['docker', 'exec', container, 'id', '-u', 'node']).strip())
    if node_uid != os.getuid():
        raise ValueError('Host user UID must match container node UID for safe file ownership; resolve permissions before installing')
    key = getpass.getpass('ADOT API key (hidden; stored only in private Compose override): ').strip()
    if not key:
        raise ValueError('An API key is required')
    override = output / 'compose.traffic.json'
    private_write(override, json.dumps({'services': {service: {'environment': {'ADOT_API_KEY': key}}}}, indent=2) + '\n')
    key = None
    compose = ['docker', 'compose', '-f', str(compose_file), '-f', str(override)]
    run(compose + ['config', '--quiet'])
    print('Install scripts to', scripts)
    print('Private setup files:', output)
    print('Applying Docker settings recreates only MeshMonitor and briefly disconnects radios.')
    if not yes('Install files and recreate MeshMonitor now?'):
        print('Prepared imports and private override only. No installed scripts or containers changed.')
        return
    backup = output / ('backup-' + time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    for name in FILES:
        target = scripts / name
        if target.is_symlink():
            raise ValueError('Refusing symlink script destination')
    state = scripts / 'state'
    if state.is_symlink():
        raise ValueError('Refusing symlink state directory')
    state.mkdir(exist_ok=True)
    ledger = state / 'traffic_attempts.json'
    if ledger.is_symlink():
        raise ValueError('Refusing symlink ledger')
    if ledger.exists():
        shutil.copy2(ledger, backup / ledger.name)
    for name in FILES:
        target = scripts / name
        if target.exists():
            shutil.copy2(target, backup / name)
        shutil.copyfile(root / name, target)
        os.chmod(target, 0o644)
    if not ledger.exists():
        private_write(ledger, '{"version": 1, "attempts": {}}\n')
    run(compose + ['up', '-d', '--no-deps', '--force-recreate', service])
    run(['docker', 'exec', '-u', 'node', container, 'python3', '-c',
         "import sys; sys.path.insert(0,'/data/scripts'); import traffic_attempts, traffic_responder; print('Imports OK')"])
    print('Scripts installed; imports passed. No RF test was sent. Backup:', backup)
    print('For future Compose operations include BOTH files:')
    import shlex
    command = shlex.join(compose)
    print(command + ' up -d')
    private_write(output / 'OPERATING-NOTES.txt',
        'Keep the private override to retain the API key on recreation.\n' + command + ' up -d\n' +
        'To roll back: disable Traffic workflows in the UI, restore script backups and use your original Compose configuration.\n' +
        'Never erase an existing attempt ledger during rollback.\n')
    if yes('Create missing disabled automations using a MeshMonitor API token?'):
        base = ask('MeshMonitor base URL', 'http://localhost:8080')
        token = getpass.getpass('API token with automations read/write permissions (hidden, not saved): ')
        if not token:
            raise ValueError('Token required for automatic API setup')
        setup_api(base, token, docs)
    else:
        print('Create global JSON variables traffic and traffic_scheduled (default {}).')
        print('Import envelopes via the UI, or paste each .graph.json into Advanced JSON and enter its name separately.')
    print('Review source/channel/hops, disable any old scheduled Traffic timer, then enable the desired workflows in the UI.')
    print('Keep the manual TEST workflow disabled. Existing matching-name workflows were not updated.')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print('Installer stopped:', error, file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled. Check any already-created files/disabled workflows before retrying.', file=sys.stderr)
        sys.exit(1)
