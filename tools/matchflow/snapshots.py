"""Claim-scoped, byte-exact checked sources. Host-owned; never worker-writable."""
import hashlib
from pathlib import Path
import re

from security import no_links


def available(row):
    if 'source_snapshot_path' not in row.keys() or not row['source_snapshot_path']:
        return False
    try:
        return hashlib.sha256(no_links(row['source_snapshot_path']).read_bytes()).hexdigest() == row['source_hash']
    except (ValueError, OSError):
        return False


def freeze(root, claim, data):
    if not re.fullmatch(r'clm_[A-Za-z0-9]+', claim):
        raise ValueError('canonical claim ID required for source snapshot')
    digest = hashlib.sha256(data).hexdigest()
    folder = no_links(Path(root)/'sources'/claim)
    folder.mkdir(parents=True, exist_ok=True)
    path = no_links(folder/(digest+'.cpp'))
    try:
        with path.open('xb') as output:
            output.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError('existing source snapshot changed; preserve evidence and reconcile')
    return path


def checked_source(db, claim, check_id):
    if type(check_id) is not int or check_id <= 0:
        raise ValueError('positive own-claim check_id required')
    row = db.execute('SELECT * FROM checks WHERE id=? AND claim_id=?', (check_id, claim)).fetchone()
    if not row or not row['valid'] or not row['source_snapshot_path']:
        raise ValueError('valid own-claim check with a durable source snapshot required')
    path = no_links(row['source_snapshot_path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != row['source_hash']:
        raise ValueError('checked source snapshot hash changed')
    return path
