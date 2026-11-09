"""WSL-side trusted launcher. Does not change the official checker or its transport."""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys


def parse_invocation(argv):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--runtime-json', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--mode', choices=['check', 'preflight'], default='check')
    parser.add_argument('--wall-seconds', type=int)
    parser.add_argument('--watch-file', help='host-created cancellation marker for a bounded search')
    if '--' not in argv:
        parser.error('trusted runner options require an explicit -- boundary')
    boundary = argv.index('--')
    # Never parse a checker argument or a note value as a privileged runner option.
    args = parser.parse_args(argv[:boundary])
    return args, argv[boundary+1:]


def bounded(args):
    """Make a bounded search kill its own process group on timeout or STOP.

    subprocess timeout on the Windows side only kills wsl.exe.  This runs inside
    WSL and terminates the checker, its compiler descendants and the permuter
    threads together, so no compilation continues after the bound is reached.
    """
    if args.wall_seconds is None and not args.watch_file:
        return
    import signal
    import threading
    try:
        os.setpgrp()
    except OSError:
        pass

    def terminate():
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except OSError:
            # The host treats this marker as an unconfirmed process-tree stop.
            try:
                if args.watch_file:
                    with open(args.watch_file + '.unconfirmed', 'w', encoding='utf-8') as output:
                        output.write('process-group termination failed')
            except OSError:
                pass
        os._exit(124)

    if args.wall_seconds is not None:
        if args.wall_seconds <= 0:
            raise ValueError('wall seconds must be positive')
        signal.signal(signal.SIGALRM, lambda *_: terminate())
        signal.alarm(args.wall_seconds)
    if args.watch_file:
        def watch():
            while True:
                if os.path.exists(args.watch_file):
                    terminate()
                import time
                time.sleep(0.5)
        threading.Thread(target=watch, daemon=True).start()


def main():
    args, remaining = parse_invocation(sys.argv[1:])
    bounded(args)
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
