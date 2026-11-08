"""Explicit private workspace configuration, independent of the code checkout."""
import json
import os
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def settings(repository=REPOSITORY):
    local = Path(repository) / 'matchflow.local.json'
    return json.loads(local.read_text(encoding='utf-8-sig')) if local.is_file() else {}


def key_file():
    value = settings().get('key_file')
    base = os.environ.get('LOCALAPPDATA')
    path = Path(value).expanduser() if value else Path(base) / 'f3decomp/api-key' if base else Path.home() / '.f3kit/api-key'
    if not path.is_absolute():
        raise ValueError('credential path must be absolute and external')
    resolved = path.resolve()
    ws = workspace_root()
    if resolved == REPOSITORY or REPOSITORY in resolved.parents or resolved == ws or ws in resolved.parents:
        raise ValueError('credentials must be outside both repository and runtime workspace')
    return path


def runtime():
    """Non-secret, explicit paths. Never source a mutable workspace shell script."""
    ws = workspace_root()
    defaults = dict(distro='Ubuntu', user='root', checker='/root/.f3kit/f3check/f3check.py',
                    compiler='/root/.f3kit/vs2005', wine_prefix='/root/.f3kit/wine-vs2005',
                    index='/root/.f3kit/function_index.json',
                    symbols='/root/.f3kit/f3decomp_symbols.json',
                    image=str(ws / 'Fallout 3' / 'Fallout3.exe'))
    defaults.update(settings().get('toolchain', {}))
    return defaults


def workspace_root(repository=REPOSITORY):
    value = os.environ.get('F3_MATCHFLOW_WORKSPACE')
    local = repository / 'matchflow.local.json'
    if not value and local.is_file():
        value = json.loads(local.read_text(encoding='utf-8-sig'))['workspace']
    if not value:
        raise ValueError('Set F3_MATCHFLOW_WORKSPACE or ignored matchflow.local.json with an absolute workspace path')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('workspace must be an absolute path')
    path = path.resolve()
    repo = repository.resolve()
    if path == repo or repo in path.parents:
        raise ValueError('private workspace/artifacts must be outside the code repository')
    return path
