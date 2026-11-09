"""Shared fixed-address assignment pipeline. No service writes or credentials."""
import argparse
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
from config import workspace_root

ROOT = workspace_root()
CODE = Path(__file__).resolve().parent
BATCHES = ROOT / 'work' / 'batches'
DEFAULT_MODEL = 'opencode-go/deepseek-v4.1-flash#max'


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
        if not {'address', 'size', 'state', 'co_workers', 'give_ups',
                'open_problem_reports', 'file_problem_reports', 'reason'} <= target.keys():
            raise ValueError('Selection is missing current function_info fields or review annotations')
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


def prepare(name, selection_path, model=DEFAULT_MODEL):
    if not re.fullmatch(r'[^\s/]+/[^\s]+', model):
        raise ValueError('Explicit provider/model[#variant] required')
    selection = load(selection_path)
    # Import only known local verdicts; absence is never evidence of clean history.
    ledger_path = ROOT / 'work' / 'matchflow' / 'ledger.sqlite3'
    if ledger_path.exists():
        import sqlite3
        with sqlite3.connect(ledger_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
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
    tasks = load(path/'batch.json')['tasks']
    while True:
        with lifecycle.transaction(path) as db:
            row = db.execute('SELECT payload FROM control WHERE id=1').fetchone()
            if not row:
                raise ValueError('dispatch before monitoring')
            control = json.loads(row['payload'])
            states = [lifecycle.read_worker(db, task['id']) for task in tasks]
        if all(s['state'] == 'closed' for s in states):
            return dict(state='all_closed', note='Audit acknowledgements/evidence against fresh official status.')
        heartbeat = control.get('heartbeat')
        if heartbeat is None or not 0 <= time.monotonic()-heartbeat <= 30:
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
    tasks = load(path/'batch.json')['tasks']
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
    if not any(t['id'] == worker for t in load(path/'batch.json')['tasks']):
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


def audit(name, status_path):
    path = batch_path(name)
    snapshot = load(status_path)
    fresh(snapshot['checked_at'])
    status = snapshot['status']
    opened = {r['claim_id'] for r in status['open_claims']}
    rows = []
    for task in load(path/'batch.json')['tasks']:
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
    return dict(ok=all(not r['cleanup_required'] and r['evidence_integrity'] == 'valid' for r in rows),
                workers=rows, unrelated_open_claims=sorted(opened-{r['claim_id'] for r in rows if r['claim_id']}),
                note='Never release unrelated claims; inspect missing ownership and verify checker/ack evidence separately')


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
    p = sub.add_parser('prepare'); p.add_argument('batch'); p.add_argument('--selection', required=True); p.add_argument('--model', default=DEFAULT_MODEL)
    p = sub.add_parser('headless'); p.add_argument('batch'); p.add_argument('--status', required=True); p.add_argument('--jobs', type=int, required=True)
    p = sub.add_parser('audit'); p.add_argument('batch'); p.add_argument('--status', required=True)
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
        else:
            result = audit(args.batch, args.status)
            print(json.dumps(result, indent=2))
            if not result['ok']:
                parser.exit(2, 'Audit requires reconciliation or evidence repair.\n')
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
