"""Install the local OpenCode policy/MCP bridge without reading credentials."""
import argparse
import json
import os
from pathlib import Path

from config import REPOSITORY, workspace_root


def bridge(workspace, key_file):
    workspace, key_file = Path(workspace).resolve(), Path(key_file).resolve()
    if not (workspace / 'AGENTS.md').is_file():
        raise ValueError('external workspace AGENTS.md is missing')
    if not key_file.is_file():
        raise ValueError('private key file is missing; configure the service outside the repository first')
    if key_file == REPOSITORY or REPOSITORY in key_file.parents:
        raise ValueError('key file must be outside the repository')
    return {
        '$schema': 'https://opencode.ai/config.json',
        'instructions': [(workspace / 'AGENTS.md').as_posix(), 'https://f3decomp.com/agents.md'],
        'mcp': {'servers': {'f3decomp': {
            'type': 'remote', 'url': 'https://f3decomp.com/mcp', 'oauth': False,
            'headers': {'Authorization': 'Bearer {file:' + key_file.as_posix() + '}'},
        }}},
    }


def install(repository, workspace, key_file):
    repository = Path(repository)
    config = bridge(workspace, key_file)
    path = repository / 'opencode.jsonc'
    # Do not silently shadow a user's alternate project config.
    alternatives = [repository/'opencode.json', repository/'.opencode/opencode.json',
                    repository/'.opencode/opencode.jsonc']
    if any(p.exists() for p in alternatives):
        raise ValueError('existing OpenCode configuration found; merge the bridge manually')
    if path.exists():
        try:
            if json.loads(path.read_text(encoding='utf-8-sig')) == config:
                return path
        except ValueError:
            pass
        raise ValueError('existing opencode.jsonc differs; refusing to overwrite custom settings')
    with path.open('x', encoding='utf-8') as output:
        output.write(json.dumps(config, indent=2) + '\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path)
    args = parser.parse_args()
    try:
        key = args.key_file
        if key is None:
            base = os.environ.get('LOCALAPPDATA')
            if not base:
                raise ValueError('set --key-file to the existing external service credential file')
            key = Path(base)/'f3decomp'/'api-key'
        path = install(REPOSITORY, workspace_root(), key)
        print('Local OpenCode bridge ready: ' + str(path))
        print('Only file references were written; no credentials were read or copied. Check /mcps for connection status.')
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
