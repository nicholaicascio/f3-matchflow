"""Explicit service gateway for dispatched assignments. No retries or bulk cleanup."""
import argparse
from contextlib import closing
import json
import hashlib
import os
import re
from pathlib import Path
from transport import ServiceError, request

import lifecycle as life
import matchflow as ledger
from function_info import co_workers


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
    if (info.get('address') != state['address'] or info.get('state') != 'open' or
            co_workers(info)):
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


def reconcile(folder, worker, claim_id=None, api=request, db_path=ledger.DEFAULT_DB):
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
            if (folder / worker / 'submit-error.json').exists():
                return resume_rejected_submit(folder, worker, status)
            raise ValueError('no acknowledgement saved; preserve best source and reconcile with operator, no automatic retry')
        ack = life.load(ack_path)
        if ack['claim_id'] != state['claim_id'] or ack['address'] != state['address']:
            raise ValueError('saved acknowledgement differs from ownership')
        if any(c['claim_id'] == state['claim_id'] for c in status['open_claims']):
            raise ValueError('claim still open; do not treat acknowledgement/absence assumptions as closure')
        result = acknowledgement(ack['response'], ack['operation'], bool(ack['source_sha256']),
                                 claim_id=state['claim_id'], address=state['address'])
        if not result['lease_closed']:
            raise ValueError('saved response does not acknowledge closure')
        outcome = ('flag_review' if ack['operation'] == 'flag-proof' else
                   'submitted' if ack['operation'] == 'submit' else 'given_up')
        life.closed(folder, worker, outcome, ack_path, result['source_retention'],
                    closed_utc=ack['observed_utc'])
        if ack['operation'] == 'flag-proof':
            with closing(ledger.connect(db_path)) as db:
                record_closure(db, state['claim_id'], outcome, ack_path, ack, result)
        return dict(state='closed', outcome=outcome, assessment=result,
                    note=('saved flag proof reconciled and recorded without replay' if ack['operation'] == 'flag-proof' else
                          'remote receipt recovered; inspect/update local ledger with record before report generation'))
    return dict(state=state['state'], claim_id=state['claim_id'],
                note='no mutation performed; remote absence alone is not a closure receipt')


def acknowledgement(response, operation, has_source=False, *, claim_id=None, address=None):
    if not isinstance(response, dict):
        raise ValueError('service acknowledgement must be an object; preserve response and reconcile')
    if operation not in ('giveup', 'submit', 'flag-proof'):
        raise ValueError('unsupported service acknowledgement operation')
    if operation == 'flag-proof':
        legacy = response.get('released') is True and isinstance(response.get('flag_id'), str) and bool(response['flag_id'])
        if 'claim_released' in response:
            legacy = False
            flagged = response.get('flagged')
            released = (isinstance(claim_id, str) and bool(claim_id) and
                        response['claim_released'] == claim_id and isinstance(address, str) and
                        isinstance(flagged, list) and address in flagged)
        else:
            released = False
        valid = bool(legacy or released)
        return dict(lease_closed=valid, source_retention='submitted' if valid else 'unknown',
                    verification='flag_review' if valid else 'unknown', flag_id=response.get('flag_id'))
    if operation == 'submit':
        sid = response.get('submission_id')
        valid = isinstance(sid, str) and bool(sid)
        return dict(lease_closed=valid, source_retention='submitted' if valid else 'unknown',
                    submission_id=sid, verification='pending' if valid else 'unknown')
    retained = response.get('best_code')
    kept = isinstance(retained, str) and retained.startswith('best_code kept:')
    return dict(lease_closed=response.get('released') is True,
                source_retention=('retained' if kept else 'failed') if has_source else 'not_applicable',
                verification='not_submitted')


def record_closure(db, cid, outcome, ack_path, ack, assessment):
    if not db.execute('SELECT 1 FROM attempts WHERE claim_id=?', (cid,)).fetchone():
        return
    response = ack['response']
    if outcome == 'submitted':
        identifier = response['submission_id']
    elif outcome == 'flag_review':
        # The current API returns no flag ID. Use the real source checkpoint,
        # never invent an identifier or rewrite the historical response.
        identifier = response.get('flag_id') or ack.get('checkpoint_id')
    else:
        identifier = str(ack_path)
    ledger.record(db, argparse.Namespace(claim=cid, kind=outcome, identifier=identifier,
                                        checkpoint=ack.get('checkpoint_id'),
                                        note='source retention: ' + assessment['source_retention']))


def closing_notes_record(folder, worker, state=None):
    """Validate an actual own-claim notes receipt, independently of report prose."""
    folder = Path(folder)
    state = life.snapshot(folder, worker) if state is None else state
    if state.get('outcome') == 'blocked' and not state.get('claim_id') and not state.get('claim_attempted'):
        return dict(required=False, status='not_applicable', checkpoint_id=None,
                    receipt_path=None, receipt_sha256=None)
    path = folder/worker/'closing-notes.json'
    try:
        value = life.load(path)
        if not isinstance(value, dict):
            raise ValueError('notes receipt is not an object')
        body, response = value['request'], value['response']
        if not isinstance(body, dict) or not isinstance(response, dict):
            raise ValueError('notes request/response is not an object')
        if (state['state'] != 'closed' or not state.get('claim_id') or value.get('schema_version') != 1 or
                value.get('state') != 'acknowledged' or value.get('address') != state['address'] or
                value.get('session_id') != state['session_id'] or body.get('claim_id') != state['claim_id'] or
                body.get('phase') != 'notes' or not isinstance(body.get('note'), str) or not body['note'].strip() or
                not isinstance(response.get('checkpoint_id'), str) or not response['checkpoint_id']):
            raise ValueError('receipt does not acknowledge notes for this closed claim/session')
        closed = life.timestamp(state['closed_utc'])
        requested = life.timestamp(value['requested_utc'])
        observed = life.timestamp(value['observed_utc'])
        if not closed <= requested <= closed + life.timedelta(minutes=30) or not requested <= observed <= life.utc():
            # A valid server acknowledgement may arrive after the request window
            # ends. Do not discard it solely because the response was delayed.
            raise ValueError('notes request/acknowledgement timestamps are inconsistent')
        expected_id = 'closing-notes-' + hashlib.sha256((state['claim_id']+'\0'+body['note']).encode()).hexdigest()[:40]
        if body.get('request_id') != expected_id or state.get('closing_notes_request_id') != expected_id:
            raise ValueError('notes receipt request identity differs')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ValueError('closing notes missing or invalid: '+str(exc)) from None
    return dict(required=True, status='acknowledged', checkpoint_id=response['checkpoint_id'],
                receipt_path=str(path), receipt_sha256=ledger.digest(path))


def closing_notes(folder, worker, note, api=request):
    folder = Path(folder)
    state = life.snapshot(folder, worker)
    if state['state'] != 'closed' or not state.get('claim_id'):
        raise ValueError('closing notes require acknowledged own-claim closure first')
    if not isinstance(note, str) or not note.strip() or len(note.encode('utf-8')) > 32768:
        raise ValueError('closing notes need substantive bounded text')
    path = folder/worker/'closing-notes.json'
    if path.exists():
        previous = life.load(path)
        if previous.get('state') == 'acknowledged' and previous.get('request', {}).get('note') == note:
            return dict(saved=True, **closing_notes_record(folder, worker, state))
        raise ValueError('closing notes already reserved; reconcile, never retry or overwrite')
    if life.utc() > life.timestamp(state['closed_utc']) + life.timedelta(minutes=30):
        raise ValueError('closing notes window expired; do not fabricate a receipt')
    request_id = 'closing-notes-' + hashlib.sha256((state['claim_id']+'\0'+note).encode()).hexdigest()[:40]
    body = dict(claim_id=state['claim_id'], phase='notes', note=note, request_id=request_id)
    with life.transaction(folder) as db:
        current = life.read_worker(db, worker)
        if current.get('closing_notes_request_id') or current['state'] != 'closed':
            raise ValueError('closing notes already reserved; reconcile, never retry or overwrite')
        current['closing_notes_request_id'] = request_id
        life.write_worker(db, worker, current)
        state = current
    value = dict(schema_version=1, address=state['address'], session_id=state['session_id'],
                 request=body, requested_utc=life.stamp(), state='pending')
    # Reserve before mutation; ambiguous transport stays pending without replay.
    life.save(path, value)
    response = api('checkpoint', body)
    acknowledged = isinstance(response, dict) and isinstance(response.get('checkpoint_id'), str) and bool(response['checkpoint_id'])
    value.update(response=response, observed_utc=life.stamp(), state='acknowledged' if acknowledged else 'unresolved')
    life.save(path, value)
    if not acknowledged:
        raise ValueError('closing notes not acknowledged; preserve response and reconcile')
    with life.transaction(folder) as db:
        current = life.read_worker(db, worker)
        if current['state'] == 'closed':
            current['cleanup_required'] = False
            life.write_worker(db, worker, current)
    return dict(saved=True, **closing_notes_record(folder, worker, state))


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


def _near_miss_assessment(folder, worker, state, cid, notes, db_path, origin):
    """Validate a voluntary near-miss review without freezing source or writing evidence.

    A forced closure (deadline, STOP, host cleanup, recovery, operator) records the
    measured case without demanding model prose.  A worker that still has
    experiment time and a close residual must name the best check, what it tested,
    what remains untried and why stopping now is justified.
    """
    with closing(ledger.connect(db_path)) as db:
        near = ledger.near_miss(db, cid)
    voluntary = origin == 'worker' and life.voluntary_experiment(folder, worker, state)
    if near and voluntary:
        supplied = notes.get('near_miss_review') if isinstance(notes, dict) else None
        if not isinstance(supplied, dict):
            raise ValueError('a near miss needs near_miss_review before voluntary give-up '
                             '(best_check_id, tested, untried, stop_reason)')
        best = supplied.get('best_check_id')
        if type(best) is not int or best != near['check_id']:
            raise ValueError('near-miss review must name the current best valid check %d' % near['check_id'])
        for key, minimum in (('tested', 10), ('stop_reason', 20)):
            if not isinstance(supplied.get(key), str) or len(supplied[key].strip()) < minimum:
                raise ValueError('near-miss review %s needs substantive text' % key)
        if not isinstance(supplied.get('untried'), str) or not supplied['untried'].strip():
            raise ValueError("near-miss review untried must be concrete text or 'none'")
        record = dict(schema_version=1, claim_id=cid, address=state['address'], status='accepted',
                      request=dict(best_check_id=best, tested=supplied['tested'].strip(),
                                   untried=supplied['untried'].strip(),
                                   stop_reason=supplied['stop_reason'].strip()),
                      host=dict(near, voluntary=voluntary, phase=life.phase(state)),
                      origin=origin,
                      remaining_experiment_seconds=round(life.remaining_experiment_seconds(state), 1),
                      recorded_utc=life.stamp())
    else:
        record = dict(schema_version=1, claim_id=cid, address=state['address'], status='not_required',
                      request=None,
                      host=(dict(near, voluntary=voluntary, phase=life.phase(state)) if near else None),
                      origin=origin,
                      reason='no_near_miss' if not near else 'forced_or_host_closure',
                      remaining_experiment_seconds=round(life.remaining_experiment_seconds(state), 1),
                      recorded_utc=life.stamp())
    path = folder / worker / 'near-miss-review.json'
    if path.exists():
        previous = life.load(path)
        if previous.get('status') != record['status'] or previous.get('request') != record['request']:
            raise ValueError('a different near-miss review is already recorded; reconcile without retry')
        return previous
    return record


def _record_near_miss(folder, worker, cid, record, db_path):
    path = folder / worker / 'near-miss-review.json'
    if path.exists():
        previous = life.load(path)
        if previous != record:
            raise ValueError('a different near-miss review is already recorded; reconcile without retry')
    else:
        life.save(path, record)
    with closing(ledger.connect(db_path)) as db:
        if db.execute('SELECT 1 FROM attempts WHERE claim_id=?', (cid,)).fetchone():
            stored = db.execute("SELECT 1 FROM events WHERE claim_id=? AND kind='near_miss_review' "
                                "AND payload=? LIMIT 1", (cid, json.dumps(record))).fetchone()
            if not stored:
                with db:
                    ledger.event(db, cid, 'near_miss_review', record)
    return life.load(path)


def prior_unfit_rejection(error, address):
    """Recognize only the service's explicit non-mutating, address-bound rejection."""
    if not isinstance(error, ServiceError) or error.code != 409 or not error.response_body:
        return False
    try:
        value = json.loads(error.response_body)
    except (ValueError, UnicodeError):
        return False
    message = value.get('error') if isinstance(value, dict) else None
    if isinstance(message, dict):
        message = message.get('message')
    if not isinstance(message, str):
        return False
    return (message.startswith('function ' + address + ': this exact code was already found unfit ')
            and 'Nothing was submitted (no strike).' in message
            and '(it matches the pack but not the project file)' in message
            and set(re.findall(r'0x[0-9a-fA-F]{8}', message)) == {address})


def rejection_record(folder, worker, state):
    """Read and validate the private rejection without mutating lifecycle state."""
    folder = Path(folder)
    path = folder / worker / 'submit-error.json'
    saved = life.load(path)
    if saved.get('claim_id') != state['claim_id'] or saved.get('address') != state['address'] or saved.get('session_id') != state['session_id']:
        raise ValueError('rejection identity differs from assignment')
    error = ServiceError(saved['http_status'], response_body=saved['response_body'])
    if (saved.get('operation') != 'submit' or not prior_unfit_rejection(error, state['address']) or
            ledger.digest(folder / worker / 'closure-source.cpp') != saved['source_sha256']):
        raise ValueError('rejection evidence or frozen source invalid')
    return saved


def resume_rejected_submit(folder, worker, status):
    """Resolve a proven rejection, not an ambiguous mutation. Closure only, no retry."""
    folder = Path(folder)
    with life.transaction(folder) as db:
        state = life.read_worker(db, worker)
        if state['state'] != 'close_pending' or state.get('closure_operation') != 'submit':
            raise ValueError('rejection cannot resolve a different or ambiguous closure')
        rejection_record(folder, worker, state)
        owned(status, state['claim_id'], state['address'])
        state.update(state='claimed', closure_requested=True,
                     submission_rejected='prior_unfit', closure_operation=None)
        life.write_worker(db, worker, state)
    return dict(state='claimed', submission_rejected='prior_unfit', next_action='giveup',
                note='Submission definitively rejected; closure only. Preserve checked source; never resubmit it.')


def close(folder, worker, operation, source=None, notes=None, no_source_reason=None,
          db_path=ledger.DEFAULT_DB, api=request, origin='operator'):
    # Validate model-controlled shapes before any network or closure reservation.
    if notes is not None and not isinstance(notes, dict):
        raise ValueError('closure notes must be an object, not encoded JSON or a list')
    if no_source_reason is not None and (not isinstance(no_source_reason, str) or not no_source_reason.strip()):
        raise ValueError('no-source reason must be substantive text')
    if origin not in ('worker', 'recovery', 'operator'):
        raise ValueError('unknown closure origin')
    folder = Path(folder)
    state = life.snapshot(folder, worker)
    cid = state['claim_id']
    if state.get('submission_rejected') and operation != 'giveup':
        raise ValueError('prior-unfit submission rejected; giveup only, never replay submit')
    if origin == 'worker' and life.voluntary_experiment(folder, worker, state) and life.unread_help(folder, worker):
        raise ValueError('unread parent help: read status.help then help_ack before voluntary closure')
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
    # All reversible validation (including a near-miss review) happens before the
    # immutable closure source is created, so a rejected give-up cannot freeze the
    # wrong candidate and block a later, better handoff.
    near_record = (_near_miss_assessment(folder, worker, state, cid, notes, db_path, origin)
                   if operation == 'giveup' else None)
    if source:
        frozen = folder / worker / 'closure-source.cpp'
        if frozen.exists():
            if frozen.read_bytes() != data:
                raise ValueError('refusing to overwrite a different frozen closure source')
    # Reject unread answers before creating immutable artifacts. Once reserved,
    # failures leave close_pending for host reconciliation, never new experiments.
    life.begin_close(folder, worker, require_help_read=origin == 'worker', operation=operation)
    if source:
        if not frozen.exists():
            with frozen.open('xb') as output:
                output.write(data)
    if near_record is not None:
        _record_near_miss(folder, worker, cid, near_record, db_path)
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
        checkpoint_id = saved.get('checkpoint_id') if isinstance(saved, dict) else None
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError('proof checkpoint not acknowledged; preserve response and reconcile')
        response = api('flag', dict(claim_id=cid, scope='function', category='pack_problem', note=notes['note'],
                                   compile_flags=proof_flags, checkpoint_id=checkpoint_id, keep_claim=False))
    else:
        try:
            response = api(operation, body)
        except ServiceError as exc:
            # Save the actual bounded response privately before classification. Unknown
            # conflicts and transport failures keep the pending reservation untouched.
            error_path = folder / worker / (operation + '-error.json')
            error_record = dict(claim_id=cid, address=state['address'], session_id=state['session_id'],
                                operation=operation, observed_utc=life.stamp(), http_status=exc.code,
                                retry_after=exc.retry_after,
                                response_body=exc.response_body.decode('utf-8', errors='replace') if isinstance(exc.response_body, bytes) else exc.response_body,
                                source_sha256=ledger.digest(frozen) if source else None)
            with error_path.open('x', encoding='utf-8') as output:
                json.dump(error_record, output)
            if operation != 'submit' or not prior_unfit_rejection(exc, state['address']):
                raise
            resume_rejected_submit(folder, worker, api('status', {}))
            result = close(folder, worker, 'giveup', source=frozen, db_path=db_path, api=api, origin=origin,
                           notes=dict(reason='Service explicitly rejected identical previously unfit source; nothing submitted.',
                                      category='unknown_type',
                                      tried='Final official MATCH was submitted once and definitively rejected as previously unfit for project-file integration.',
                                      still_differs='No local codegen difference. Project-file integration remains unfit; private submit-error.json retains the service explanation.',
                                      suspected_cause='Candidate declarations conflict with project-file types. A future reviewed attempt needs an integration repair, not identical resubmission.'))
            result['assessment'].update(submission_rejected='prior_unfit', operation='giveup',
                                        next_action='Save closing notes and generate report; source retained, not submitted.')
            return result
    ack_path = folder / worker / 'service-ack.json'
    envelope = dict(claim_id=cid, address=state['address'], operation=operation,
                    observed_utc=life.stamp(), response=response, assessment=None,
                    source_sha256=ledger.digest(frozen) if source else None,
                    no_source_reason=no_source_reason, checkpoint_id=checkpoint_id)
    # Even a malformed mutation response is durable before interpretation.
    life.save(ack_path, envelope)
    assessment = acknowledgement(response, operation, bool(source), claim_id=cid, address=state['address'])
    envelope['assessment'] = assessment
    life.save(ack_path, envelope)
    if not assessment['lease_closed']:
        raise ValueError('service did not acknowledge closure; reconcile saved response')
    outcome = 'flag_review' if operation == 'flag-proof' else 'submitted' if operation == 'submit' else 'given_up'
    life.closed(folder, worker, outcome, ack_path, assessment['source_retention'])
    # Remote acknowledgement remains durable even if local recording fails.
    with closing(ledger.connect(db_path)) as db:
        record_closure(db, cid, outcome, ack_path, envelope, assessment)
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ledger.DEFAULT_DB))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('claim', 'giveup', 'submit', 'reconcile', 'flag-proof', 'notes'):
        p = sub.add_parser(name); p.add_argument('folder'); p.add_argument('worker')
        if name == 'giveup':
            p.add_argument('--source'); p.add_argument('--notes', required=True)
            p.add_argument('--no-source-reason')
        if name == 'reconcile':
            p.add_argument('--claim-id')
        if name == 'flag-proof':
            p.add_argument('--notes', required=True)
        if name == 'notes':
            p.add_argument('--notes', required=True, help='JSON with substantive note text')
    args = parser.parse_args()
    try:
        result = (closing_notes(args.folder, args.worker, life.load(args.notes)['note']) if args.command == 'notes' else
                  reconcile(args.folder, args.worker, args.claim_id, db_path=args.db) if args.command == 'reconcile' else
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
