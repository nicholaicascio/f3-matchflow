"""Build the exact official checker invocation without credentials in argv."""
import json
import re
from pathlib import Path

from config import REPOSITORY, key_file, runtime


def wsl_path(path):
    value = str(path)
    match = re.match(r'^([A-Za-z]):[\\/](.*)$', value)
    return '/mnt/' + match[1].lower() + '/' + match[2].replace('\\', '/') if match else value


def command(arguments, mode='check', wall_seconds=None, watch_file=None):
    config = runtime()
    for field in ('image', 'index', 'symbols', 'checker', 'compiler', 'wine_prefix'):
        config[field] = wsl_path(config[field])
    prefix = ['--runtime-json', json.dumps(config, separators=(',', ':')),
              '--key-file', wsl_path(key_file()), '--mode', mode]
    # A bounded search needs cancellation inside WSL, not only a killed wsl.exe.
    # These are trusted runner options; the explicit -- keeps them out of checker argv.
    if wall_seconds is not None:
        prefix += ['--wall-seconds', str(int(wall_seconds))]
    if watch_file is not None:
        prefix += ['--watch-file', wsl_path(watch_file)]
    # WSL's plain -- still invokes the Linux user's shell and expands note text.
    # --exec bypasses it; check.sh only forwards literal argv through "$@".
    return ['wsl', '-d', config['distro'], '-u', config['user'], '--exec', 'bash',
            wsl_path(REPOSITORY / 'tools/matchflow/check.sh'), *prefix, '--', *arguments]
