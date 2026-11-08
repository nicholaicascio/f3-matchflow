"""Explicit service gateway for dispatched assignments. No retries or bulk cleanup."""
import argparse
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import urllib.error
import urllib.request

import lifecycle as life
import matchflow as ledger


class ServiceError(ValueError):
    def __init__(self, code, retry_after):
        self.code = code
        super().__init__('service HTTP %s; Retry-After=%s; no automatic retry' % (code, retry_after))


def request(tool, body):
    # Deliberately not configurable to an arbitrary host: never forward the key.
    key = (Path(os.environ['LOCALAPPDATA']) / 'f3decomp' / 'api-key').read_text().strip()
    req = urllib.request.Request('https://f3decomp.com/api/v1/' + tool,
                                 data=json.dumps(body).encode('utf-8'),
                                 headers={'Authorization': 'Bearer ' + key,
                                          'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Error bodies can contain submitted source. Keep them out of chat/logs.
        raise ServiceError(exc.code, exc.headers.get('Retry-After', 'unspecified')) from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError('service transport failed; outcome ambiguous, reconcile without retry') from None


def owned(status, claim, address):
    matches = [c for c in status['open_claims'] if c['claim_id'] == claim]
    if len(matches) != 1 or matches[0].get('this_key') is not True or matches[0]['address'] != address:
        raise ValueError('fresh status does not confirm this exact claim/address belongs to this key')
    return matches[0]


def claim(folder, worker, api=request):
    folder = Path(folder)
    assignment = life.load(folder / worker / 'assignment.json')
    state = life.require_experiment(folder, worker)
    body = assignment['claim_body']
    if (body.get('address') != state['address'] or body.get('model') != state['model'].split('#')[0] or
            body.get('cowork') is not False or body.get('context_mode') != 'resume' or 'band' in body):
        raise ValueError('claim body differs from dispatched address/model/claim policy')
    info = api('function_info', {'address': state['address']})
    if info.get('state') != 'open' or (info.get('regions') or {}).get('co_workers', 0):
        life.blocked(folder, worker, 'fixed target unavailable; no replacement or claim made')
        return dict(outcome='blocked', claim_id=None, address=state['address'])
    # Spend the one-attempt reservation BEFORE the network mutation. A crash or
    # lost response must not allow a duplicate claim on restart.
    life.reserve_claim(folder, worker)
    response = api('claim', body)
    life.save(folder / worker / 'claim-response.json', response)
    cid = response.get('claim_id')
    if not cid:
        raise ValueError('claim response has no claim_id; reconcile saved response, never retry')
    status = api('status', {})
    life.save(folder / worker / 'claim-status.json', dict(checked_at=life.stamp(), status=status))
    lease = owned(status, cid, state['address'])
    state = life.claimed(folder, worker, cid, lease['address'], lease['expires'])
    life.save(folder / worker / 'ownership.json', state)
    return dict(claim_id=cid, address=state['address'], deadline_utc=state['deadline_utc'],
                response_path=str(folder / worker / 'claim-response.json'))


def reconcile(folder, worker, claim_id=None, api=request):
    """Read-only remote reconciliation. Never retry a mutation or infer a receipt."""
    folder = Path(folder)
    state = life.snapshot(folder, worker)
    status = api('status', {})
    life.save(folder / worker / 'reconcile-status.json', dict(checked_at=life.stamp(), status=status))
    if state['state'] == 'claim_pending':
        path = folder / worker / 'claim-response.json'
        recorded = life.load(path).get('claim_id') if path.exists() else None
        cid = recorded or claim_id
        if not cid or (recorded and claim_id and recorded != claim_id):
            raise ValueError('supply the exact service claim ID; do not guess from an address')
        lease = owned(status, cid, state['address'])
        state = life.claimed(folder, worker, cid, lease['address'], lease['expires'])
        life.save(folder / worker / 'ownership.json', state)
        return dict(state='claimed', claim_id=cid, note='original deadlines retained; stale watchdog means closure only')
    if state['state'] == 'close_pending':
        ack_path = folder / worker / 'service-ack.json'
        if not ack_path.exists():
            raise ValueError('no acknowledgement saved; preserve best source and reconcile with operator, no automatic retry')
        ack = life.load(ack_path)
        if ack['claim_id'] != state['claim_id'] or ack['address'] != state['address']:
            raise ValueError('saved acknowledgement differs from ownership')
        if any(c['claim_id'] == state['claim_id'] for c in status['open_claims']):
            raise ValueError('claim still open; do not treat acknowledgement/absence assumptions as closure')
        result = acknowledgement(ack['response'], ack['operation'], bool(ack['source_sha256']))
        if not result['lease_closed']:
            raise ValueError('saved response does not acknowledge closure')
        outcome = ('flag_review' if ack['operation'] == 'flag-proof' else
                   'submitted' if ack['operation'] == 'submit' else 'given_up')
        life.closed(folder, worker, outcome, ack_path, result['source_retention'])
        return dict(state='closed', outcome=outcome, assessment=result,
                    note='remote receipt recovered; inspect/update local ledger with record before report generation')
    return dict(state=state['state'], claim_id=state['claim_id'],
                note='no mutation performed; remote absence alone is not a closure receipt')


def acknowledgement(response, operation, has_source=False):
    if operation == 'flag-proof':
        return dict(lease_closed=response.get('released') is True and bool(response.get('flag_id')),
                    source_retention='submitted', verification='flag_review', flag_id=response.get('flag_id'))
    if operation == 'submit':
        sid = response.get('submission_id')
        return dict(lease_closed=bool(sid), source_retention='submitted' if sid else 'unknown',
                    submission_id=sid, verification='pending' if sid else 'unknown')
    retained = response.get('best_code')
    kept = isinstance(retained, str) and retained.startswith('best_code kept:')
    return dict(lease_closed=response.get('released') is True,
                source_retention=('retained' if kept else 'failed') if has_source else 'not_applicable',
                verification='not_submitted')


def submit_artifact(db, cid, proof=False):
    row = ledger.attempt(db, cid)
    ledger.ensure_active(row)
    if row['state'] != ('proof_ready' if proof else 'submit_ready'):
        raise ValueError('submit_ready required; diagnostic matches need the flag-proof route')
    final = db.execute('SELECT * FROM checks WHERE id=? AND claim_id=?',
                       (row['final_check_id'], cid)).fetchone()
    if not final or not final['valid'] or final['stage'] != 'final':
        raise ValueError('missing valid final checker record')
    result = json.loads(final['result_json'])
    pack = life.load(Path(row['pack_path']) / 'pack.json')
    normal_flags = any(ledger.same_flags(json.loads(final['flags_json']), f) for f in ledger.accepted_sets(pack))
    if (result.get('status') != 'match' or normal_flags == proof or
            ledger.digest(row['artifact_path']) != row['artifact_hash'] or
            row['artifact_hash'] != final['source_hash'] or
            ledger.tree_digest(row['pack_path']) != final['pack_hash']):
        raise ValueError('final source, pack, flags or MATCH evidence changed')
    return Path(row['artifact_path'])


def close(folder, worker, operation, source=None, notes=None, no_source_reason=None,
          db_path=ledger.DEFAULT_DB, api=request):
    folder = Path(folder)
    state = life.snapshot(folder, worker)
    cid = state['claim_id']
    owned(api('status', {}), cid, state['address'])
    assignment = life.load(folder / worker / 'assignment.json')
    body = dict(claim_id=cid, model=assignment['claim_body']['model'])
    if operation == 'giveup':
        notes = notes or {}
        required = ('reason', 'category', 'tried', 'still_differs', 'suspected_cause')
        if not all(isinstance(notes.get(k), str) and notes[k].strip() for k in required):
            raise ValueError('giveup needs reason/category/tried/still_differs/suspected_cause')
        if notes['category'] not in {'pack_problem', 'compiler_mismatch', 'register_allocation',
                                     'instruction_order', 'stack_layout', 'eh', 'unknown_type', 'out_of_time', 'other'}:
            raise ValueError('unsupported giveup category')
        body.update({k: notes[k] for k in required})
        if source:
            source = Path(source)
            # Freeze exact bytes locally; only decoded C++ goes over the service.
            data = source.read_bytes()
            text = data.decode('utf-8-sig')
            if not text.strip():
                raise ValueError('empty best source')
            body['best_code'] = text
        elif not no_source_reason:
            raise ValueError('giveup requires best source or an explicit no-source reason')
    elif operation in ('submit', 'flag-proof'):
        with closing(ledger.connect(db_path)) as db:
            source = submit_artifact(db, cid, proof=operation == 'flag-proof')
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != ledger.attempt(db, cid)['artifact_hash']:
                raise ValueError('artifact changed while freezing submission')
            body['source'] = data.decode('utf-8-sig')
            if operation == 'flag-proof':
                row = ledger.attempt(db, cid)
                final = db.execute('SELECT * FROM checks WHERE id=?', (row['final_check_id'],)).fetchone()
                proof_flags = json.loads(final['flags_json'])
                proof_result = json.loads(final['result_json'])
                if not notes or not notes.get('note', '').strip():
                    raise ValueError('flag-proof requires a concise explanation of the flags discrepancy')
    else:
        raise ValueError('only giveup, submit or flag-proof supported')
    if source:
        frozen = folder / worker / 'closure-source.cpp'
        if frozen.exists():
            if frozen.read_bytes() != data:
                raise ValueError('refusing to overwrite a different frozen closure source')
        else:
            with frozen.open('xb') as output:
                output.write(data)
    # Reserve once, after local validation/freezing but before network mutation.
    # Failed transport leaves close_pending for read-only reconciliation.
    life.begin_close(folder, worker)
    checkpoint_id = None
    if operation == 'flag-proof':
        checkpoint = dict(claim_id=cid, request_id='flag-proof-' + life.uuid.uuid4().hex,
                          source=body['source'], phase='attempt', change='freeze exact alternate-flags proof',
                          why=notes['note'], observed='Final official checker returned MATCH under diagnostic flags.',
                          expect='Server recompilation reproduces the exact match.',
                          result=dict(compile='ok', metric='diffs', value=0, checker=proof_result.get('checker')))
        if isinstance(proof_result.get('regions'), str):
            checkpoint['region_summary'] = proof_result['regions']
        life.save(folder / worker / 'proof-checkpoint-request.json', checkpoint)
        saved = api('checkpoint', checkpoint)
        life.save(folder / worker / 'proof-checkpoint-response.json', saved)
        checkpoint_id = saved.get('checkpoint_id')
        if not checkpoint_id:
            raise ValueError('proof checkpoint not acknowledged; preserve response and reconcile')
        response = api('flag', dict(claim_id=cid, scope='function', category='pack_problem', note=notes['note'],
                                   compile_flags=proof_flags, checkpoint_id=checkpoint_id, keep_claim=False))
    else:
        response = api(operation, body)
    ack_path = folder / worker / 'service-ack.json'
    assessment = acknowledgement(response, operation, bool(source))
    envelope = dict(claim_id=cid, address=state['address'], operation=operation,
                    observed_utc=life.stamp(), response=response, assessment=assessment,
                    source_sha256=ledger.digest(frozen) if source else None,
                    no_source_reason=no_source_reason, checkpoint_id=checkpoint_id)
    life.save(ack_path, envelope)
    if not assessment['lease_closed']:
        raise ValueError('service did not acknowledge closure; reconcile saved response')
    outcome = 'flag_review' if operation == 'flag-proof' else 'submitted' if operation == 'submit' else 'given_up'
    life.closed(folder, worker, outcome, ack_path, assessment['source_retention'])
    # Remote acknowledgement remains durable even if local recording fails.
    with closing(ledger.connect(db_path)) as db:
        if db.execute('SELECT 1 FROM attempts WHERE claim_id=?', (cid,)).fetchone():
            args = argparse.Namespace(claim=cid, kind=outcome,
                                      identifier=response['submission_id'] if operation == 'submit' else
                                      response['flag_id'] if operation == 'flag-proof' else str(ack_path),
                                      checkpoint=checkpoint_id, note='source retention: ' + assessment['source_retention'])
            ledger.record(db, args)
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ledger.DEFAULT_DB))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('claim', 'giveup', 'submit', 'reconcile', 'flag-proof'):
        p = sub.add_parser(name); p.add_argument('folder'); p.add_argument('worker')
        if name == 'giveup':
            p.add_argument('--source'); p.add_argument('--notes', required=True)
            p.add_argument('--no-source-reason')
        if name == 'reconcile':
            p.add_argument('--claim-id')
        if name == 'flag-proof':
            p.add_argument('--notes', required=True)
    args = parser.parse_args()
    try:
        result = (reconcile(args.folder, args.worker, args.claim_id) if args.command == 'reconcile' else
                  claim(args.folder, args.worker) if args.command == 'claim' else
                  close(args.folder, args.worker, args.command, source=getattr(args, 'source', None),
                        notes=life.load(args.notes) if hasattr(args, 'notes') else None,
                        no_source_reason=getattr(args, 'no_source_reason', None), db_path=args.db))
        # The raw service response can contain source/pack context: print only assessment.
        print(json.dumps(result.get('assessment', result), indent=2))
    except ServiceError as exc:
        if exc.code in (429, 503):
            folder = Path(args.folder)
            (folder / 'STOP').touch()
            life.save(folder / 'SERVICE_COOLDOWN.json', dict(observed_utc=life.stamp(), message=str(exc)))
        parser.exit(2, str(exc) + '\n')
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
