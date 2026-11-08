"""Read-only readiness checks. No claims, uploads, or paid model requests."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from config import workspace_root, runtime, key_file, REPOSITORY
from checker import command
import lifecycle as life
from transport import request, fetch


def fingerprint():
    from matchflow import digest
    inputs = dict(workspace=str(workspace_root()), toolchain=runtime(), key_file=str(key_file()),
                  sources={p.name: digest(p) for p in sorted((REPOSITORY/'tools/matchflow').glob('*.py'))},
                  wrapper=digest(REPOSITORY/'tools/matchflow/check.sh'),
                  plugin={p.name: digest(p) for p in sorted((REPOSITORY/'integrations/opencode').glob('*.mjs'))},
                  policy=digest(REPOSITORY/'docs/matching-policy.md'),
                  prompt=digest(REPOSITORY/'tools/matchflow/prompts/worker.txt'),
                  agent=digest(REPOSITORY/'.opencode/agents/match-worker.md'))
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def run(online=False, api=request, download_url=None):
    result = dict(schema_version=3, checked_at=life.stamp(), fingerprint=fingerprint(),
                  online=online, ready=False, checks={})
    ws = workspace_root()
    result['checks']['workspace'] = ws.is_dir()
    result['checks']['credential_file'] = key_file().is_file()
    process = subprocess.run(command([], mode='preflight'), capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=210)
    try:
        local = json.loads(process.stdout.strip())
    except ValueError:
        local = dict(status='launcher_failed', exit_code=process.returncode)
    result['checks']['toolchain'] = local
    if online:
        welcome = api('start_here', {})
        status = api('status', {})
        result['checks']['service'] = dict(connected=isinstance(welcome, dict) and bool(status.get('tier')),
                                          notices=status.get('notices', []), next_step=status.get('next_step'))
        # The checker has its own upstream transport. Probe that exact read-only endpoint
        # through its Python module, without uploading an attempt or modifying the kit.
        probe = subprocess.run(command(['--probe-notes']), capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=100)
        try:
            result['checks']['notes_transport'] = json.loads(probe.stdout.strip())
        except ValueError:
            result['checks']['notes_transport'] = dict(ready=False, error='invalid probe output')
        if download_url:
            fetch(download_url)
            result['checks']['download'] = 'verified'
        else:
            result['checks']['download'] = 'not_probed_without_official_pack_url'
    result['ready'] = (result['checks']['workspace'] and result['checks']['credential_file'] and
                       process.returncode == 0 and local.get('status') == 'ready' and online and
                       result['checks'].get('service', {}).get('connected') is True and
                       result['checks'].get('notes_transport', {}).get('ready') is True)
    return result


def require(path=None):
    from batch import fresh
    path = path or workspace_root()/'work/matchflow/preflight.json'
    record = life.load(path)
    fresh(record['checked_at'])
    if not record.get('ready') or record.get('fingerprint') != fingerprint():
        raise ValueError('run a fresh successful online preflight before launch')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--download-url', help='official exact-claim pack URL, optional read-only download probe')
    args = parser.parse_args()
    folder = workspace_root()/'work/matchflow'
    folder.mkdir(parents=True, exist_ok=True)
    # An observed failed probe must invalidate any earlier successful readiness.
    life.save(folder/'preflight.json', dict(ready=False, checked_at=life.stamp(), state='probing'))
    try:
        result = run(args.online, download_url=args.download_url)
        life.save(folder/'preflight.json', result)
        print(json.dumps(result, indent=2))
        return 0 if result['ready'] else 2
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        life.save(folder/'preflight.json', dict(ready=False, checked_at=life.stamp(), error=str(exc)))
        print(str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
