"""WSL-side trusted launcher. Does not change the official checker or its transport."""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-json', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--mode', choices=['check', 'preflight'], default='check')
    args, remaining = parser.parse_known_args()
    config = json.loads(args.runtime_json)
    checker = Path(config['checker'])
    required = [checker, checker.with_name('msvc9.py'),
                Path(config['compiler']) / 'VC/bin/cl.exe',
                *[Path(config[k]) for k in ('image', 'symbols', 'index')]]
    missing = [str(p) for p in required if not p.is_file()]
    sdk = Path(config['compiler']) / 'SDK/Include'
    if not any((sdk/name).is_file() for name in ('windows.h', 'Windows.h')):
        missing.append(str(sdk/'windows.h'))
    if missing:
        print(json.dumps(dict(status='setup_error', missing=missing)))
        return 2
    os.environ.update(F3_MSVC_ROOT=config['compiler'], F3_MSVC_PREFIX=config['wine_prefix'],
                      F3CHECK_INDEX=config['index'], F3CHECK_SYMBOLS=config['symbols'],
                      F3CHECK_IMAGE=config['image'], F3_SITE='https://f3decomp.com')
    sys.path.insert(0, str(checker.parent))
    if len(remaining) == 2 and remaining[0] == '--target-json':
        from target import inspect
        request = json.loads(remaining[1])
        print(json.dumps(inspect(config['image'], **request)))
        return 0
    if args.mode == 'preflight':
        import subprocess
        compiler = subprocess.run([sys.executable, str(checker.with_name('msvc9.py'))],
                                  capture_output=True, text=True, timeout=90)
        image = subprocess.run([sys.executable, str(checker), '--check-image'],
                               capture_output=True, text=True, timeout=90)
        compiler_ok = '14.00.50727.762' in compiler.stdout + compiler.stderr
        image_ok = image.returncode == 0 and 'IMAGE OK' in image.stdout
        print(json.dumps(dict(compiler_ok=compiler_ok, image_ok=image_ok,
                             image_output=image.stdout[:2000],
                             status='ready' if compiler_ok and image_ok else 'setup_error')))
        return 0 if compiler_ok and image_ok else 2
    # Kept inside the trusted launcher, not passed in argv or sent to the worker.
    os.environ['F3_API_KEY'] = Path(args.key_file).read_text().strip()
    if len(remaining) == 2 and remaining[0] == '--reference':
        name = remaining[1]
        if name not in ('compiler_tips', 'large_functions'):
            raise ValueError('unknown fixed reference')
        print((Path(config['index']).parent/(name+'.md')).read_text())
        return 0
    if remaining == ['--probe-notes']:
        try:
            import f3notes
            response = f3notes._api('status', {})
            print(json.dumps(dict(ready=isinstance(response, dict) and bool(response.get('tier')),
                                  scope='official f3notes read-only transport, no upload')))
        except Exception as exc:
            print(json.dumps(dict(ready=False, http_status=getattr(exc, 'code', None),
                                  error='official work-note transport unavailable; no workaround applied')))
        return 0
    sys.argv = [str(checker), *remaining]
    runpy.run_path(str(checker), run_name='__main__')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError):
        print('checker launcher setup failed; inspect configured paths/credential access', file=sys.stderr)
        sys.exit(2)
