"""Download exact-claim context packs; reject archive escapes and links."""
import io
import tarfile
from pathlib import Path

import lifecycle as life
from security import scoped, no_links
from transport import fetch


def unpack(data, destination):
    destination = no_links(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        checked = []
        total = 0
        for count, entry in enumerate(archive, 1):
            total += entry.size
            if count > 10000 or total > 128 * 1024 * 1024:
                raise ValueError('context pack is too large')
            name = entry.name
            while name.startswith('./'):
                name = name[2:]
            if not name and entry.isdir():
                continue
            target = scoped(destination, name.rstrip('/'))
            if not (entry.isfile() or entry.isdir()) or entry.sparse is not None:
                raise ValueError('pack links and special files are forbidden')
            checked.append((entry, target))
        # Validate the complete archive before writing even its first member.
        for entry, target in checked:
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    output.write(archive.extractfile(entry).read())
    if not (destination / 'pack.json').is_file():
        raise ValueError('pack.json must be at archive root; preserve archive and reconcile')


def acquire(workspace, claim, packet, downloader=fetch):
    destination = Path(workspace) / 'packs' / claim
    unpack(downloader(packet['pack_url']), destination)
    return destination
