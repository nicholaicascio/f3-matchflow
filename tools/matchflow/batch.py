"""Shared fixed-address assignment pipeline. No service writes or credentials."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time
import sys
import uuid

import lifecycle
import review
from function_info import co_workers
from config import workspace_root, worker_model, DEFAULT_WORKER_MODEL

ROOT = workspace_root()
CODE = Path(__file__).resolve().parent
BATCHES = ROOT / 'work' / 'batches'
DEFAULT_MODEL = DEFAULT_WORKER_MODEL


def now():
    return lifecycle.stamp()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    lifecycle.save(path, value)


def fresh(stamp):
    parsed = lifecycle.timestamp(stamp)
    if parsed.tzinfo is None or not -60 <= (datetime.now(timezone.utc)-parsed).total_seconds() <= 900:
        raise ValueError('Refresh service metadata/status: timezone-aware checked_at within 15 minutes required')


def validate_selection(selection):
    fresh(selection['checked_at'])
    targets = selection['targets']
    if not targets:
        raise ValueError('Orchestrator must select at least one address')
    seen = set()
    for target in targets:
        missing = {'address', 'size', 'state', 'give_ups',
                   'open_problem_reports', 'file_problem_reports', 'reason'} - target.keys()
        if missing:
            raise ValueError('Selection %s missing required fields: %s' % (
                target.get('address', '<unknown>'), ', '.join(sorted(missing))))
        # Normalize only this selection, never the saved service receipt.
        target['co_workers'] = co_workers(target)
        address = target['address']
        if not re.fullmatch(r'0x[0-9a-f]{8}', address) or address in seen:
            raise ValueError('Use unique canonical 0x addresses, not bands')
        seen.add(address)
        if target.get('state') != 'open' or target.get('co_workers', 0):
            raise ValueError('Only currently open, unheld functions may be dispatched')
        if not isinstance(target.get('reason'), str) or not target['reason'].strip():
            raise ValueError('Each selection needs a reviewed reason')
        if not isinstance(target.get('size'), int) or target['size'] <= 0:
            raise ValueError('Missing function size')
        for key in ('give_ups', 'open_problem_reports', 'file_problem_reports', 'co_workers'):
            if type(target.get(key, 0)) is not int or target.get(key, 0) < 0:
                raise ValueError('selection %s must be a nonnegative integer' % key)
        # Report scope, repeated blockers and prior integration failure are separate
        # facts.  An unrelated sibling report no longer excludes an otherwise good
        # target, and there is no mutually exclusive lane to choose.
        target['review'] = review.normalized(target)
        target.pop('lane', None)
    return targets


def batch_path(name):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('Batch name must be a short lowercase slug')
    return BATCHES / name


def prompt_for(assignment):
    from string import Template
    target = load(assignment)['target']
    return Template((CODE/'prompts/worker.txt').read_text(encoding='utf-8')).substitute(
        address=target['address'], review=review.summary(target), reason=target['reason'])


def prepare(name, selection_path, model=None):
    model = worker_model(model)
    selection = load(selection_path)
    # Import only known local verdicts; absence is never evidence of clean history.
    ledger_path = ROOT / 'work' / 'matchflow' / 'ledger.sqlite3'
    if ledger_path.exists():
        import sqlite3
        with closing(sqlite3.connect(ledger_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            for target in selection['targets']:
                rows = db.execute('SELECT verdict FROM server_results WHERE address=? AND verdict IS NOT NULL',
                                  (target['address'],)).fetchall()
                rows += db.execute("SELECT state FROM attempts WHERE address=? AND state='unfit'",
                                   (target['address'],)).fetchall()
                known = sorted(set(r[0] for r in rows))
                if 'review' in target:
                    target['review']['prior_verdicts'] = sorted(
                        set(target['review'].get('prior_verdicts', []) + known))
                else:
                    target['prior_verdicts'] = sorted(set(target.get('prior_verdicts', []) + known))
    targets = validate_selection(selection)
    # Render every prompt before creating any batch directory, so a fact/type or
    # template error cannot leave a partial immutable batch behind.
    from string import Template
    template = Template((CODE/'prompts/worker.txt').read_text(encoding='utf-8'))
    prompts = [template.substitute(address=t['address'], review=review.summary(t), reason=t['reason'])
               for t in targets]
    path = batch_path(name)
    path.mkdir(parents=True, exist_ok=False)
    tasks = []
    for i, (target, prompt) in enumerate(zip(targets, prompts), 1):
        worker = path / f'worker{i:02}'
        worker.mkdir()
        assignment = worker / 'assignment.json'
        (worker/'files').mkdir()
        claim_body = dict(address=target['address'], cowork=False, context_mode='resume',
                          model=model.split('#')[0], harness='OpenCode')
        if '#' in model:
            claim_body['effort'] = model.split('#', 1)[1]
        save(assignment, dict(schema_version=3, worker=i, model=model, harness='OpenCode', target=target,
                             workspace=str(ROOT), output_dir=str(worker), selection_checked_at=selection['checked_at'],
                             claim_body=claim_body))
        (worker/'prompt.txt').write_text(prompt, encoding='utf-8')
        from matchflow import digest
        tasks.append(dict(id=worker.name, address=target['address'], model=model, agent='match-worker',
                          assignment_sha256=digest(assignment), prompt=prompt))
    manifest = dict(schema_version=3, batch=name, created=now(), model=model, selection=selection, tasks=tasks)
    save(path/'batch.json', manifest)
    return dict(batch=str(path), count=len(tasks), model=model)


def capacity(snapshot, count):
    fresh(snapshot['checked_at'])
    status = snapshot['status']
    if status.get('submissions_paused') or status.get('claiming_paused_until'):
        raise ValueError('Service claiming/submission pause: do not launch')
    if count > status['limits']['claims'] - len(status['open_claims']):
        raise ValueError('Insufficient free claim slots for this entire fixed batch')
    if status.get('submissions_today', 0) + count > status['limits'].get('subsPerDay', float('inf')):
        raise ValueError('Insufficient daily submission capacity; do not launch')
    if status.get('awaiting_verification', 0) + count > status['limits'].get('pending', float('inf')):
        raise ValueError('Insufficient pending verification capacity; do not launch')


def dispatch(name, method, status_path):
    if method != 'subagents':
        raise ValueError('only restricted subagents are enabled; legacy launches are unsafe')
    path = batch_path(name)
    manifest = load(path / 'batch.json')
    if manifest.get('schema_version') != 3:
        raise ValueError('legacy batch cannot launch; prepare reviewed v3 assignments')
    from preflight import require
    require()
    validate_selection(manifest['selection'])
    capacity(load(status_path), len(manifest['tasks']))
    result = lifecycle.dispatch(path, method)
    token = str(uuid.uuid4())
    lifecycle.tick(path, token)
    with (path / 'watchdog.log').open('a', encoding='utf-8') as output:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('lifecycle.py')),
                                    'watch', str(path), '--token', token], cwd=ROOT,
                                   stdout=output, stderr=output)
    save(path / 'launch.json', dict(method=method, started=result['dispatched_utc'], watchdog_pid=process.pid))
    return dict(batch=str(path), **result, watchdog_pid=process.pid)


def monitor(name):
    """Local safety monitor: run with the shell tool in background for a wake-up.

    Never polls a service or asks agents for progress. Returns on closure request,
    supervisor failure, or all workers closing, so the harness notifies the parent.
    """
    path = batch_path(name)
    while True:
        manifest = lifecycle.manifest(path)
        tasks = manifest['tasks']
        if manifest.get('streaming'):
            # The helper refreshes the manifest on every pass. Read the queue again
            # under the control lock: a producer may append between those reads.
            with lifecycle.transaction(path) as db:
                from streaming import supervise
                supervise(db,path)
                row = db.execute('SELECT payload FROM control WHERE id=1').fetchone()
                if not row:
                    raise ValueError('dispatch before monitoring')
                control = json.loads(row['payload'])
                queue = db.execute('SELECT worker,payload,state FROM stream_tasks ORDER BY seq').fetchall()
                tasks = [json.loads(item['payload']) for item in queue]
                states = []
                for task in tasks:
                    row = db.execute('SELECT payload FROM workers WHERE id=?', (task['id'],)).fetchone()
                    # Preparing/failed entries may not have a lifecycle worker yet.
                    # They remain unresolved, never disappear from the completion set.
                    states.append(json.loads(row['payload']) if row else None)

                def event(key, value):
                    seen = control.setdefault(key, [])
                    if value in seen:
                        return False
                    seen.append(value)
                    db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(control),))
                    return True

                stream = control.get('stream', {})
                producer = stream.get('producer_state')
                active = producer not in ('done', 'failed') or any(
                    state is None or state['state'] != 'closed' for state in states)
                heartbeat = control.get('heartbeat')
                safety = []
                if (path/'STOP').exists():
                    safety.append(dict(event='stop'))
                if active and (heartbeat is None or not 0 <= time.monotonic()-heartbeat <= 30):
                    safety.append(dict(event='supervisor_lost'))
                for key in ('cleanup_barrier', 'start_barrier'):
                    if control.get(key):
                        safety.append(dict(event=key, detail=control[key]))
                fresh_safety = [item for item in safety if event('monitor_seen_safety', item)]
                if fresh_safety:
                    return dict(state='supervisor_lost' if any(
                        item['event'] == 'supervisor_lost' for item in fresh_safety) else 'closure_requested',
                        events=fresh_safety, workers=[], cleanup_required=True,
                        note='Safety barrier active; reconcile before starting more workers.')
                due = [dict(worker=task['id'], claim_id=state.get('claim_id'),
                            session_id=state.get('session_id'), phase=lifecycle.phase(state))
                       for task, state in zip(tasks, states) if state and state['state'] != 'closed' and
                       (state.get('closure_requested') or lifecycle.phase(state) != 'experiment' or
                        (path/task['id']/'CLOSE_REQUESTED.json').exists() or (path/'STOP').exists())]
                fresh_due = [item for item in due if event('monitor_seen', item)]
                if fresh_due:
                    return dict(state='closure_requested', workers=fresh_due, cleanup_required=True,
                                note='Re-arm monitor for new requests or escalation; no replacement starts.')
                # Read help directly, including newly appended workers not present
                # in the immutable header used by legacy help_requests().
                requests = db.execute("SELECT worker,payload FROM escalations WHERE state='pending' ORDER BY seq").fetchall()
                pending = [dict(json.loads(row['payload']), worker=row['worker'], state='pending') for row in requests]
                fresh_help = [item for item in pending if event('monitor_seen_help', item['request_id'])]
                if fresh_help:
                    return dict(state='worker_help_requested', workers=fresh_help, cleanup_required=False,
                                note='Answer the worker help request, then re-arm the monitor.')
                if producer == 'failed' and event('monitor_seen_stream', dict(event='producer_failed')):
                    return dict(state='producer_failed', stream=stream, cleanup_required=True,
                                note='Discovery failed; preserve unresolved tasks and reconcile before new starts.')
                unresolved = [dict(worker=item['worker'], state=item['state'],
                                   reason='Incomplete stream assignment; reconcile, never replay preparation or claiming.')
                              for item, task in zip(queue, tasks) if item['state'] == 'failed' or
                              (producer in ('done', 'failed') and item['state'] in ('preparing', 'claiming')) or
                              (item['state'] == 'queued' and not task.get('assignment_sha256'))]
                fresh_unresolved = [item for item in unresolved if event('monitor_seen_unresolved', item)]
                if fresh_unresolved:
                    return dict(state='stream_tasks_unresolved', workers=fresh_unresolved, cleanup_required=True,
                                note='Unresolved queue rows remain part of the batch; no automatic retries or replacements.')
                # Already-notified safety requests still suppress launch signals.
                if not safety and not due and not unresolved and producer != 'failed':
                    queued = [dict(worker=item['worker'], address=task['address'])
                              for item, task in zip(queue, tasks) if item['state'] == 'queued']
                    available = [item for item in queued if event('monitor_seen_queue', item)]
                    if available:
                        return dict(state='queue_available', workers=available, cleanup_required=False,
                                    note='Reviewed queued assignments available; launch only these exact workers.')
                claimed = stream.get('claimed', sum(bool(state and state.get('claim_id')) for state in states))
                if producer == 'done' and claimed < stream.get('requested', 0):
                    exhausted = dict(event='producer_exhausted', requested=stream['requested'],
                                     claimed=claimed, prepared=len(tasks))
                    if event('monitor_seen_stream', exhausted):
                        return dict(state='producer_exhausted', **exhausted, cleanup_required=False,
                                    note='Discovery ended before the requested count; unresolved tasks still require closure.')
                # Retain this exact snapshot for evidence checks outside the DB lock.
                snapshot = [(task, state, item['state']) for task, state, item in zip(tasks, states, queue)]

            from service import closing_notes_record
            from evidence import local_completion
            complete = []
            for task, state, queue_state in snapshot:
                if state is None or state['state'] != 'closed' or queue_state in ('preparing', 'claiming', 'failed'):
                    continue
                try:
                    notes_done = closing_notes_record(path, task['id'], state)['status'] in ('acknowledged', 'not_applicable')
                    report_done = notes_done and local_completion(path, task, state)['complete']
                except (ValueError, OSError):
                    report_done = False
                if report_done:
                    complete.append(dict(worker=task['id'], event='worker_complete', claim_id=state.get('claim_id'),
                                         session_id=state.get('session_id')))
            # Evidence checks can take time. Revalidate producer, full queue and
            # ownership under the lock before recording any completion decision.
            all_complete = False
            with lifecycle.transaction(path) as db:
                current = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
                latest_queue = db.execute('SELECT worker,payload,state FROM stream_tasks ORDER BY seq').fetchall()
                latest_states = []
                for task in tasks:
                    row = db.execute('SELECT payload FROM workers WHERE id=?', (task['id'],)).fetchone()
                    latest_states.append(json.loads(row['payload']) if row else None)
                control_changed = any(current.get(key) != control.get(key)
                                      for key in ('cleanup_barrier', 'start_barrier')) or (
                    current.get('stream', {}).get('producer_state') != producer)
                if (control_changed or latest_states != states or
                        [tuple(row) for row in latest_queue] != [tuple(row) for row in queue]):
                    continue
                heartbeat = current.get('heartbeat')
                stale = active and (heartbeat is None or not 0 <= time.monotonic()-heartbeat <= 30)
                closed_set = all(state is not None and state['state'] == 'closed' for state in states)
                if not stale and (not (path/'STOP').exists() or closed_set):
                    seen = current.setdefault('monitor_seen_milestones', [])
                    new = [item for item in complete if item not in seen]
                    if new:
                        seen.extend(new)
                        db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(current),))
                        return dict(state='worker_milestones', events=new,
                                    note='Notes and report complete; re-arm while discovery or other workers remain.')
                    if not unresolved and producer in ('done', 'failed') and len(complete) == len(tasks):
                        all_complete = True
            if all_complete:
                return dict(state='all_complete', debrief=debrief(name),
                            note='Producer ended and every task has complete notes and report; audit fresh official status.')
            time.sleep(5)
            continue
        with lifecycle.transaction(path) as db:
            row = db.execute('SELECT payload FROM control WHERE id=1').fetchone()
            if not row:
                raise ValueError('dispatch before monitoring')
            control = json.loads(row['payload'])
            states = [lifecycle.read_worker(db, task['id']) for task in tasks]
        milestones = []
        from service import closing_notes_record
        from evidence import local_completion
        for task, state in zip(tasks, states):
            if state['state'] != 'closed':
                continue
            milestones.append(dict(worker=task['id'], event='claim_closed', claim_id=state.get('claim_id'),
                                   session_id=state.get('session_id')))
            try:
                notes_done = closing_notes_record(path, task['id'], state)['status'] in ('acknowledged', 'not_applicable')
            except (ValueError, OSError):
                notes_done = False
            if notes_done:
                milestones.append(dict(worker=task['id'], event='notes_complete', claim_id=state.get('claim_id'),
                                       session_id=state.get('session_id')))
            if notes_done and local_completion(path, task, state)['complete']:
                milestones.append(dict(worker=task['id'], event='report_complete', claim_id=state.get('claim_id'),
                                       session_id=state.get('session_id')))
        if sum(m['event'] == 'report_complete' for m in milestones) == len(tasks):
            summary = debrief(name)
            return dict(state='all_complete', debrief=summary,
                        note='Claims, notes and reports complete; audit against fresh official status.')
        with lifecycle.transaction(path) as db:
            current = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
            seen = current.setdefault('monitor_seen_milestones', [])
            new = [m for m in milestones if m not in seen]
            if new:
                seen.extend(new)
                db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(current),))
                return dict(state='worker_milestones', events=new, note='Re-arm monitor until all_complete; closure alone is not report completion.')
        heartbeat = control.get('heartbeat')
        if any(s['state'] != 'closed' for s in states) and (heartbeat is None or not 0 <= time.monotonic()-heartbeat <= 30):
            return dict(state='supervisor_lost', cleanup_required=True, note='Closure only; reconcile owned sessions/claims.')
        due = [dict(worker=t['id'], claim_id=s['claim_id'], session_id=s['session_id'], phase=lifecycle.phase(s))
                for t, s in zip(tasks, states) if s['state'] != 'closed' and
                (s.get('closure_requested') or lifecycle.phase(s) != 'experiment' or
                 (path/t['id']/'CLOSE_REQUESTED.json').exists() or (path/'STOP').exists())]
        if due:
            # Re-arming the parent monitor must wait for NEW requests, not spin on
            # the same unfinished worker. Deadlines escalating to reconcile are new.
            with lifecycle.transaction(path) as db:
                current = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
                seen = current.setdefault('monitor_seen', [])
                fresh_due = [item for item in due if item not in seen]
                if fresh_due:
                    seen.extend(fresh_due)
                    db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(current),))
                    return dict(state='closure_requested', workers=fresh_due, cleanup_required=True,
                                note='Re-arm monitor while other workers remain; only new requests or escalation wake it.')
        help_due = help_requests(name)['requests']
        pending = [r for r in help_due if r.get('state') == 'pending']
        if pending:
            with lifecycle.transaction(path) as db:
                current = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
                seen = current.setdefault('monitor_seen_help', [])
                fresh_help = [r for r in pending if r['request_id'] not in seen]
                if fresh_help:
                    seen.extend(r['request_id'] for r in fresh_help)
                    db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(current),))
                    return dict(state='worker_help_requested', workers=fresh_help, cleanup_required=False,
                                note='A worker is still working. Answer with batch.py answer, then re-arm the monitor.')
        time.sleep(5)


def help_requests(name, worker=None):
    """List durable worker help requests without touching a running session."""
    path = batch_path(name)
    tasks = lifecycle.manifest(path)['tasks']
    if worker is not None and worker not in {t['id'] for t in tasks}:
        raise ValueError('unknown prepared worker')
    requests, responses = [], []
    for task in tasks:
        if worker is not None and task['id'] != worker:
            continue
        value = lifecycle.escalations(path, task['id'])
        requests.extend(value['requests'])
        responses.extend(value['responses'])
    return dict(requests=requests, responses=responses,
                note='Requests are informational; answering does not close, extend or replace the claim.')


def answer(name, worker, text, request_id=None):
    """Answer one pending worker help request while its claim stays active."""
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 8000:
        raise ValueError('help answer needs substantive bounded text')
    if not re.fullmatch(r'worker[0-9]{2,3}', worker):
        raise ValueError('canonical prepared worker required')
    path = batch_path(name)
    if not any(t['id'] == worker for t in lifecycle.manifest(path)['tasks']):
        raise ValueError('unknown prepared worker')
    state = lifecycle.snapshot(path, worker)
    if state['state'] != 'claimed':
        raise ValueError('worker is not active; answering a closed assignment is meaningless')
    response = lifecycle.answer_escalation(path, worker, text, request_id)
    return dict(worker=worker, request_id=response['request_id'], answered=True,
                note='The worker reads this through status help; the claim and its deadlines are unchanged.')


def run_worker(path, task, cli):
    raise ValueError('legacy headless workers disabled; use matchflow_launch restricted subagents')


def headless(name, status_path, jobs):
    raise ValueError('legacy headless workers disabled; use matchflow_launch restricted subagents')


def audit(name, status_path, expected=None):
    path = batch_path(name)
    snapshot = load(status_path)
    fresh(snapshot['checked_at'])
    status = snapshot['status']
    opened = {r['claim_id'] for r in status['open_claims']}
    rows = []
    manifest = lifecycle.manifest(path)
    if manifest.get('batch') != name:
        raise ValueError('batch identity mismatch')
    if expected is not None:
        if expected.get('batch') != name or not expected.get('workers'):
            raise ValueError('expected batch and worker identities required')
        for item in expected['workers']:
            state = lifecycle.snapshot(path, item['worker'])
            task = next((t for t in manifest['tasks'] if t['id'] == item['worker']), None)
            if not task or any(not item.get(k) or state.get(k) != item[k]
                               for k in ('address', 'claim_id', 'session_id')) or task['address'] != item['address']:
                raise ValueError('expected address/claim/session does not match exact assignment')
    for task in manifest['tasks']:
        worker = path/task['id']
        ownership = (lifecycle.snapshot(path, task['id']) if (path/'lifecycle.sqlite3').exists() else
                     load(worker/'ownership.json') if (worker/'ownership.json').exists() else {})
        claim = ownership.get('claim_id')
        report = (worker/'report.json').is_file()
        ack = ownership.get('service_ack_path')
        ack_present = bool(ack and ((ROOT/ack).is_file() or (worker/ack).is_file()))
        blocked = ownership.get('outcome') in ('blocked', 'not_started') and not claim
        from evidence import audit_worker
        detail = audit_worker(path, task, ownership, status)
        rows.append(dict(worker=task['id'], address=task['address'], claim_id=claim,
                         cleanup_required=bool(claim in opened) if claim else not blocked,
                          evidence_review_required=detail['evidence_integrity'] != 'valid',
                         ack_present=ack_present,
                         ownership_address_matches=ownership.get('address') == task['address'],
                         report_present=report,
                          note='Remote absence proves no open lease, not a MATCH or submission acknowledgement',
                          **detail))
    batch_claims = {r['claim_id'] for r in rows if r['claim_id']}
    if expected is not None:
        selected = {w['worker'] for w in expected['workers']}
        rows = [r for r in rows if r['worker'] in selected]
    return dict(batch=name, ok=all(not r['cleanup_required'] and r['evidence_integrity'] == 'valid' for r in rows),
                workers=rows, unrelated_open_claims=sorted(opened-batch_claims),
                note='Never release unrelated claims; inspect missing ownership and verify checker/ack evidence separately')


def debrief(name, status_path=None):
    """Compact local evidence summary. Never claims, checks or polls the service."""
    path = batch_path(name)
    manifest = lifecycle.manifest(path)
    if manifest.get('batch') != name:
        raise ValueError('batch identity mismatch')
    audited = audit(name, status_path) if status_path else None
    verified = {r['worker']: r['verification'] for r in audited['workers']} if audited else {}
    rows = []
    for task in manifest['tasks']:
        state = lifecycle.snapshot(path, task['id'])
        report_path = path/task['id']/'report.json'
        report = load(report_path) if report_path.is_file() else {}
        from evidence import local_completion
        integrity = local_completion(path, task, state)
        if report and not integrity['complete']:
            raise ValueError('report evidence invalid; audit before debrief: ' + '; '.join(integrity['errors']))
        if report and (report.get('claim_id') != state.get('claim_id') or
                       report.get('address') != task['address'] or report.get('session_id') != state.get('session_id')):
            raise ValueError('report identity mismatch; audit before debrief')
        retained = report.get('retained_check') or {}
        closing = report.get('closing_notes') or {}
        # local_completion above validates the receipt/hash and exact identity.
        # Read prose only from the acknowledged receipt, never reconstruct it.
        handoff_note = None
        if integrity['complete'] and closing.get('status') == 'acknowledged':
            handoff_note = load(path/task['id']/'closing-notes.json')['request']['note']
        rows.append(dict(worker=task['id'], address=task['address'], claim_id=state.get('claim_id'),
                         session_id=state.get('session_id'), model=task['model'],
                         outcome=report.get('outcome', state.get('outcome')), report_complete=integrity['complete'],
                         verification=verified.get(task['id'], 'unknown_without_fresh_status'),
                         retained_check_id=retained.get('check_id'), ndiff_insn=retained.get('ndiff_insn'),
                         extent=retained.get('extent'), diagnostic=retained.get('diagnostic'),
                         near_miss=(report.get('near_miss') or {}).get('status'),
                         measured_near_miss=isinstance(retained.get('ndiff_insn'), int) and
                                            0 < retained['ndiff_insn'] <= 40 and retained.get('valid') is True,
                          permuter_runs=len(report.get('permute_runs') or []),
                          handoff_note=handoff_note, remaining=report.get('remaining', [])))
    result = dict(batch=name, generated_utc=now(), workers=rows,
                  counts=dict(assignments=len(rows), reports_complete=sum(r['report_complete'] for r in rows),
                              submitted=sum(r['outcome'] == 'submitted' for r in rows),
                              given_up=sum(r['outcome'] == 'given_up' for r in rows),
                              verified=sum(r['verification'] == 'verified' for r in rows),
                              near_misses=sum(r['near_miss'] == 'accepted' for r in rows),
                              measured_near_misses=sum(r['measured_near_miss'] for r in rows)),
                  audit_ok=audited['ok'] if audited else None,
                  usage_note='Raw input, output, reasoning and cache counters are not assumed additive; billing unknown.')
    save(path/'debrief.json', result)
    return result


def recover(name, actions_path=None, apply=False, api=None):
    import recovery
    import matchflow as ledger
    from transport import request
    api = api or request
    path = batch_path(name)
    if apply:
        if not actions_path:
            raise ValueError('--apply requires an explicit reviewed --actions file')
        return recovery.apply(path, load(actions_path), db_path=ledger.DEFAULT_DB, api=api)
    if actions_path:
        raise ValueError('--actions requires --apply; default recover is inventory only')
    return recovery.inventory(path, api('status', {}), db_path=ledger.DEFAULT_DB)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('batch'); p.add_argument('--selection', required=True)
    p.add_argument('--model', help='provider/model[#variant]; overrides local worker_model, then built-in default')
    p = sub.add_parser('headless'); p.add_argument('batch'); p.add_argument('--status', required=True); p.add_argument('--jobs', type=int, required=True)
    p = sub.add_parser('audit'); p.add_argument('batch'); p.add_argument('--status', required=True)
    p.add_argument('--expect', help='Reviewed JSON with batch and exact worker/address/claim_id/session_id identities')
    p = sub.add_parser('audit-worker'); p.add_argument('batch'); p.add_argument('--status', required=True)
    for key in ('worker', 'address', 'claim-id', 'session-id'):
        p.add_argument('--'+key, required=True)
    p = sub.add_parser('debrief'); p.add_argument('batch'); p.add_argument('--status')
    p = sub.add_parser('dispatch'); p.add_argument('batch'); p.add_argument('--method', choices=['workflow', 'subagents'], required=True); p.add_argument('--status', required=True)
    p = sub.add_parser('monitor'); p.add_argument('batch')
    p = sub.add_parser('help'); p.add_argument('batch'); p.add_argument('--worker')
    p = sub.add_parser('answer'); p.add_argument('batch'); p.add_argument('--worker', required=True)
    p.add_argument('--text', required=True); p.add_argument('--request-id')
    p = sub.add_parser('recover'); p.add_argument('batch'); p.add_argument('--actions'); p.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            print(json.dumps(prepare(args.batch, args.selection, args.model), indent=2))
        elif args.command == 'headless':
            headless(args.batch, args.status, args.jobs)
        elif args.command == 'dispatch':
            print(json.dumps(dispatch(args.batch, args.method, args.status), indent=2))
        elif args.command == 'monitor':
            print(json.dumps(monitor(args.batch), indent=2))
        elif args.command == 'help':
            print(json.dumps(help_requests(args.batch, args.worker), indent=2))
        elif args.command == 'answer':
            print(json.dumps(answer(args.batch, args.worker, args.text, args.request_id), indent=2))
        elif args.command == 'recover':
            print(json.dumps(recover(args.batch, args.actions, args.apply), indent=2))
        elif args.command == 'debrief':
            print(json.dumps(debrief(args.batch, args.status), indent=2))
        else:
            expected = (dict(batch=args.batch, workers=[dict(worker=args.worker, address=args.address,
                        claim_id=args.claim_id, session_id=args.session_id)]) if args.command == 'audit-worker'
                        else load(args.expect) if args.expect else None)
            result = audit(args.batch, args.status, expected)
            print(json.dumps(result, indent=2))
            if not result['ok']:
                parser.exit(2, 'Audit requires reconciliation or evidence repair.\n')
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
