"""Trusted streaming discovery gateway. Network mutations have durable reservations.

The immutable batch header freezes model/configuration; append-only tasks live in
the lifecycle database so adding a task never invalidates a sibling's binding.
"""
import argparse
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import batch
import lifecycle as life
import matchflow as ledger
import review
import screen
import service
from config import worker_model, workspace_root
from security import private_root, scoped


def control(db):
    row = db.execute('SELECT payload FROM control WHERE id=1').fetchone()
    if not row:
        raise ValueError('stream not initialized')
    value = json.loads(row[0])
    if 'stream' not in value:
        raise ValueError('not a streaming run')
    return value


def put_control(db, value):
    db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(value),))


def guard(folder, value):
    if (Path(folder)/'STOP').exists() or value.get('cleanup_barrier') or value.get('start_barrier'):
        raise ValueError('run stopped; reconcile, never refill')
    heartbeat = value.get('heartbeat')
    if heartbeat is None or not 0 <= time.monotonic()-heartbeat <= 30:
        raise ValueError('supervisor missing/stale; no discovery claims or launches')
    producer = value['stream']
    if producer['producer_state'] == 'running' and not 0 <= time.monotonic()-producer['producer_heartbeat'] <= 30:
        raise ValueError('producer controller lost; no new claims or launches')
    # Unlike fresh preflight, this checks a frozen fingerprint without re-running tooling.
    from preflight import fingerprint
    if value['stream']['fingerprint'] != fingerprint():
        raise ValueError('configuration/tooling changed during stream; stop and reconcile')


def open_run(name, requested, parent, model=None, backlog=2):
    from broker import session_id, registry
    from preflight import require, fingerprint
    session_id(parent)
    if type(requested) is not int or not 1 <= requested <= 50 or type(backlog) is not int or not 1 <= backlog <= 4:
        raise ValueError('requested 1..50 and backlog 1..4 required')
    require()
    model = worker_model(model)
    with closing(registry()) as db:
        if db.execute('SELECT 1 FROM bindings WHERE session=?', (parent,)).fetchone():
            raise ValueError('workers cannot open streams')
    folder = private_root(batch.batch_path(name))
    folder.mkdir(exist_ok=False, parents=True)
    life.save(folder/'batch.json', dict(schema_version=3, batch=name, streaming=True,
              created=life.stamp(), model=model, tasks=[]))
    life.dispatch(folder, 'subagents')
    with life.transaction(folder) as db:
        db.execute('''CREATE TABLE stream_tasks(seq INTEGER PRIMARY KEY, worker TEXT UNIQUE,
            address TEXT UNIQUE, payload TEXT NOT NULL, state TEXT NOT NULL)''')
        db.execute('CREATE TABLE stream_info(address TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        value = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()[0])
        value['stream'] = dict(requested=requested, backlog=backlog, parent=parent,
             model=model, fingerprint=fingerprint(), producer_state='running',
             discovery_session=None, catalog_pages=0, cursor=None, inspections=0,
             started=False, claimed=0, producer_heartbeat=time.monotonic(),
             producer_started=time.monotonic())
        put_control(db, value)
    token = str(uuid.uuid4())
    life.tick(folder, token)
    with (folder/'watchdog.log').open('a', encoding='utf-8') as out:
        process = subprocess.Popen([sys.executable, str(Path(life.__file__)), 'watch', str(folder),
                                    '--token', token], stdout=out, stderr=out)
    life.save(folder/'launch.json', dict(method='subagents', watchdog_pid=process.pid))
    return dict(batch=name, requested=requested, model=model)


def folder_for(session, producer=False):
    from broker import registry, session_id
    session_id(session)
    with closing(registry()) as db:
        row = db.execute('SELECT folder,parent FROM discoveries WHERE session=?', (session,)).fetchone()
    if not row:
        raise ValueError('unbound discovery session')
    folder = private_root(row['folder'])
    with life.transaction(folder) as db:
        value = control(db)
        if value['stream']['discovery_session'] != session or value['stream']['parent'] != row['parent']:
            raise ValueError('discovery identity mismatch')
    return folder


def bind_discovery(name, session, parent):
    from broker import registry, session_id
    session_id(session); session_id(parent)
    if session == parent:
        raise ValueError('distinct discovery child required')
    folder = private_root(batch.batch_path(name))
    with life.transaction(folder) as db:
        value = control(db); guard(folder, value)
        stream = value['stream']
        if stream['parent'] != parent or stream['discovery_session'] is not None:
            raise ValueError('producer already bound or wrong parent; never relaunch')
        stream['discovery_session'] = session
        put_control(db, value)
    with closing(registry()) as db, db:
        db.execute('INSERT INTO discoveries VALUES(?,?,?)', (session, str(folder), parent))
    return dict(bound=True, requested=stream['requested'], model=stream['model'])


def status(folder):
    with life.transaction(folder) as db:
        value = control(db)
        tasks = [dict(r) for r in db.execute('SELECT seq,worker,address,state FROM stream_tasks ORDER BY seq')]
    return dict(batch=Path(folder).name, **value['stream'], tasks=tasks,
                stopped=bool(value.get('cleanup_barrier') or value.get('start_barrier') or (Path(folder)/'STOP').exists()))


def stop(folder, reason):
    with life.transaction(folder) as db:
        value = control(db)
        value['stream']['producer_state'] = 'failed'
        value.setdefault('start_barrier', dict(reason=reason, requested_utc=life.stamp()))
        put_control(db, value)
    (Path(folder)/'STOP').touch()


def discover(session, op, data, api=service.request):
    folder = folder_for(session)
    if op == 'status':
        return status(folder)
    if op == 'wait':
        # Bounded local database wait, no service polling or claim retry.
        until = time.monotonic()+60
        while True:
            with life.transaction(folder) as db:
                value = control(db); guard(folder,value)
                pending = db.execute("SELECT count(*) FROM stream_tasks WHERE state IN ('preparing','claiming','queued','launching')").fetchone()[0]
                in_flight = db.execute("SELECT count(*) FROM stream_tasks WHERE state IN ('preparing','claiming')").fetchone()[0]
                if value['stream']['producer_state'] != 'running' or (not in_flight and pending < value['stream']['backlog']):
                    return dict(producer_state=value['stream']['producer_state'], backpressure=False)
            if time.monotonic() >= until:
                return dict(backpressure=True, note='consumer has not freed backlog; wait again or finish')
            time.sleep(1)
    if op == 'finish':
        with life.transaction(folder) as db:
            value = control(db)
            if value['stream']['producer_state'] != 'running':
                return dict(producer_state=value['stream']['producer_state'])
            value['stream'].update(producer_state='done', finish_reason=str(data.get('reason', 'discovery exhausted'))[:2000])
            put_control(db, value)
        return dict(producer_state='done')
    with life.transaction(folder) as db:
        value = control(db); guard(folder, value)
        if value['stream']['producer_state'] != 'running':
            raise ValueError('producer finished; no more requests')
    try:
        if op == 'catalog':
            with life.transaction(folder) as db:
                value = control(db); guard(folder, value)
                stream = value['stream']
                if stream['catalog_pages'] >= 4 or (stream['catalog_pages'] and not stream['cursor']):
                    raise ValueError('bounded catalog exhausted; finish discovery')
                number = stream['catalog_pages'] + 1
                cursor = stream['cursor']
                # Reserve read too: a crashed producer cannot expand its search bound.
                stream['catalog_pages'] = number
                first = not stream['started']; stream['started'] = True
                put_control(db, value)
            if first:
                for tool in ('start_here', 'status', 'list_bands'):
                    response = api(tool, {})
                    life.save(folder/(tool+'.json'), dict(checked_at=life.stamp(), response=response))
            body = dict(order='give_ups', limit=500, include_held=False)
            if cursor:
                body['cursor'] = cursor
            result = api('list_functions', body)
            life.save(folder/('catalog-%d.json' % number), dict(checked_at=life.stamp(), request=body, response=result))
            with life.transaction(folder) as db:
                value = control(db); value['stream']['cursor'] = result.get('next'); put_control(db, value)
            return result
        if op == 'inspect':
            address = data.get('address')
            if not isinstance(address, str) or not batch.re.fullmatch(r'0x[0-9a-f]{8}', address):
                raise ValueError('canonical address required')
            with life.transaction(folder) as db:
                value = control(db); guard(folder, value)
                if not value['stream']['started'] or value['stream']['inspections'] >= value['stream']['requested']*4:
                    raise ValueError('start catalog first; bounded shortlist inspections exhausted')
                value['stream']['inspections'] += 1; put_control(db, value)
            info = api('function_info', dict(address=address))
            receipt = dict(checked_at=life.stamp(), response=info,
                local_history=screen.local_history(workspace_root()/'work/matchflow/ledger.sqlite3', address))
            with life.transaction(folder) as db:
                db.execute('INSERT OR REPLACE INTO stream_info VALUES(?,?)', (address, json.dumps(receipt)))
            return receipt
        if op == 'enqueue':
            return enqueue(folder, data, api)
        raise ValueError('unknown discovery operation')
    except service.ServiceError as exc:
        life.save(folder/'SERVICE_ERROR.json', dict(code=exc.code, kind=exc.kind, retry_after=exc.retry_after,
                                                   observed_utc=life.stamp()))
        stop(folder, 'service_error')
        raise


def enqueue(folder, data, api):
    address = data.get('address')
    with life.transaction(folder) as db:
        value = control(db); guard(folder, value)
        stream = value['stream']
        reserved = db.execute("SELECT count(*) FROM stream_tasks WHERE state IN ('preparing','claiming')").fetchone()[0]
        if stream['producer_state'] != 'running' or stream['claimed'] + reserved >= stream['requested']:
            raise ValueError('requested claims already secured or producer finished')
        if reserved:
            return dict(backpressure=True, note='a discovery claim is still pending; wait, never race or replay it')
        if db.execute("SELECT count(*) FROM stream_tasks WHERE state IN ('preparing','claiming','queued','launching')").fetchone()[0] >= stream['backlog']:
            return dict(backpressure=True, note='wait for orchestrator handoff; do not claim more')
        if db.execute('SELECT 1 FROM stream_tasks WHERE address=?', (address,)).fetchone():
            raise ValueError('candidate already reserved; never replay')
        row = db.execute('SELECT payload FROM stream_info WHERE address=?', (address,)).fetchone()
        if not row:
            raise ValueError('inspect candidate before reviewing')
        receipt = json.loads(row[0]); batch.fresh(receipt['checked_at'])
        target = dict(receipt['response'], reason=data.get('reason'), review=data.get('review'))
        # Merge actual known local integration failures, not just the annotation.
        known = [r.get('verdict') for r in receipt['local_history'].get('verdicts', []) if r.get('verdict')]
        if any(r.get('state') == 'unfit' for r in receipt['local_history'].get('attempts', [])):
            known.append('unfit')
        if isinstance(target['review'], dict):
            target['review']['prior_verdicts'] = sorted(set(target['review'].get('prior_verdicts', [])+known))
        batch.validate_selection(dict(checked_at=receipt['checked_at'], targets=[target]))
        seq = db.execute('SELECT coalesce(max(seq),0)+1 FROM stream_tasks').fetchone()[0]
        worker = 'worker%02d' % seq
        task = dict(id=worker, address=address, model=stream['model'], agent='match-worker')
        db.execute('INSERT INTO stream_tasks VALUES(?,?,?,?,?)', (seq,worker,address,json.dumps(task),'preparing'))
        now, mono = life.utc(), time.monotonic()
        state = dict(address=address, model=stream['model'], method='subagents', worker_start_utc=life.stamp(now),
            monotonic_start=mono, experiment_deadline_utc=life.stamp(now+timedelta(minutes=40)),
            closure_deadline_utc=life.stamp(now+timedelta(minutes=45)), experiment_seconds=2400, closure_seconds=2700,
            last_improvement_monotonic=mono, register_wall_monotonic=None, state='dispatched',
            claim_attempted=False, claim_id=None, session_id=None, cleanup_required=True,
            reviewed_utc=receipt['checked_at'])
        db.execute('INSERT INTO workers VALUES(?,?)', (worker,json.dumps(state)))
    try:
        root = scoped(folder, worker); root.mkdir(); (root/'files').mkdir()
        body = dict(address=address, cowork=False, context_mode='resume', model=task['model'].split('#')[0], harness='OpenCode')
        if '#' in task['model']:
            body['effort'] = task['model'].split('#',1)[1]
        assignment = dict(schema_version=3, worker=seq, model=task['model'], harness='OpenCode', target=target,
            workspace=str(workspace_root()), output_dir=str(root), selection_checked_at=receipt['checked_at'], claim_body=body)
        life.save(root/'assignment.json', assignment)
        task.update(assignment_sha256=ledger.digest(root/'assignment.json'), prompt=batch.prompt_for(root/'assignment.json'))
        (root/'prompt.txt').write_text(task['prompt'], encoding='utf-8')
        with life.transaction(folder) as db:
            db.execute("UPDATE stream_tasks SET payload=?,state='claiming' WHERE worker=?", (json.dumps(task),worker))
        life.setup_step(folder,worker,'claim')
        batch.capacity(dict(checked_at=life.stamp(),status=api('status',{})),1)
        result = service.claim(folder, worker, api=api)
        with life.transaction(folder) as db:
            value = control(db)
            blocked = result.get('outcome') == 'blocked'
            db.execute('UPDATE stream_tasks SET state=? WHERE worker=?', ('blocked' if blocked else 'queued',worker))
            if not blocked:
                value['stream']['claimed'] += 1
                if value['stream']['claimed'] == value['stream']['requested']:
                    value['stream']['producer_state'] = 'done'
                state = life.read_worker(db, worker)
                state['claim_ack_utc'] = life.stamp()
                life.write_worker(db, worker, state)
            put_control(db,value)
        if blocked:
            import evidence
            evidence.report(folder,worker,db_path=ledger.DEFAULT_DB)
        return dict(worker=worker, **result)
    except BaseException:
        # Includes response loss, startup interruption, disk-full and cancellation.
        # A reserved intent is never retried or automatically converted to a queue entry.
        life.request_cleanup(folder, worker, 'discovery_claim_failed')
        stop(folder, 'discovery_claim_failed')
        raise


def next_task(name, parent):
    folder = private_root(batch.batch_path(name))
    with life.transaction(folder) as db:
        value = control(db); guard(folder, value)
        if value['stream']['parent'] != parent:
            raise ValueError('wrong stream owner')
        row = db.execute("SELECT worker,payload FROM stream_tasks WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
        if not row:
            return dict(empty=True, producer_state=value['stream']['producer_state'])
        life.experiment_state(db,folder,row['worker'])
        db.execute("UPDATE stream_tasks SET state='launching' WHERE worker=?", (row['worker'],))
        state = life.read_worker(db,row['worker'])
        state['launch_reserved_monotonic'] = time.monotonic()
        life.write_worker(db,row['worker'],state)
        return dict(json.loads(row['payload']), batch=name, worker=row['worker'], preclaimed=True)


def supervise(db, folder):
    """Independent durable controller/launch leases, checked by host watchdog."""
    value = control(db)
    stream = value['stream']
    now = time.monotonic()
    reason = None
    if stream['producer_state'] == 'running':
        pulse = stream.get('producer_heartbeat')
        started = stream.get('producer_started')
        if pulse is not None and not 0 <= now-pulse <= 30:
            reason = 'producer_controller_lost'
        elif started is not None and not 0 <= now-started <= 2400:
            reason = 'producer_deadline'
    for row in db.execute("SELECT worker FROM stream_tasks WHERE state='launching'"):
        state = life.read_worker(db,row['worker'])
        reserved = state.get('launch_reserved_monotonic')
        if reserved is not None and not 0 <= now-reserved <= 120:
            reason = 'abandoned_launch_reservation'
    if reason:
        stream.update(producer_state='failed', failure_reason=reason)
        value.setdefault('start_barrier',dict(reason=reason,requested_utc=life.stamp()))
        put_control(db,value)
        (Path(folder)/'STOP').touch()
    return reason


def assigned(folder, worker):
    with life.transaction(folder) as db:
        row = db.execute('SELECT state FROM stream_tasks WHERE worker=?', (worker,)).fetchone()
        if not row or row[0] != 'launching':
            raise ValueError('handoff not reserved; no replay')
        state = life.read_worker(db,worker)
        if state.get('setup_state') != 'ready' or not state.get('session_id'):
            raise ValueError('handoff has no ready bound session')
        state['assigned_utc'] = life.stamp()
        state['review_to_claim_seconds'] = (life.timestamp(state['claim_ack_utc'])-life.timestamp(state['reviewed_utc'])).total_seconds()
        state['claim_to_worker_seconds'] = (life.timestamp(state['assigned_utc'])-life.timestamp(state['claim_ack_utc'])).total_seconds()
        life.write_worker(db,worker,state)
        db.execute("UPDATE stream_tasks SET state='assigned' WHERE worker=?", (worker,))


def unassigned_recovery(folder, worker, state):
    """Prove a stopped pre-handoff claim has never had a worker capability."""
    from broker import registry
    if not life.load(Path(folder)/'batch.json').get('streaming') or state.get('session_id') is not None:
        return False
    with life.transaction(folder) as db:
        value = control(db)
        row = db.execute('SELECT state FROM stream_tasks WHERE worker=?', (worker,)).fetchone()
        stopped = value.get('start_barrier') or value.get('cleanup_barrier') or (Path(folder)/'STOP').exists()
        if not stopped or not row or row[0] not in ('queued','launching','claiming','closed'):
            return False
    with closing(registry()) as db:
        return not db.execute('SELECT 1 FROM bindings WHERE folder=? AND worker=?', (str(Path(folder)),worker)).fetchone()
