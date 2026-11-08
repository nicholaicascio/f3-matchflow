"""Shared fixed-address assignment pipeline. No service writes or credentials."""
import argparse
import concurrent.futures as futures
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import sys
import uuid

import lifecycle
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
                'open_problem_reports', 'file_problem_reports', 'lane', 'reason'} <= target.keys():
            raise ValueError('Selection is missing current function_info fields or review annotations')
        address = target['address']
        if not re.fullmatch(r'0x[0-9a-f]{8}', address) or address in seen:
            raise ValueError('Use unique canonical 0x addresses, not bands')
        seen.add(address)
        if address == '0x00ad1250':
            raise ValueError('User-excluded target; choose another function')
        if target.get('state') != 'open' or target.get('co_workers', 0):
            raise ValueError('Only currently open, unheld functions may be dispatched')
        if target.get('lane') not in ('production', 'research', 'integration') or not target.get('reason'):
            raise ValueError('Each selection needs a reviewed lane and reason')
        if not isinstance(target.get('size'), int) or target['size'] <= 0:
            raise ValueError('Missing function size')
        if target['lane'] == 'production' and (target.get('give_ups', 0) >= 3 or
                target.get('open_problem_reports', 0) or target.get('file_problem_reports', 0) or
                target.get('prior_unfit')):
            raise ValueError('Recurring blockers/reports/unfit are not production assignments')
        if target.get('prior_unfit') and target['lane'] != 'integration':
            raise ValueError('Prior unfit requires integration lane')
        verdicts = target.get('prior_verdicts')
        if not isinstance(verdicts, list) or target.get('verdict_history') not in ('reviewed', 'unknown'):
            raise ValueError('prior_verdicts list and verdict_history reviewed/unknown required')
        if target.get('history_reviewed') is not True or not target.get('history_sources'):
            raise ValueError('review history and name its evidence sources before dispatch')
        if 'unfit' in verdicts and target['lane'] != 'integration':
            raise ValueError('Prior unfit requires integration lane')
        if target['lane'] == 'production' and target['verdict_history'] == 'unknown':
            raise ValueError('unknown verdict history is not a clean production assignment')
        if target['lane'] == 'research' and not all(
                isinstance(target.get(k), str) and len(target[k].strip()) >= 20
                for k in ('new_hypothesis', 'expected_observation')):
            raise ValueError('research needs a specific new_hypothesis and expected_observation')
    return targets


def batch_path(name):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('Batch name must be a short lowercase slug')
    return BATCHES / name


def prompt_for(assignment):
    folder, worker = assignment.parent.parent.as_posix(), assignment.parent.name
    prompt = (f'Read {ROOT.as_posix()}/AGENTS.md, tools/matchflow/README.md and {assignment.as_posix()}. '
            f'Work from {ROOT.as_posix()}. Execute only that fixed-address assignment; no delegation/refills. '
            f'Lifecycle is already dispatched by the orchestrator. First run python tools/matchflow/lifecycle.py '
            f'guard "{folder}" {worker}. Claim ONCE through python tools/matchflow/service.py claim '
            f'"{folder}" {worker}, never through direct MCP/REST claim calls. Never reclaim after release. '
            'Read its saved claim-response.json for pack/history; register the claim in Matchflow with '
            f'--assignment "{assignment.as_posix()}". Use managed Matchflow checks only, not direct compiler/checker '
            'bypasses. Read lifecycle guard before each new hypothesis; CLOSE_REQUESTED.json means freeze and close. '
            'Do not infer elapsed time from service timestamps or write your own ownership/start times. '
            'Use service.py giveup with --source and --notes, service.py submit after accepted final MATCH/promotion, '
            'or service.py flag-proof after an exact diagnostic MATCH promoted to proof_ready. '
            'Do not pass note-only checkpoint IDs for best code. Save required closing work notes through official '
            'checkpoint after closure. Generate report with python tools/matchflow/evidence.py report '
            f'"{folder}" {worker}. If a call fails ambiguously, do not retry mutation: preserve evidence and '
            'report cleanup_required for the orchestrator. If prior unfit is discovered, stop and ask orchestrator '
            'to reclassify the owned assignment to integration before submitting. '
            'Final response: one RESULT line with address, outcome, report path, claim ID and cleanup_required.')
    prompt = re.sub(r'python tools/matchflow/([a-z]+\.py)',
                    lambda match: 'python "' + (CODE / match.group(1)).as_posix() + '"', prompt)
    return prompt.replace('tools/matchflow/README.md', (CODE / 'README.md').as_posix())


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
                target['prior_verdicts'] = sorted(set(target.get('prior_verdicts', []) + [r[0] for r in rows]))
    targets = validate_selection(selection)
    path = batch_path(name)
    path.mkdir(parents=True, exist_ok=False)
    tasks = []
    for i, target in enumerate(targets, 1):
        worker = path / f'worker{i:02}'
        worker.mkdir()
        assignment = worker / 'assignment.json'
        claim_body = dict(address=target['address'], cowork=False, context_mode='resume',
                          model=model.split('#')[0], harness='OpenCode')
        if '#' in model:
            claim_body['effort'] = model.split('#', 1)[1]
        save(assignment, dict(schema_version=2, worker=i, model=model, harness='OpenCode', target=target,
                             workspace=str(ROOT), output_dir=str(worker), selection_checked_at=selection['checked_at'],
                             claim_body=claim_body))
        prompt = prompt_for(assignment)
        (worker/'prompt.txt').write_text(prompt, encoding='utf-8')
        tasks.append(dict(id=worker.name, address=target['address'], model=model, prompt=prompt))
    manifest = dict(schema_version=2, batch=name, created=now(), model=model, selection=selection, tasks=tasks)
    save(path/'batch.json', manifest)
    save(path/'workflow-args.json', dict(schema_version=2, batch=name, lifecycle_dispatch_required=True, tasks=tasks))
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


def bundled_cli():
    path = Path(os.environ['LOCALAPPDATA']) / 'Programs/@openchamberelectron/resources/opencode-cli/opencode.exe'
    if not path.is_file():
        raise ValueError('Bundled CLI not found: discover the active OpenChamber installation; do not use PATH opencode')
    return path


def dispatch(name, method, status_path):
    path = batch_path(name)
    manifest = load(path / 'batch.json')
    if manifest.get('schema_version') != 2:
        raise ValueError('legacy batch cannot launch; prepare reviewed v2 assignments')
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
               (lifecycle.phase(s) != 'experiment' or (path/t['id']/'CLOSE_REQUESTED.json').exists() or (path/'STOP').exists())]
        if due:
            return dict(state='closure_requested', workers=due, cleanup_required=True)
        time.sleep(5)


def run_worker(path, task, cli):
    worker = path/task['id']
    if (path/'STOP').exists():
        return dict(worker=task['id'], outcome='not_started', reason='STOP marker present')
    lock = worker/'started.json'
    with lock.open('x', encoding='utf-8') as f:
        json.dump(dict(started=now(), method='headless'), f)
    lifecycle.require_experiment(path, task['id'])
    started = time.monotonic()
    run = dict(worker=task['id'], address=task['address'], started=now(), model=task['model'], sessions=[], results=[])
    out = (worker/'events.jsonl').open('w', encoding='utf-8')
    err = (worker/'stderr.log').open('w', encoding='utf-8')
    process = subprocess.Popen([str(cli), 'run', '-m', task['model'], '--format', 'json',
                                '--title', f"{path.name} {task['id']}", task['prompt']],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=err, text=True, encoding='utf-8', errors='replace')
    run['pid'] = process.pid
    save(worker/'run.json', run)

    def capture():
        try:
            for line in process.stdout:
                out.write(line)
                out.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                sid = event.get('sessionID')
                if sid and sid not in run['sessions']:
                    run['sessions'].append(sid)
                    lifecycle.bind(path, task['id'], sid)
                    save(worker/'run.json', run)
                if event.get('type') == 'error':
                    (path/'STOP').touch()
                part = event.get('part') or {}
                if part.get('type') == 'text':
                    run['results'] += [s for s in part.get('text', '').splitlines() if s.startswith('RESULT')]
        except Exception as exc:
            run['capture_error'] = str(exc)
            (path/'STOP').touch()
        finally:
            out.close()

    reader = threading.Thread(target=capture, daemon=True)
    reader.start()
    try:
        state = lifecycle.snapshot(path, task['id'])
        remaining = max(0, state['closure_seconds'] - (time.monotonic()-state['monotonic_start']))
        run['exit_code'] = process.wait(timeout=remaining)
        reader.join(timeout=5)
    except subprocess.TimeoutExpired:
        run['timeout'] = True
        run['warning'] = 'Reconcile session/owned claim now. CLI not killed; server session may still be active.'
    run.update(elapsed_seconds=time.monotonic()-started, cleanup_required=True)
    # Conservative until the orchestrator verifies official status; exit 0 is not closure.
    save(worker/'run.json', run)
    err.close()
    # Same raw-counter collector used by the other two transports. Missing or
    # malformed events remain explicit errors, never fabricated zero usage.
    from usage import headless_events
    try:
        save(worker/'usage-raw.json', headless_events(worker/'events.jsonl'))
    except (ValueError, KeyError, OSError) as exc:
        save(worker/'usage-raw.json', dict(raw=None, error=str(exc)))
    return run


def headless(name, status_path, jobs):
    path = batch_path(name)
    manifest = load(path/'batch.json')
    validate_selection(manifest['selection'])
    capacity(load(status_path), len(manifest['tasks']))
    if jobs <= 0:
        raise ValueError('jobs must be positive')
    cli = bundled_cli()
    dispatch(name, 'headless', status_path)
    with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = [pool.submit(run_worker, path, task, cli) for task in manifest['tasks']]
        for task in futures.as_completed(pending):
            try:
                result = task.result()
            except Exception as exc:
                result = dict(error=str(exc), cleanup_required=True)
            print(json.dumps(result), flush=True)
    print('Reconcile this batch against fresh official status; no automatic remote cleanup or retries.', flush=True)


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('batch'); p.add_argument('--selection', required=True); p.add_argument('--model', default=DEFAULT_MODEL)
    p = sub.add_parser('headless'); p.add_argument('batch'); p.add_argument('--status', required=True); p.add_argument('--jobs', type=int, required=True)
    p = sub.add_parser('audit'); p.add_argument('batch'); p.add_argument('--status', required=True)
    p = sub.add_parser('dispatch'); p.add_argument('batch'); p.add_argument('--method', choices=['workflow', 'subagents'], required=True); p.add_argument('--status', required=True)
    p = sub.add_parser('monitor'); p.add_argument('batch')
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
        else:
            result = audit(args.batch, args.status)
            print(json.dumps(result, indent=2))
            if not result['ok']:
                parser.exit(2, 'Audit requires reconciliation or evidence repair.\n')
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
