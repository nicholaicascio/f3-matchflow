"""Durable batch ownership and deadlines. No network access in this module."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

def utc():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utc()).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be a timezone-aware ISO string')
    # PowerShell emits seven fractional digits; datetime on older Python accepts six.
    value = re.sub(r'(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)', r'\1', value)
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp needs a timezone')
    return result.astimezone(timezone.utc)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def transaction(folder):
    db = sqlite3.connect(str(Path(folder) / 'lifecycle.sqlite3'), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute('CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS escalations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, worker TEXT NOT NULL,
            created TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS escalation_answers (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, worker TEXT NOT NULL,
            created TEXT NOT NULL, payload TEXT NOT NULL)''')
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def read_worker(db, worker):
    row = db.execute('SELECT payload FROM workers WHERE id=?', (worker,)).fetchone()
    if not row:
        raise ValueError('assignment not dispatched; orchestrator must dispatch once first')
    return json.loads(row['payload'])


def write_worker(db, worker, value):
    db.execute('UPDATE workers SET payload=? WHERE id=?', (json.dumps(value), worker))


def escalate(folder, worker, record):
    """Append one worker help request under the same transaction as its limit check."""
    with transaction(folder) as db:
        pending = db.execute("SELECT count(*) FROM escalations WHERE worker=? AND state='pending'",
                             (worker,)).fetchone()[0]
        if pending >= 5:
            raise ValueError('five help requests are already pending; wait for an answer')
        db.execute('INSERT INTO escalations(id,worker,created,state,payload) VALUES(?,?,?,?,?)',
                   (record['request_id'], worker, record['created_utc'], 'pending', json.dumps(record)))
    return record


def escalations(folder, worker=None):
    """Read requests/answers without a lost-update window."""
    with transaction(folder) as db:
        if worker is None:
            requests = db.execute('SELECT seq,worker,state,payload FROM escalations ORDER BY seq').fetchall()
            answers = db.execute('SELECT seq,worker,payload FROM escalation_answers ORDER BY seq').fetchall()
        else:
            requests = db.execute('SELECT seq,worker,state,payload FROM escalations WHERE worker=? ORDER BY seq',
                                  (worker,)).fetchall()
            answers = db.execute('SELECT seq,worker,payload FROM escalation_answers WHERE worker=? ORDER BY seq',
                                 (worker,)).fetchall()
    value = [dict(json.loads(row['payload']), worker=row['worker'], state=row['state'])
             for row in requests]
    responses = [dict(json.loads(row['payload']), worker=row['worker']) for row in answers]
    return dict(requests=value, responses=responses)


def answer_escalation(folder, worker, text, request_id=None):
    """Atomically select one pending request, answer it and append the response."""
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 8000:
        raise ValueError('help answer needs substantive bounded text')
    with transaction(folder) as db:
        state = read_worker(db, worker)
        if state.get('state') != 'claimed':
            raise ValueError('worker is not active; answering a closed assignment is meaningless')
        if request_id is None:
            row = db.execute("SELECT seq,id,payload FROM escalations WHERE worker=? AND state='pending' "
                             "ORDER BY seq LIMIT 1", (worker,)).fetchone()
        else:
            row = db.execute("SELECT seq,id,payload FROM escalations WHERE worker=? AND id=? AND state='pending'",
                             (worker, request_id)).fetchone()
        if not row:
            raise ValueError('no matching pending help request; do not guess or replay an answer')
        response = dict(request_id=row['id'], text=text.strip(), answered_utc=stamp())
        request = json.loads(row['payload'])
        request.update(state='answered', answered_utc=response['answered_utc'])
        db.execute("UPDATE escalations SET state='answered', payload=? WHERE seq=?",
                   (json.dumps(request), row['seq']))
        db.execute('INSERT INTO escalation_answers(request_id,worker,created,payload) VALUES(?,?,?,?)',
                   (row['id'], worker, response['answered_utc'], json.dumps(response)))
    return response


def dispatch(folder, method):
    folder = Path(folder)
    if method not in ('workflow', 'subagents', 'headless'):
        raise ValueError('unknown dispatch method')
    manifest = load(folder / 'batch.json')
    wall, mono = utc(), time.monotonic()
    with transaction(folder) as db:
        if db.execute('SELECT 1 FROM control').fetchone():
            raise ValueError('batch already dispatched; reconcile, never replay')
        control = dict(method=method, dispatched_utc=stamp(wall), monotonic_start=mono,
                       watcher=None, heartbeat=None)
        db.execute('INSERT INTO control VALUES(1,?)', (json.dumps(control),))
        for task in manifest['tasks']:
            state = dict(address=task['address'], model=task['model'], method=method,
                         worker_start_utc=stamp(wall), monotonic_start=mono,
                         experiment_deadline_utc=stamp(wall + timedelta(minutes=40)),
                         closure_deadline_utc=stamp(wall + timedelta(minutes=45)),
                         experiment_seconds=2400, closure_seconds=2700,
                         last_improvement_monotonic=mono, register_wall_monotonic=None,
                         state='dispatched', claim_attempted=False, claim_id=None,
                         session_id=None, cleanup_required=True)
            db.execute('INSERT INTO workers VALUES(?,?)', (task['id'], json.dumps(state)))
    return control


def snapshot(folder, worker):
    with transaction(folder) as db:
        return read_worker(db, worker)


def bind(folder, worker, session):
    if not re.fullmatch(r'ses_[A-Za-z0-9]+', session):
        raise ValueError('real session ID required')
    with transaction(folder) as db:
        state = read_worker(db, worker)
        if state['session_id'] not in (None, session):
            raise ValueError('assignment already bound to a different session')
        state['session_id'] = session
        write_worker(db, worker, state)
    return state


def phase(state):
    elapsed = time.monotonic() - state['monotonic_start']
    if elapsed < 0:
        return 'reconcile'  # Reboot/clock discontinuity: never reset the start.
    wall = utc()
    stop = experiment_limit(state)
    if elapsed >= min(state['closure_seconds'], stop + 300) or wall >= timestamp(state['closure_deadline_utc']):
        return 'reconcile'
    if elapsed >= stop or wall >= timestamp(state['experiment_deadline_utc']):
        return 'closure'
    return 'experiment'


def experiment_limit(state):
    limits = [state['experiment_seconds'],
              state['last_improvement_monotonic'] - state['monotonic_start'] + 1800]
    if state['register_wall_monotonic'] is not None:
        limits.append(state['register_wall_monotonic'] - state['monotonic_start'] + 1200)
    return min(limits)


def remaining_experiment_seconds(state):
    """Measured time left, never a worker estimate."""
    return max(0.0, experiment_limit(state) - (time.monotonic() - state['monotonic_start']))


def voluntary_experiment(folder, worker, state):
    """True only while the worker may still run experiments by its own choice.

    Use the same control facts as experiment admission: a stale/missing watchdog
    and the durable cleanup barrier are forced closure even when experiment time
    remains and no STOP marker exists.  Otherwise a voluntary near-miss review
    would obstruct urgent cleanup.
    """
    if state.get('state') != 'claimed' or phase(state) != 'experiment' or state.get('closure_requested'):
        return False
    if (Path(folder) / 'STOP').exists() or (Path(folder) / worker / 'CLOSE_REQUESTED.json').exists():
        return False
    with transaction(folder) as db:
        row = db.execute('SELECT payload FROM control WHERE id=1').fetchone()
        if not row:
            return False
        control = json.loads(row['payload'])
        if control.get('cleanup_barrier'):
            return False
        heartbeat = control.get('heartbeat')
        if heartbeat is None or not 0 <= time.monotonic() - heartbeat <= 30:
            return False
    return True


def progress(folder, worker, flags=None, metric=None, register_wall=False, evidence=None):
    with transaction(folder) as db:
        state = read_worker(db, worker)
        if state['state'] != 'claimed' or phase(state) != 'experiment':
            raise ValueError('cannot reset progress after stopping or closure')
        if register_wall:
            if not evidence:
                raise ValueError('register-wall classification needs evidence')
            if state['register_wall_monotonic'] is None:
                state['register_wall_monotonic'] = time.monotonic()
                state['register_wall_evidence'] = evidence
        if type(metric) is int and metric >= 0:
            key = json.dumps(flags)
            previous = state.setdefault('best_metrics', {}).get(key)
            if previous is not None and metric < previous:
                state['last_improvement_monotonic'] = time.monotonic()
            if previous is None or metric < previous:
                state['best_metrics'][key] = metric
        write_worker(db, worker, state)
    return state


def experiment_state(db, folder, worker):
    state = read_worker(db, worker)
    control = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
    if control.get('cleanup_barrier'):
        raise ValueError('batch cleanup barrier active; reconcile, no new starts or checks')
    if state.get('closure_requested'):
        raise ValueError('worker closure requested; no more experiments')
    heartbeat = control.get('heartbeat')
    if heartbeat is None or not 0 <= time.monotonic() - heartbeat <= 30:
        raise ValueError('watchdog missing/stale; closure only, do not resume experiments')
    if state['state'] not in ('dispatched', 'claimed') or phase(state) != 'experiment':
        raise ValueError('assignment is closed, ambiguous or out of experiment time')
    if (Path(folder) / worker / 'CLOSE_REQUESTED.json').exists():
        raise ValueError('closure requested; no more experiments')
    if (Path(folder) / 'STOP').exists():
        raise ValueError('batch STOP requested; closure only')
    return state


def require_experiment(folder, worker):
    with transaction(folder) as db:
        return experiment_state(db, folder, worker)


def require_start(folder, worker):
    with transaction(folder) as db:
        control = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
        if control.get('start_barrier'):
            raise ValueError('new starts blocked pending worker reconciliation; existing healthy workers may finish')
        return experiment_state(db, folder, worker)


def request_worker_cleanup(folder, worker, reason):
    """A completed ready session cannot run more checks; siblings keep their budgets."""
    folder = Path(folder)
    with transaction(folder) as db:
        state = read_worker(db, worker)
        state.update(closure_requested=True, cleanup_required=True)
        state.setdefault('completion_failure', dict(reason=reason, requested_utc=stamp()))
        write_worker(db, worker, state)
        control = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
        control.setdefault('start_barrier', dict(worker=worker, reason=reason, requested_utc=stamp()))
        db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(control),))
    save(folder/worker/'CLOSE_REQUESTED.json', dict(worker=worker, reason=reason,
         claim_id=state['claim_id'], session_id=state['session_id'], cleanup_required=True))
    return dict(stopped=True, scope='worker', cleanup_required=True, worker=worker)


def setup_step(folder, worker, stage):
    if stage not in ('preflight', 'capacity', 'claim', 'packet', 'pack', 'register', 'ready'):
        raise ValueError('unknown host setup stage')
    with transaction(folder) as db:
        state = read_worker(db, worker)
        state.update(setup_stage=stage, setup_state='ready' if stage == 'ready' else 'pending')
        write_worker(db, worker, state)


def request_cleanup(folder, worker, reason):
    """Durable host failure barrier; STOP is a wake-up marker, not the authority."""
    folder = Path(folder)
    with transaction(folder) as db:
        state = read_worker(db, worker)
        failure = state.get('setup_failure') or dict(stage=state.get('setup_stage', 'launch'),
                                                    failed_utc=stamp(), reason=reason)
        state.update(setup_failure=failure, setup_state='failed', cleanup_required=True)
        write_worker(db, worker, state)
        control = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
        control.setdefault('cleanup_barrier', dict(worker=worker, session_id=state['session_id'],
                                                  claim_id=state['claim_id'], **failure))
        db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(control),))
    (folder/'STOP').touch()
    save(folder/worker/'SETUP_FAILED.json', failure)
    return dict(stopped=True, cleanup_required=True, worker=worker, failure=failure)


def reserve_claim(folder, worker):
    with transaction(folder) as db:
        control = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
        if control.get('start_barrier'):
            raise ValueError('new starts blocked pending worker reconciliation')
        state = experiment_state(db, folder, worker)
        if state['claim_attempted'] or state['state'] != 'dispatched':
            raise ValueError('one claim attempt per assignment; reconcile, never retry')
        state.update(claim_attempted=True, state='claim_pending')
        write_worker(db, worker, state)
    return state


def claimed(folder, worker, claim, address, deadline):
    with transaction(folder) as db:
        state = read_worker(db, worker)
        if state['state'] != 'claim_pending' or state['claim_id']:
            raise ValueError('no reserved claim attempt')
        if address != state['address'] or not re.fullmatch(r'clm_[A-Za-z0-9]+', claim):
            raise ValueError('service claim differs from fixed assignment; reconcile response')
        # Experiments stop with twenty minutes lease margin; closure retains fifteen.
        lease = timestamp(deadline)
        start = timestamp(state['worker_start_utc'])
        stop = min(timestamp(state['experiment_deadline_utc']), lease - timedelta(minutes=20))
        close = min(timestamp(state['closure_deadline_utc']), lease - timedelta(minutes=15))
        state.update(claim_id=claim, deadline_utc=stamp(lease), state='claimed',
                     experiment_deadline_utc=stamp(stop), closure_deadline_utc=stamp(close),
                     experiment_seconds=min(state['experiment_seconds'], (stop-start).total_seconds()),
                     closure_seconds=min(state['closure_seconds'], (close-start).total_seconds()))
        write_worker(db, worker, state)
    return state


def begin_close(folder, worker):
    with transaction(folder) as db:
        state = read_worker(db, worker)
        if state['state'] != 'claimed' or not state['claim_id']:
            raise ValueError('claim not owned or closure already attempted; reconcile without retry')
        state['state'] = 'close_pending'
        write_worker(db, worker, state)
    return state


def closed(folder, worker, outcome, ack_path, handoff, *, closed_utc=None):
    with transaction(folder) as db:
        state = read_worker(db, worker)
        if state['state'] != 'close_pending':
            raise ValueError('no pending closure')
        observed = timestamp(closed_utc) if closed_utc is not None else utc()
        if not timestamp(state['worker_start_utc']) <= observed <= utc():
            raise ValueError('closure timestamp is outside the assignment lifetime')
        state.update(state='closed', outcome=outcome, service_ack_path=str(ack_path),
                     handoff=handoff, cleanup_required=False, closed_utc=stamp(observed),
                     elapsed_seconds=((observed-timestamp(state['worker_start_utc'])).total_seconds()
                                      if closed_utc is not None else max(0, time.monotonic()-state['monotonic_start'])))
        write_worker(db, worker, state)
    save(Path(folder) / worker / 'ownership.json', state)
    return state


def blocked(folder, worker, reason):
    if not reason.strip():
        raise ValueError('blocked reason required')
    with transaction(folder) as db:
        state = read_worker(db, worker)
        if state['claim_attempted'] or state['state'] != 'dispatched':
            raise ValueError('cannot call an attempted/ambiguous claim not-started')
        state.update(state='closed', outcome='blocked', closed_utc=stamp(),
                     blocked_reason=reason, cleanup_required=False, handoff='not_applicable',
                     elapsed_seconds=max(0, time.monotonic()-state['monotonic_start']))
        write_worker(db, worker, state)
    save(Path(folder) / worker / 'ownership.json', state)
    return state


def tick(folder, watcher):
    folder = Path(folder)
    notifications = []
    with transaction(folder) as db:
        row = db.execute('SELECT payload FROM control WHERE id=1').fetchone()
        if not row:
            raise ValueError('batch not dispatched')
        control = json.loads(row['payload'])
        # Never resume a lost supervisor into experiments after a crash/restart.
        if control['watcher'] not in (None, watcher):
            raise ValueError('watchdog already assigned; reconcile lost supervisor, do not restart')
        control.update(watcher=watcher, heartbeat=time.monotonic())
        db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(control),))
        for row in db.execute('SELECT id,payload FROM workers').fetchall():
            state = json.loads(row['payload'])
            if state['state'] == 'closed':
                continue
            current = phase(state)
            if current != 'experiment':
                message = dict(phase=current, requested_utc=stamp(), claim_id=state['claim_id'],
                               session_id=state['session_id'], cleanup_required=True)
                marker = folder / row['id'] / 'CLOSE_REQUESTED.json'
                if not marker.exists():
                    save(marker, message)
                    notifications.append(dict(worker=row['id'], **message))
                if current == 'reconcile' and not state.get('escalated'):
                    state['escalated'] = True
                    write_worker(db, row['id'], state)
                    notifications.append(dict(worker=row['id'], **message))
    if notifications:
        path = folder / 'RECONCILE_REQUIRED.json'
        previous = load(path)['events'] if path.exists() else []
        save(path, dict(events=previous + notifications, observed_utc=stamp()))
    return notifications


def watch(folder, watcher=None):
    watcher = watcher or str(uuid.uuid4())
    while True:
        notices = tick(folder, watcher)
        for notice in notices:
            print(json.dumps(notice), flush=True)
        with transaction(folder) as db:
            states = [json.loads(r[0]) for r in db.execute('SELECT payload FROM workers')]
        if all(s['state'] == 'closed' or s.get('escalated') for s in states):
            return
        time.sleep(5)  # Local deadline watchdog, not polling remote workers/service.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('watch'); p.add_argument('folder'); p.add_argument('--token')
    for name in ('status', 'bind', 'guard'):
        p = sub.add_parser(name); p.add_argument('folder'); p.add_argument('worker')
        if name == 'bind':
            p.add_argument('session')
    p = sub.add_parser('register-wall'); p.add_argument('folder'); p.add_argument('worker'); p.add_argument('--evidence', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'watch':
            watch(args.folder, args.token)
        else:
            result = (progress(args.folder, args.worker, register_wall=True, evidence=args.evidence)
                      if args.command == 'register-wall' else
                      bind(args.folder, args.worker, args.session) if args.command == 'bind' else
                      require_experiment(args.folder, args.worker) if args.command == 'guard' else
                      snapshot(args.folder, args.worker))
            print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
