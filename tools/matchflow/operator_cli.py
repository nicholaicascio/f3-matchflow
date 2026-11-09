"""Orchestrator conveniences. No claims, compilation, launches or service mutations."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

import batch
from config import workspace_root
import lifecycle as life
import review
from security import no_links, private_root
from transport import request, ServiceError


def write_new(path, value):
    """Runtime output stays in the workspace; never replace historical evidence."""
    root = private_root(workspace_root())
    path = Path(path)
    if not path.is_absolute():
        path = root/'work/matchflow'/path
    path = no_links(path).resolve()
    if root not in path.parents:
        raise ValueError('output must be inside the external workspace')
    text = json.dumps(value, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(text)
    return dict(path=str(path))


def status_snapshot(output, api=request):
    # Check destination before consuming one-shot notices or making network calls.
    root = private_root(workspace_root())
    path = Path(output)
    if not path.is_absolute():
        path = root/'work/matchflow'/path
    path = no_links(path).resolve()
    start_path = path.with_name(path.name+'.start.json')
    if root not in path.parents or path.exists() or no_links(start_path).exists():
        raise ValueError('new output inside the external workspace required')
    started = life.stamp()
    start = api('start_here', {})
    start_checked_at = life.stamp()
    write_new(start_path, dict(checked_at=start_checked_at, response=start))
    status = api('status', {})
    if not isinstance(status, dict) or not isinstance(status.get('open_claims'), list):
        raise ValueError('invalid service status; no snapshot written')
    notices = (start.get('notices') or []) + (status.get('notices') or [])
    result = write_new(path, dict(checked_at=started, status=status,
                                 start_here=start, start_checked_at=start_checked_at))
    return dict(result, checked_at=started, notices=notices,
                open_claims=len(status['open_claims']))


def annotation_template(screen_path):
    evidence = life.load(Path(screen_path)/'review.json')
    return dict(targets=[dict(address=row['metadata']['address'], reason='', review=dict(
        history_reviewed=False, history_sources=[], prior_verdicts=[], verdict_history='unknown',
        report_scope='unknown', report_evidence=None, prior_unfit=False,
        next_experiment=None, integration_plan=None)) for row in evidence['shortlist']])


def reviewed_selection(screen_path, annotations):
    """Join explicit annotations to saved function_info, never re-date service facts."""
    folder = Path(screen_path)
    shortlist = life.load(folder/'review.json')['shortlist']
    if not isinstance(annotations, dict) or set(annotations) != {'targets'}:
        raise ValueError('annotations must contain only targets')
    if not isinstance(annotations['targets'], list) or not 1 <= len(annotations['targets']) <= 50:
        raise ValueError('review 1..50 exact targets')
    saved = {}
    paths = sorted(folder.glob('info-*.json'))
    if not 1 <= len(paths) <= 50:
        raise ValueError('screen must contain 1..50 function_info receipts')
    for path in paths:
        record = life.load(path)
        address = record['response']['address']
        if address in saved:
            raise ValueError('duplicate function_info evidence')
        if record['request'] != dict(address=address):
            raise ValueError('function_info request/response identity mismatch')
        saved[address] = (record, path)
    targets, stamps, provenance = [], [], []
    for annotation in annotations['targets']:
        if not isinstance(annotation, dict) or set(annotation) != {'address', 'reason', 'review'}:
            raise ValueError('each annotation needs only address, reason and explicit review facts')
        address = annotation['address']
        if not isinstance(address, str) or address not in saved:
            raise ValueError('target needs saved function_info evidence')
        record, path = saved[address]
        info = record['response']
        if not any(row['metadata'] == info for row in shortlist):
            raise ValueError('target must belong to the saved shortlist with unchanged metadata')
        batch.fresh(record['checked_at'])
        stamps.append(record['checked_at'])
        target = dict(info, reason=annotation['reason'], review=annotation['review'])
        # Do not infer substantive review, report scope or a hypothesis from counts.
        target['review'] = review.normalized(target)
        targets.append(target)
        from matchflow import digest
        provenance.append(dict(address=address, path=str(path.resolve()), sha256=digest(path),
                               checked_at=record['checked_at']))
    result = dict(checked_at=min(stamps, key=life.timestamp), targets=targets,
                  evidence=provenance)
    batch.validate_selection(result)
    return result


def read_database(path):
    """No schema initialization, migration, journal-mode changes or missing-file creation."""
    path = no_links(path).resolve()
    if not path.is_file():
        raise ValueError('database does not exist')
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def inspect_ledger(path, kind, address=None, claim=None, limit=20, offset=0):
    if kind not in ('claims', 'checks', 'verdicts'):
        raise ValueError('inspect claims, checks or verdicts')
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('limit 1..100 and nonnegative offset required')
    if address is not None and not re.fullmatch(r'0x[0-9a-f]{8}', address):
        raise ValueError('canonical address required')
    if claim is not None and (not isinstance(claim, str) or not claim.strip() or len(claim) > 160):
        raise ValueError('short claim ID required')
    if kind == 'verdicts' and claim:
        raise ValueError('verdicts filter by address, not claim')
    queries = {
        'claims': ('attempts a', 'a.claim_id,a.address,a.state,a.model,a.variant,a.created,a.deadline,a.final_check_id,a.submission_id', 'a.created DESC,a.claim_id'),
        'checks': ('checks c JOIN attempts a ON a.claim_id=c.claim_id',
                   'c.id,c.claim_id,a.address,c.stage,c.created,c.valid,c.exit_code,c.result_json', 'c.id DESC'),
        'verdicts': ('server_results a', 'a.submission_id,a.address,a.verdict,a.detail,a.observed', 'a.observed DESC,a.submission_id'),
    }
    table, columns, order = queries[kind]
    where, values = [], []
    for column, value in (('a.address', address), ('a.claim_id', claim)):
        if value is not None:
            where.append(column+'=?'); values.append(value)
    sql = 'SELECT '+columns+' FROM '+table
    if where:
        sql += ' WHERE '+' AND '.join(where)
    with closing(read_database(path)) as db:
        rows = [dict(row) for row in db.execute(sql+' ORDER BY '+order+' LIMIT ? OFFSET ?',
                                               [*values, limit, offset])]
    for row in rows:
        if 'result_json' in row:
            result = json.loads(row.pop('result_json'))
            row['metrics'] = {key: result.get(key) for key in ('status', 'ndiff_insn', 'extent', 'diagnostic')}
    return dict(kind=kind, rows=rows, limit=limit, offset=offset)


def inspect_lifecycle(folder, worker=None, limit=20, offset=0):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('limit 1..100 and nonnegative offset required')
    if worker is not None and not re.fullmatch(r'worker[0-9]{2,3}', worker):
        raise ValueError('exact worker ID required')
    fields = ('worker', 'address', 'claim_id', 'session_id', 'state', 'setup_step', 'outcome',
              'closure_requested', 'service_ack_path', 'dispatched_utc')
    with closing(read_database(Path(folder)/'lifecycle.sqlite3')) as db:
        sql = 'SELECT id,payload FROM workers'
        values = []
        if worker:
            sql += ' WHERE id=?'; values.append(worker)
        rows = []
        for row in db.execute(sql+' ORDER BY id LIMIT ? OFFSET ?', [*values, limit, offset]):
            payload = json.loads(row['payload'])
            rows.append(dict(worker=row['id'], **{k: payload[k] for k in fields if k != 'worker' and k in payload}))
    return dict(kind='lifecycle', rows=rows, limit=limit, offset=offset,
                note='Read-only local state, not closure acknowledgement or server verification.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('status'); p.add_argument('--output', required=True)
    p = sub.add_parser('review-template'); p.add_argument('--screen', required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('selection'); p.add_argument('--screen', required=True)
    p.add_argument('--annotations', required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('inspect'); p.add_argument('kind', choices=['claims', 'checks', 'verdicts', 'lifecycle'])
    p.add_argument('--address'); p.add_argument('--claim'); p.add_argument('--batch'); p.add_argument('--worker')
    p.add_argument('--limit', type=int, default=20); p.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == 'status':
            result = status_snapshot(args.output)
        elif args.command == 'review-template':
            result = write_new(args.output, annotation_template(args.screen))
        elif args.command == 'selection':
            result = write_new(args.output, reviewed_selection(args.screen, life.load(args.annotations)))
        elif args.kind == 'lifecycle':
            if not args.batch or args.address or args.claim:
                raise ValueError('lifecycle requires --batch; use --worker, not address/claim')
            result = inspect_lifecycle(batch.batch_path(args.batch), args.worker, args.limit, args.offset)
        else:
            if args.batch or args.worker:
                raise ValueError('--batch/--worker apply only to lifecycle')
            result = inspect_ledger(workspace_root()/'work/matchflow/ledger.sqlite3', args.kind,
                                    args.address, args.claim, args.limit, args.offset)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, ServiceError) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
