"""Bounded read-only service screening. Produces review material, never assignments."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from config import workspace_root
from function_info import co_workers
import lifecycle as life
from security import private_root
from transport import request, ServiceError


def local_history(path, address):
    """Only shortlisted addresses, read-only SQL; never walk packs or source trees."""
    if not Path(path).is_file():
        return dict(available=False, attempts=[], verdicts=[])
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            attempts = [dict(row) for row in db.execute('SELECT claim_id,state,created FROM attempts WHERE address=?', (address,))]
            verdicts = [dict(row) for row in db.execute(
                'SELECT submission_id,verdict,detail,observed FROM server_results WHERE address=?', (address,))]
    except sqlite3.Error:
        return dict(available=False, attempts=[], verdicts=[], note='Local history unreadable; review manually, do not assume clean.')
    return dict(available=True, attempts=attempts, verdicts=verdicts,
                note='Local records only; absence is not proof of clean complete service history.')


def screen(output, count=8, max_size=1023, max_pages=2, order='give_ups',
           addresses=None, api=request):
    if type(count) is not int or not 1 <= count <= 50 or not 1 <= max_pages <= 10 or max_size <= 0:
        raise ValueError('count 1..50, pages 1..10 and positive max-size required')
    if order not in ('give_ups', 'size', 'returned'):
        raise ValueError('order must be give_ups, size or returned')
    if addresses is not None:
        if not isinstance(addresses, (list, tuple)) or not 1 <= len(addresses) <= 50:
            raise ValueError('addresses 1..50 required')
        if len(set(addresses)) != len(addresses):
            raise ValueError('targeted addresses must be unique')
        if not all(isinstance(a, str) and re.fullmatch(r'0x[0-9a-f]{8}', a) for a in addresses):
            raise ValueError('canonical 0x addresses required, not bands')
        addresses = list(addresses)
    output = private_root(output)
    if output.exists():
        raise ValueError('screen output already exists; preserve prior review evidence')
    output.mkdir(parents=True)
    # Save every actual response before continuing. 429/503 propagate without loops.
    def read(tool, body, name):
        try:
            result = api(tool, body)
        except ServiceError as exc:
            life.save(output/'service-error.json', dict(tool=tool, observed_utc=life.stamp(),
                      http_status=exc.code, kind=exc.kind, retry_after=exc.retry_after))
            raise
        life.save(output/name, dict(checked_at=life.stamp(), request=body, response=result))
        return result
    start = read('start_here', {}, 'start.json')
    status = read('status', {}, 'status-response.json')
    life.save(output/'status.json', dict(checked_at=life.stamp(), status=status))
    bands = read('list_bands', {}, 'bands.json')
    allowed = {b['band'] for b in bands.get('bands', []) if b.get('allowed') and b.get('open', 0)}
    held = {c['address'] for c in status.get('open_claims', [])}
    if addresses is None:
        candidates, cursor, seen_cursors = {}, None, set()
        for page in range(max_pages):
            body = dict(order=order, limit=500, include_held=False)
            if cursor:
                body['cursor'] = cursor
            listing = read('list_functions', body, 'page-%02d.json' % (page+1))
            for f in listing.get('functions', []):
                address = f.get('address')
                if (f.get('band') in allowed and type(f.get('size')) is int and f['size'] <= max_size and
                        f.get('open_problem_reports') == 0 and f.get('failed_verifications') == 0 and address not in held):
                    candidates[address] = f
            cursor = listing.get('next')
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        # Keep the screen independent of draft existence and sibling-report counts:
        # both are review evidence, never a selection preference or ranking score.
        ranked = sorted(candidates.values(), key=lambda f: (f.get('give_ups', 10**9),
                        f['size'], f['address']))
        selected = [candidate['address'] for candidate in ranked[:count]]
    else:
        # Exact-address review for a known shortlist (near-miss retries): the catalog
        # is not paged and no replacement target is chosen.
        selected = addresses
    shortlist = []
    # Only inspect the final bounded shortlist. A raced target is not replaced here.
    for i, address in enumerate(selected, 1):
        info = read('function_info', dict(address=address), 'info-%02d.json' % i)
        if (info.get('address') != address or info.get('state') != 'open' or
                co_workers(info) != 0 or info.get('open_problem_reports') != 0 or
                info.get('failed_verifications') != 0 or type(info.get('size')) is not int or
                info['size'] > max_size or address in held or
                (addresses is not None and info.get('band') not in allowed)):
            continue
        shortlist.append(dict(metadata=info, local_history=local_history(output.parent/'ledger.sqlite3', info['address']),
                              history_reviewed=False, verdict_history='unknown',
                              report_scope='unknown', report_evidence=None,
                              review_required=['Review substantive history and local verdicts',
                                               'Determine report scope: function, sibling-only, unit-wide or unknown',
                                               'A saved draft or sibling report is not a selection preference',
                                               'Write next_experiment only where the facts justify it']))
    result = dict(schema_version=1, checked_at=life.stamp(), shortlist=shortlist,
                  requested=(len(addresses) if addresses is not None else count),
                  complete=len(shortlist) == (len(addresses) if addresses is not None else count), review_required=True,
                  notices=(start.get('notices') or []) + (status.get('notices') or []),
                  note='Not a selection. No claims, hypothesis invention, game inspection or launches. '
                       'Annotate actual evidence and prepare a reviewed selection separately.')
    life.save(output/'review.json', result)
    return dict(output=str(output), count=len(shortlist), complete=result['complete'], review_required=True,
                notices=result['notices'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--count', type=int, default=8)
    parser.add_argument('--max-size', type=int, default=1023)
    parser.add_argument('--max-pages', type=int, default=2)
    parser.add_argument('--order', choices=('give_ups', 'size', 'returned'), default='give_ups',
                        help='catalog page order for the candidate pool; the shortlist itself still prefers least-tried functions')
    parser.add_argument('--address', action='append', default=None,
                        help='review this exact address (repeatable, 1..50); skips catalog paging, no replacement target')
    args = parser.parse_args()
    try:
        output = Path(args.output)
        if not output.is_absolute():
            output = workspace_root()/'work/matchflow'/output
        if workspace_root().resolve() not in output.resolve().parents:
            raise ValueError('screening artifacts must stay inside the external workspace')
        print(json.dumps(screen(output, args.count, args.max_size, args.max_pages,
                                args.order, args.address), indent=2))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
