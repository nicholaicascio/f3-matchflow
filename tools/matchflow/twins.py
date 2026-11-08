"""Pinned f3twins adapter. Public dependency here; private cache always external."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import urllib.request

from config import REPOSITORY, settings, workspace_root, runtime
from security import private_root, scoped, no_links
import lifecycle as life

LOCK = REPOSITORY/'third_party/f3twins.lock.json'


def lock():
    return life.load(LOCK)


def dependency():
    return REPOSITORY/'.deps/f3twins'/lock()['commit']


def install():
    spec = lock()
    root = no_links(dependency())
    root.mkdir(parents=True, exist_ok=True)
    for name, expected in spec['files'].items():
        path = scoped(root, name)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('installed public dependency differs from lock; refusing overwrite')
            continue
        url = 'https://raw.githubusercontent.com/mathisto/f3twins/'+spec['commit']+'/'+name
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(1024 * 1024)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('upstream dependency hash mismatch')
        with path.open('xb') as output:
            output.write(data)
    return dict(installed=str(root), commit=spec['commit'], license='MIT')


def cache_root():
    return private_root(workspace_root()/'work/matchflow/twins-cache')


def fnv_root():
    value = settings().get('fnv_decomp')
    if not value or not Path(value).is_absolute():
        raise ValueError('configure absolute fnv_decomp dependency path in matchflow.local.json')
    return no_links(value).resolve()


def inputs():
    config = runtime()
    config.update(settings().get('twins_inputs', {}))
    return {k: config[k] for k in ('image', 'index', 'symbols')}


def identity():
    from matchflow import digest
    config = inputs()
    for value in config.values():
        if not Path(value).is_file():
            raise ValueError('configure host-readable twins_inputs image/index/symbols')
    fnv = fnv_root()
    rev = subprocess.run(['git', '-C', str(fnv), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(fnv), 'status', '--porcelain', '--untracked-files=all'],
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise ValueError('FNV reference must be a clean pinned checkout')
    return dict(upstream=lock()['commit'], inputs={k: digest(v) for k, v in config.items()},
                fnv_commit=rev, fnv_units=digest(fnv/'build/units.json'))


def bridge_identity():
    value = settings().get('fnv_x360')
    if not value or not Path(value).is_absolute():
        raise ValueError('configure an absolute fnv_x360 public reference path for the optional bridge')
    root = no_links(value).resolve()
    revision = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all'],
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise ValueError('Xbox reference must be a clean pinned checkout')
    return dict(root=str(root), revision=revision)


def build(command):
    if command not in ('build', 'all', 'bridge'):
        raise ValueError('unsupported twins build operation')
    from matchflow import digest
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    current = identity()
    if command == 'all' and life.load(root/'build-inputs.json') != current:
        raise ValueError('twins inputs changed; rebuild features first')
    for name, expected in lock()['files'].items():
        if digest(dependency()/name) != expected:
            raise ValueError('dependency integrity failed; reinstall pinned f3twins')
    env = os.environ.copy()
    config = inputs()
    env.update(F3CHECK_IMAGE=config['image'], F3CHECK_INDEX=config['index'], F3CHECK_SYMBOLS=config['symbols'],
               FNV_DECOMP=str(fnv_root()), F3TWINS_CACHE=str(root), F3TWINS_SEEDS=str(dependency()/'seeds.json'))
    # Never pass service credentials to a public twin-finder process.
    for name in list(env):
        if 'KEY' in name.upper() or 'TOKEN' in name.upper() or 'SECRET' in name.upper():
            env.pop(name)
    arguments = [settings().get('twins_python', sys.executable), str(dependency()/'f3twins.py'), command]
    xbox = None
    if command == 'bridge':
        xbox = bridge_identity()
        arguments.append(xbox['root'])
    subprocess.run(arguments, env=env, cwd=root, check=True)
    if identity() != current:
        raise ValueError('inputs changed during twins build; cache not promoted')
    if command != 'bridge':
        life.save(root/('build-inputs.json' if command == 'build' else 'inputs.json'), current)
    else:
        if bridge_identity() != xbox:
            raise ValueError('Xbox reference changed during bridge build')
        life.save(root/'bridge-inputs.json', dict(inputs=current, xbox=xbox, sha256=digest(root/'x360_bridge.json')))
    if command == 'all':
        life.save(root/'result-hash.json', dict(sha256=digest(root/'twins.json')))
    return dict(cache=str(root), operation=command)


def lookup(address):
    from matchflow import digest
    if not re.fullmatch(r'0x[0-9a-f]{8}', address):
        raise ValueError('canonical address required')
    root = cache_root()
    if not (root/'inputs.json').is_file():
        return dict(available=False, reason='twins cache not built; ask parent to configure/build it')
    if life.load(root/'inputs.json') != identity() or life.load(root/'result-hash.json')['sha256'] != digest(root/'twins.json'):
        raise ValueError('stale or changed twin cache; rebuild before using it')
    candidates = life.load(root/'twins.json').get(address, [])[:3]
    if (root/'bridge-inputs.json').is_file():
        manifest = life.load(root/'bridge-inputs.json')
        if (manifest['inputs'] != identity() or manifest.get('xbox') != bridge_identity() or
                manifest['sha256'] != digest(root/'x360_bridge.json')):
            raise ValueError('stale/changed optional bridge; rebuild it')
        bridge = life.load(root/'x360_bridge.json')
        candidates = [dict(c, x360=bridge.get(c['fnv'])) for c in candidates]
    return dict(available=True, candidates=candidates, confidence_is_not_proof=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'build', 'all', 'bridge', 'lookup'])
    parser.add_argument('address', nargs='?')
    args = parser.parse_args()
    try:
        result = install() if args.command == 'install' else lookup(args.address) if args.command == 'lookup' else build(args.command)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('twins: '+str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
