"""Build the exact official checker invocation without credentials in argv."""
import json
import re
from pathlib import Path

from config import REPOSITORY, key_file, runtime


def wsl_path(path):
    value = str(path)
    match = re.match(r'^([A-Za-z]):[\\/](.*)$', value)
    return '/mnt/' + match[1].lower() + '/' + match[2].replace('\\', '/') if match else value


def command(arguments, mode='check'):
    config = runtime()
    for field in ('image', 'index', 'symbols', 'checker', 'compiler', 'wine_prefix'):
        config[field] = wsl_path(config[field])
    return ['wsl', '-d', config['distro'], '-u', config['user'], '--', 'bash',
            wsl_path(REPOSITORY / 'tools/matchflow/check.sh'),
            '--runtime-json', json.dumps(config, separators=(',', ':')),
            '--key-file', wsl_path(key_file()), '--mode', mode, '--', *arguments]
