"""Filesystem capability checks. Workers never receive an arbitrary-path API."""
import os
from pathlib import Path, PureWindowsPath
import stat


def no_links(path):
    path = Path(os.path.abspath(path))
    for item in [path, *path.parents]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('symlinks/junctions/reparse points are not allowed')
    return path


def scoped(root, relative, exists=False):
    if not isinstance(relative, str) or not relative or len(relative) > 240:
        raise ValueError('short nonempty relative path required')
    windows = PureWindowsPath(relative)
    parts = relative.replace('\\', '/').split('/')
    if windows.drive or windows.root or relative.startswith('/'):
        raise ValueError('absolute/drive/UNC paths are not allowed')
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith((' ', '.')) or
                any(c in part for c in ':\x00<>|?*') or any(ord(c) < 32 for c in part) or
                part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                    *('COM%d' % i for i in range(1, 10)), *('LPT%d' % i for i in range(1, 10))}):
            raise ValueError('unsafe path component')
    base = no_links(root).resolve()
    target = no_links(base.joinpath(*parts))
    if base not in target.resolve().parents:
        raise ValueError('path escapes capability root')
    if exists and not target.is_file():
        raise ValueError('requested file does not exist')
    return target


def private_root(path):
    from config import REPOSITORY
    target = no_links(path).resolve()
    repo = REPOSITORY.resolve()
    if target == repo or repo in target.parents or target in repo.parents:
        raise ValueError('runtime roots must be external to the repository')
    return target
