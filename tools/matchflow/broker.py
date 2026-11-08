"""Trusted host broker. Only the OpenCode plugin supplies session identity.

CLI is a host integration entry point, NOT a worker shell command. Untrusted tool
inputs never select a session, claim, batch, database, executable or credential.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

import batch
import evidence
import lifecycle as life
import matchflow as ledger
import service
from config import workspace_root, REPOSITORY, runtime
from security import private_root, scoped, no_links
from transport import ServiceError

MAX_TEXT = 256 * 1024
SAFE_FLAGS = {f.lower() for f in (
    '/MT', '/MTd', '/MD', '/MDd', '/O1', '/O2', '/Od', '/Ox', '/Ob0', '/Ob1', '/Ob2',
    '/Oi', '/Oi-', '/Os', '/Ot', '/Oy', '/Oy-', '/GS', '/GS-', '/EHsc', '/EHa', '/Gy',
    '/Gy-', '/GR', '/GR-', '/GL', '/GL-', '/Gd', '/Gr', '/Gz', '/fp:precise', '/fp:fast',
    '/fp:strict', '/arch:SSE', '/arch:SSE2', '/arch:IA32', '/Zc:wchar_t', '/Zc:wchar_t-',
    '/Zc:forScope', '/Zc:forScope-')}


def registry():
    root = private_root(workspace_root()/'work/matchflow')
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root/'bindings.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS bindings (
        session TEXT PRIMARY KEY, parent TEXT NOT NULL, folder TEXT NOT NULL,
        worker TEXT NOT NULL, assignment_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL,
        UNIQUE(folder, worker))''')
    return db


def session_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'ses_[A-Za-z0-9]+', value):
        raise ValueError('real session identity required')
    return value


def bind(name, worker, session, parent):
    session_id(session); session_id(parent)
    if session == parent or not re.fullmatch(r'worker[0-9]{2,3}', worker):
        raise ValueError('distinct child and canonical worker required')
    folder = private_root(batch.batch_path(name))
    manifest = life.load(folder/'batch.json')
    if manifest.get('schema_version') != 3:
        raise ValueError('only reviewed v3 batches can bind')
    task = next((t for t in manifest['tasks'] if t['id'] == worker), None)
    if not task or task.get('agent') != 'match-worker':
        raise ValueError('dedicated matching subagent required')
    assignment = scoped(folder, worker+'/assignment.json', exists=True)
    if ledger.digest(assignment) != task['assignment_sha256']:
        raise ValueError('assignment changed after preparation')
    state = life.require_experiment(folder, worker)
    if state['method'] != 'subagents':
        raise ValueError('only restricted subagents enabled')
    with closing(registry()) as db, db:
        if db.execute('SELECT 1 FROM bindings WHERE session=?', (parent,)).fetchone():
            raise ValueError('a worker cannot launch children')
        db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,?)',
                   (session, parent, str(folder), worker, ledger.digest(assignment), ledger.digest(folder/'batch.json')))
    life.bind(folder, worker, session)
    return dict(bound=True, address=state['address'], model=task['model'], prompt=task['prompt'])


def task(name, worker, parent):
    session_id(parent)
    from preflight import require
    require()
    if not re.fullmatch(r'worker[0-9]{2,3}', worker):
        raise ValueError('canonical worker required')
    with closing(registry()) as db:
        if db.execute('SELECT 1 FROM bindings WHERE session=?', (parent,)).fetchone():
            raise ValueError('workers cannot launch assignments')
    folder = private_root(batch.batch_path(name))
    manifest = life.load(folder/'batch.json')
    batch.validate_selection(manifest['selection'])
    if manifest.get('schema_version') != 3:
        raise ValueError('prepare a v3 batch')
    target = next((t for t in manifest['tasks'] if t['id'] == worker), None)
    if not target or target.get('agent') != 'match-worker':
        raise ValueError('unknown restricted assignment')
    if ledger.digest(scoped(folder, worker+'/assignment.json', exists=True)) != target['assignment_sha256']:
        raise ValueError('assignment changed')
    life.require_experiment(folder, worker)
    return target


def binding(session):
    with closing(registry()) as db:
        row = db.execute('SELECT * FROM bindings WHERE session=?', (session_id(session),)).fetchone()
    if not row:
        raise ValueError('unbound session; parent must launch a prepared assignment')
    row = dict(row)
    folder = private_root(row['folder'])
    assignment = scoped(folder, row['worker']+'/assignment.json', exists=True)
    if (ledger.digest(assignment) != row['assignment_hash'] or
            ledger.digest(folder/'batch.json') != row['manifest_hash']):
        raise ValueError('immutable assignment/manifest changed; parent must reconcile')
    state = life.snapshot(folder, row['worker'])
    if state['session_id'] != session:
        raise ValueError('lifecycle session does not match capability binding')
    return row, life.load(assignment), state


def start(session, api=service.request, acquire=None):
    """Host-only just-in-time claim. Never callable by the matching worker tool."""
    row, assignment, state = binding(session)
    from preflight import require
    require()
    folder, worker = Path(row['folder']), row['worker']
    batch.capacity(dict(checked_at=life.stamp(), status=api('status', {})), 1)
    result = service.claim(folder, worker, api=api)
    if result.get('outcome') == 'blocked':
        evidence.report(folder, worker, db_path=ledger.DEFAULT_DB)
        return result
    cid = result['claim_id']
    packet = api('packet', dict(claim_id=cid, context_mode='resume'))
    life.save(folder/worker/'packet.json', packet)
    from packs import acquire as download_pack
    pack = (acquire or download_pack)(workspace_root(), cid, packet)
    state = life.snapshot(folder, worker)
    model, _, variant = state['model'].partition('#')
    args = argparse.Namespace(claim=cid, address=state['address'], lane=assignment['target']['lane'],
                              model=model, variant=variant or 'default', harness='OpenCode', pack=str(pack),
                              deadline=state['deadline_utc'], assignment=str(folder/worker/'assignment.json'))
    with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
        ledger.register(db, args)
    return dict(ready=True, claim_id=cid, address=state['address'])


def completion(session, metadata=None):
    row, _, _ = binding(session)
    folder, worker = Path(row['folder']), row['worker']
    result = worker_call(session, 'completion', {})
    if result['cleanup_required']:
        (folder/'STOP').touch()
    if metadata is not None:
        if not isinstance(metadata, dict) or metadata.get('id') != session:
            raise ValueError('completion metadata does not identify the bound child')
        tokens = metadata.get('tokens')
        usage = dict(session_id=session, captured_utc=life.stamp(), raw_session_tokens=tokens,
                     input_semantics='unknown', service_usage=None, billed_cost=None,
                     model=metadata.get('model'), session_outcome=metadata.get('outcome'))
        life.save(folder/worker/'usage-raw.json', usage)
        result['usage'] = usage
    return result


def context(session):
    row, assignment, state = binding(session)
    folder, worker = Path(row['folder']), row['worker']
    return row, assignment, state, folder, worker, scoped(folder, worker+'/files')


def area_root(area, folder, worker, files, state):
    if area == 'files':
        return files
    if area == 'pack' and state.get('claim_id'):
        return private_root(workspace_root()/'packs'/state['claim_id'])
    raise ValueError('area must be files or the assigned pack')


def read_text(path, offset=1, limit=200):
    if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('offset/limit outside allowed range')
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('file too large; use a narrower artifact')
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    return dict(total_lines=len(lines), text='\n'.join('%d: %s' % (i+1, s)
                for i, s in enumerate(lines) if offset-1 <= i < offset-1+limit)[:MAX_TEXT])


def worker_call(session, op, data, api=service.request):
    row, assignment, state, folder, worker, files = context(session)
    if not isinstance(data, dict) or len(json.dumps(data)) > MAX_TEXT:
        raise ValueError('tool input must be a small object')
    cid = state.get('claim_id')
    if op == 'status':
        return dict(target=assignment['target'], ownership=state, phase=life.phase(state),
                    close_requested=(folder/worker/'CLOSE_REQUESTED.json').exists() or (folder/'STOP').exists())
    if op == 'completion':
        return dict(worker=worker, address=state['address'], session_id=session, claim_id=cid,
                    state=state['state'], outcome=state.get('outcome'), cleanup_required=state['state'] != 'closed',
                    report_path=str(folder/worker/'report.json'))
    if op == 'read':
        if data.get('area') == 'packet':
            path = scoped(folder, worker+'/packet.json', exists=True)
        else:
            path = scoped(area_root(data.get('area', 'files'), folder, worker, files, state), data['path'], exists=True)
        return read_text(path, data.get('offset', 1), data.get('limit', 200))
    if op in ('list', 'search'):
        root = area_root(data.get('area', 'files'), folder, worker, files, state)
        found = []
        query = data.get('text', '')
        if op == 'search' and (not isinstance(query, str) or not 1 <= len(query) <= 200):
            raise ValueError('literal search text required')
        for index, path in enumerate(root.rglob('*')):
            if index >= 10000:
                break
            path = scoped(root, path.relative_to(root).as_posix())
            if path.is_file():
                if op == 'list':
                    found.append(path.relative_to(root).as_posix())
                elif path.stat().st_size < 1024 * 1024:
                    for number, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                        if query in line:
                            found.append(dict(path=path.relative_to(root).as_posix(), line=number, text=line[:500]))
            if len(found) >= 200:
                break
        return dict(items=found, bounded=True)
    if op == 'reference':
        if data.get('name') == 'fnv':
            from twins import fnv_root
            path = scoped(fnv_root(), data['path'], exists=True)
            if path.suffix.lower() not in ('.cpp', '.h', '.hpp', '.inl', '.md', '.json') or any(
                    p.startswith('.') for p in Path(data['path']).parts):
                raise ValueError('only public FNV source/reference files allowed')
            return read_text(path, data.get('offset', 1), data.get('limit', 200))
        if data.get('name') == 'upstream_tips':
            from twins import dependency
            return read_text(scoped(dependency(), 'MATCHING_TIPS.md', exists=True), data.get('offset', 1), data.get('limit', 200))
        choices = {'policy': REPOSITORY/'docs/matching-policy.md', 'recipe': REPOSITORY/'docs/matching-recipes.md',
                   'compiler_tips': Path(runtime()['index']).parent/'compiler_tips.md',
                   'large_functions': Path(runtime()['index']).parent/'large_functions.md'}
        name = data.get('name')
        if name not in choices:
            raise ValueError('unknown approved reference')
        path = choices[name]
        # WSL-only official references are exposed by a fixed runner, not arbitrary shell.
        if name in ('compiler_tips', 'large_functions'):
            from checker import command
            import subprocess
            process = subprocess.run(command(['--reference', name]), capture_output=True, text=True, timeout=30)
            if process.returncode:
                raise ValueError('official reference unavailable')
            return dict(text=process.stdout[:MAX_TEXT])
        return read_text(path, data.get('offset', 1), data.get('limit', 200))
    if op == 'twin':
        from twins import lookup
        return lookup(state['address'])
    if op == 'target':
        from checker import command
        import subprocess
        # No address/extent supplied by the worker is ever forwarded.
        query = dict(address=int(state['address'], 16), size=assignment['target']['size'],
                     offset=data.get('offset', 0), limit=data.get('limit', 200))
        process = subprocess.run(command(['--target-json', json.dumps(query)]),
                                 capture_output=True, text=True, timeout=30)
        if process.returncode:
            raise ValueError('assigned target inspection failed; parent must inspect toolchain setup')
        return json.loads(process.stdout)
    if op == 'write':
        life.require_experiment(folder, worker)
        path = scoped(files, data['path'])
        if path.suffix.lower() not in ('.cpp', '.h', '.hpp', '.inl', '.txt', '.md', '.json'):
            raise ValueError('only candidate/source/note files allowed')
        if any(p.lower().startswith('.') or p.lower() in ('agents.md', 'opencode.json', 'opencode.jsonc')
               for p in path.relative_to(files).parts):
            raise ValueError('configuration and hidden files forbidden')
        text = data.get('text')
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_TEXT:
            raise ValueError('bounded text content required')
        path.parent.mkdir(parents=True, exist_ok=True)
        no_links(path)
        path.write_text(text, encoding='utf-8')
        return dict(path=data['path'], sha256=ledger.digest(path))
    if not cid:
        raise ValueError('host has not registered an owned claim')
    if op == 'check':
        source = scoped(files, data['source'], exists=True)
        if source.suffix.lower() != '.cpp':
            raise ValueError('C++ source required')
        stage = data.get('stage')
        if stage not in ('baseline', 'attempt', 'final'):
            raise ValueError('invalid check stage')
        alternate = data.get('flags')
        if alternate is not None and (not isinstance(alternate, str) or
                not alternate or any(f.lower() not in SAFE_FLAGS for f in alternate.split(','))):
            raise ValueError('only documented codegen flags allowed; include/output/path options forbidden')
        args = argparse.Namespace(claim=cid, source=str(source), stage=stage, flags=data.get('flags'),
                                  evidence_dir=None, import_evidence=None,
                                  **{k: data.get(k) for k in ('change', 'observed', 'why', 'expect', 'regions')})
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            result = ledger.check(db, args)
        raw = life.load(result['evidence'])
        return dict(**result, output=raw['stdout'][:MAX_TEXT], stderr=raw['stderr'][:8000])
    if op == 'gate':
        life.require_experiment(folder, worker)
        if data.get('decision') not in ('continue', 'stop') or not data.get('reason'):
            raise ValueError('gate decision/reason required')
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            return ledger.gate(db, argparse.Namespace(claim=cid, decision=data['decision'], reason=data['reason'],
                    hypothesis=data.get('hypothesis'), expected=data.get('expected'), integration_plan=data.get('integration_plan')))
    if op == 'register_wall':
        return life.progress(folder, worker, register_wall=True, evidence=data.get('evidence'))
    if op == 'promote':
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            return ledger.promote(db, argparse.Namespace(claim=cid, output=str(folder/worker/'match.cpp')))
    if op == 'checkpoint':
        phase = data.get('phase')
        if phase not in ('plan', 'draft_review', 'notes') or not isinstance(data.get('note'), str) or not data['note'].strip():
            raise ValueError('checkpoint requires plan/draft_review/notes and a substantive note')
        if phase != 'notes':
            life.require_experiment(folder, worker)
        elif state['state'] not in ('claimed', 'closed'):
            raise ValueError('resolve ambiguous claim state before closing notes')
        digest = hashlib.sha256((phase+'\0'+data['note']).encode('utf-8')).hexdigest()
        body = dict(claim_id=cid, phase=phase, note=data['note'], request_id='mf-'+digest[:40])
        path = folder/worker/(body['request_id']+'.json')
        if path.exists():
            previous = life.load(path)
            if previous.get('state') == 'acknowledged':
                return dict(checkpoint_id=previous['response'].get('checkpoint_id'), saved=True)
            raise ValueError('checkpoint outcome ambiguous; parent must reconcile, no retry')
        life.save(path, dict(request=body, state='pending'))
        response = api('checkpoint', body)
        acknowledged = isinstance(response.get('checkpoint_id'), str) and bool(response['checkpoint_id'])
        life.save(path, dict(request=body, response=response, state='acknowledged' if acknowledged else 'unresolved'))
        if not acknowledged:
            raise ValueError('checkpoint not acknowledged; preserve response and reconcile')
        return dict(checkpoint_id=response.get('checkpoint_id'), saved=True)
    if op == 'close':
        operation = data.get('operation')
        if operation not in ('submit', 'giveup', 'flag-proof'):
            raise ValueError('unsupported closure')
        source = str(scoped(files, data['source'], exists=True)) if data.get('source') else None
        result = service.close(folder, worker, operation, source=source, notes=data.get('notes'),
                               no_source_reason=data.get('no_source_reason'), db_path=ledger.DEFAULT_DB, api=api)
        return result['assessment']
    if op == 'report':
        return evidence.report(folder, worker, db_path=ledger.DEFAULT_DB)
    raise ValueError('operation not available to workers')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['task', 'bind', 'start', 'worker', 'completion'])
    args = parser.parse_args()
    value = json.loads(sys.stdin.read(MAX_TEXT + 4096))
    try:
        if args.command == 'task':
            result = task(value['batch'], value['worker'], value['parent'])
        elif args.command == 'bind':
            result = bind(value['batch'], value['worker'], value['session'], value['parent'])
        elif args.command == 'start':
            result = start(value['session'])
        elif args.command == 'completion':
            result = completion(value['session'], value.get('metadata'))
        else:
            result = worker_call(value['session'], value['op'], value.get('data', {}))
        print(json.dumps(result))
        return 0
    except ServiceError as exc:
        if exc.code in (429, 503):
            row, _, _ = binding(value['session'])
            folder = Path(row['folder'])
            (folder/'STOP').touch()
            life.save(folder/'SERVICE_COOLDOWN.json', dict(observed_utc=life.stamp(), message=str(exc)))
        print(json.dumps(dict(error=str(exc), cleanup_required=True)))
        return 2
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps(dict(error=str(exc), cleanup_required=True)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
