"""Explicit private workspace configuration, independent of the code checkout."""
import json
import os
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


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
