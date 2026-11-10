"""Host-only batch recovery: inventory by default, explicit reviewed actions to close.

Never claims, retries an ambiguous mutation, chooses a replacement, or releases an
unrelated lease. This is not an automatic bulk cleanup policy.
"""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

import evidence
import lifecycle as life
import matchflow as ledger
import service
from security import scoped
from snapshots import checked_source, available


def inventory(folder, status, db_path=ledger.DEFAULT_DB):
    folder = Path(folder)
    tasks = life.manifest(folder)['tasks']
    opened = {c['claim_id']: c for c in status['open_claims']}
    workers = []
    for task in tasks:
        worker = task['id']
        state = life.snapshot(folder, worker)
        cid = state.get('claim_id')
        checks = []
        if cid and Path(db_path).is_file() and evidence.has_attempt(db_path, cid):
            with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                for c in db.execute('SELECT * FROM checks WHERE claim_id=? ORDER BY id', (cid,)):
                    item = evidence.check_record(db, c)
                    item['snapshot_available'] = available(c)
                    checks.append(item)
        try:
            notes = service.closing_notes_record(folder, worker, state)
        except ValueError:
            notes = dict(status='missing_or_unresolved')
        ack = folder/worker/'service-ack.json'
        near = folder/worker/'near-miss-review.json'
        from broker import saved_binding
        bound = saved_binding(folder,worker)
        workers.append(dict(worker=worker, address=state['address'], session_id=state['session_id'], claim_id=cid,
                             partial_binding_session_id=(bound['session'] if bound and state.get('session_id') is None else None),
                            state=state['state'], outcome=state.get('outcome'),
                            session_finished=bool(state.get('session_finished_utc') or (folder/worker/'usage-raw.json').exists()),
                            lease=opened.get(cid), acknowledgement_present=ack.is_file(), closing_notes=notes,
                            near_miss=life.load(near) if near.is_file() else None,
                            checks=checks, review_required=state['state'] != 'closed' or notes['status'] == 'missing_or_unresolved'))
    return dict(workers=workers, unrelated_open_claims=sorted(set(opened)-{w['claim_id'] for w in workers}),
                note='Inventory only. Review exact identities, checked source, structured giveup notes and closing note. '
                     'Do not apply actions to running sessions or replay pending mutations.')


def apply(folder, actions, db_path=ledger.DEFAULT_DB, api=service.request):
    folder = Path(folder)
    manifest = life.manifest(folder)
    if manifest.get('schema_version') != 3:
        raise ValueError('only reviewed v3 assignments support applied recovery')
    if not isinstance(actions, dict) or actions.get('batch') != manifest['batch'] or not isinstance(actions.get('workers'), list):
        raise ValueError('reviewed actions must identify this batch and a workers list')
    tasks = {t['id']: t for t in manifest['tasks']}
    seen = set()
    # Validate the entire reviewed set locally before any remote mutation.
    for action in actions['workers']:
        if not isinstance(action, dict) or action.get('worker') not in tasks or action['worker'] in seen:
            raise ValueError('unique exact prepared workers required')
        worker = action['worker']; seen.add(worker)
        if not re.fullmatch(r'worker[0-9]{2,3}', worker):
            raise ValueError('canonical prepared worker required')
        state = life.snapshot(folder, worker)
        assignment_path = scoped(folder, worker+'/assignment.json', exists=True)
        assignment = life.load(assignment_path)
        if (ledger.digest(assignment_path) != tasks[worker].get('assignment_sha256') or
                assignment['target']['address'] != state.get('address') or tasks[worker]['address'] != state.get('address')):
            raise ValueError('immutable recovery assignment differs from task or lifecycle')
        if action.get('binding_session_id') and state.get('session_id') is None:
            if action.get('session_id') != action['binding_session_id']:
                raise ValueError('reviewed session must match explicit partial binding session')
            from broker import reconcile_unprompted_binding
            reconcile_unprompted_binding(folder,worker,action['binding_session_id'])
            state = life.snapshot(folder,worker)
        if any(action.get(k) != state.get(k) for k in ('address', 'claim_id', 'session_id')):
            raise ValueError('reviewed recovery identity differs from bound assignment')
        from streaming import unassigned_recovery
        unassigned = action.get('unassigned') is True and unassigned_recovery(folder, worker, state)
        if not unassigned and (not isinstance(state.get('session_id'), str) or not re.fullmatch(r'ses_[A-Za-z0-9]+', state['session_id'])):
            raise ValueError('real bound session required for applied recovery')
        unprompted = (action.get('unprompted_binding') is True and
                      state.get('unprompted_binding', {}).get('session_id') == state.get('session_id') and
                      not state.get('setup_started'))
        if not unassigned and not unprompted and action.get('session_finished') is not True:
            raise ValueError('review must confirm this exact session has finished')
        if not unassigned and not unprompted and not state.get('session_finished_utc'):
            usage_path = folder/worker/'usage-raw.json'
            usage = life.load(usage_path) if usage_path.is_file() else {}
            if usage.get('session_id') != state['session_id'] or not usage.get('captured_utc'):
                raise ValueError('host completion evidence required; never race a running worker')
        if action.get('operation') not in ('giveup', 'finish'):
            raise ValueError('recovery only gives up an explicitly reviewed claim or finishes acknowledged closure')
        if (unassigned or unprompted) and (action.get('source') or 'check_id' in action or not action.get('no_source_reason')):
            raise ValueError('unassigned recovery requires explicit no-source reason; no candidate exists')
        if action.get('source') and 'check_id' in action:
            raise ValueError('choose source OR check_id, not both')
        if (state['state'] == 'close_pending' and not (folder/worker/'service-ack.json').exists()
                and not (state.get('closure_operation') == 'submit' and (folder/worker/'submit-error.json').exists())):
            raise ValueError('pending closure has no receipt; reconcile without retry')
        try:
            service.closing_notes_record(folder, worker, state)
        except ValueError:
            if (folder/worker/'closing-notes.json').exists():
                raise ValueError('existing closing-notes reservation is unresolved; no replay')
            if not isinstance(action.get('closing_note'), str) or not action['closing_note'].strip():
                raise ValueError('substantive reviewed closing_note required')
            if len(action['closing_note'].encode('utf-8')) > 32768:
                raise ValueError('reviewed closing_note exceeds service note limit')
        if action['operation'] == 'giveup' and state['state'] not in ('claimed', 'close_pending', 'closed'):
            raise ValueError('resolve pending claim ownership manually before recovery')
        if action['operation'] == 'giveup' and state['state'] in ('claimed', 'close_pending'):
            notes = action.get('notes')
            if not isinstance(notes, dict) or not all(isinstance(notes.get(k), str) and notes[k].strip()
                    for k in ('reason', 'category', 'tried', 'still_differs', 'suspected_cause')):
                raise ValueError('reviewed structured giveup notes required')
            if notes['category'] not in {'pack_problem', 'compiler_mismatch', 'register_allocation', 'instruction_order',
                                         'stack_layout', 'eh', 'unknown_type', 'out_of_time', 'other'}:
                raise ValueError('unsupported giveup category')
            if 'check_id' in action:
                with closing(ledger.connect(db_path)) as db:
                    checked_source(db, state['claim_id'], action['check_id'])
            elif action.get('source'):
                source = scoped(folder/worker/'files', action['source'], exists=True)
                if ledger.digest(source) != action.get('source_sha256'):
                    raise ValueError('reviewed source_sha256 required and must match the legacy candidate')
            elif not isinstance(action.get('no_source_reason'), str) or not action['no_source_reason'].strip():
                raise ValueError('choose a checked source or give an explicit no-source reason')
    results = []
    for action in actions['workers']:
        worker = action['worker']
        state = life.snapshot(folder, worker)
        if (action.get('unassigned') or action.get('unprompted_binding')) and state['state'] != 'closed':
            from streaming import unassigned_recovery
            if not (state.get('unprompted_binding') and not state.get('setup_started')) and not unassigned_recovery(folder,worker,state):
                raise ValueError('unassigned claim no longer safe for recovery')
            life.setup_step(folder,worker,'claim')
            life.request_cleanup(folder,worker,'unassigned_stream_recovery')
        # Read-only reconciliation first; close_pending is never replayed.
        service.reconcile(folder, worker, api=api, db_path=db_path)
        state = life.snapshot(folder, worker)
        if state['state'] == 'claimed':
            if action['operation'] != 'giveup':
                raise ValueError('finish requires an acknowledged closure, not an open lease')
            source = None
            if 'check_id' in action:
                with closing(ledger.connect(db_path)) as db:
                    source = checked_source(db, state['claim_id'], action['check_id'])
            elif action.get('source'):
                source = scoped(folder/worker/'files', action['source'], exists=True)
                if ledger.digest(source) != action['source_sha256']:
                    raise ValueError('reviewed source changed before recovery; do not close')
            service.close(folder, worker, 'giveup', source=source, notes=action['notes'],
                          no_source_reason=action.get('no_source_reason'), db_path=db_path,
                          api=api, origin='recovery')
        if life.snapshot(folder, worker)['state'] != 'closed':
            raise ValueError('closure unresolved; no retry or guessed acknowledgement')
        try:
            service.closing_notes_record(folder, worker)
        except ValueError:
            service.closing_notes(folder, worker, action['closing_note'], api=api)
        report_path = folder/worker/'report.json'
        if not report_path.exists():
            evidence.report(folder, worker, db_path=db_path)
        status = api('status', {})
        audited = evidence.audit_worker(folder, tasks[worker], life.snapshot(folder, worker), status, db_path)
        results.append(dict(worker=worker, **audited))
        if audited['evidence_integrity'] != 'valid' or audited['lease_closure'] not in ('closed_acknowledged', 'not_claimed'):
            raise ValueError('recovery audit requires review; no further actions applied')
        if manifest.get('streaming'):
            with life.transaction(folder) as db:
                db.execute("UPDATE stream_tasks SET state='closed' WHERE worker=?", (worker,))
    return dict(workers=results, ok=True, note='Only reviewed workers touched; no claims, replacements or mutation retries.')
