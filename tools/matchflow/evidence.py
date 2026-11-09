"""Generate reports from recorded evidence and audit independent closure dimensions."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

import lifecycle as life
import matchflow as ledger
from service import acknowledgement, closing_notes_record, owned

REPORT_VERSION = 3


def schema_errors(value, schema, root=None, path='$'):
    """Validate the JSON-schema subset used by our versioned report contract."""
    root = root or schema
    if '$ref' in schema:
        target = root
        for key in schema['$ref'].split('/')[1:]:
            target = target[key]
        return schema_errors(value, target, root, path)
    errors = []
    types = {'object': lambda x: isinstance(x, dict), 'array': lambda x: isinstance(x, list),
             'string': lambda x: isinstance(x, str), 'null': lambda x: x is None,
             'number': lambda x: type(x) in (int, float), 'integer': lambda x: type(x) is int,
             'boolean': lambda x: type(x) is bool}
    wanted = schema.get('type')
    if wanted and not any(types[k](value) for k in ([wanted] if isinstance(wanted, str) else wanted)):
        return [path + ': wrong type']
    if 'const' in schema and value != schema['const']:
        errors.append(path + ': wrong version/value')
    if 'enum' in schema and value not in schema['enum']:
        errors.append(path + ': unsupported value')
    if type(value) in (int, float) and 'minimum' in schema and value < schema['minimum']:
        errors.append(path + ': below minimum')
    if isinstance(value, str):
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(path + ': invalid pattern')
        if schema.get('format') == 'date-time':
            try:
                life.timestamp(value)
            except ValueError:
                errors.append(path + ': invalid timestamp')
    if isinstance(value, dict):
        errors += [path + ': missing ' + k for k in schema.get('required', []) if k not in value]
        for key, item in value.items():
            if key in schema.get('properties', {}):
                errors += schema_errors(item, schema['properties'][key], root, path + '.' + key)
    if isinstance(value, list) and 'items' in schema:
        for i, item in enumerate(value):
            errors += schema_errors(item, schema['items'], root, path + '[' + str(i) + ']')
    return errors


def resolve(path, folder):
    if not path:
        raise ValueError('missing evidence path')
    value = Path(path)
    if value.is_absolute():
        return value
    return ledger.ROOT / value if (ledger.ROOT / value).exists() else folder / value


def check_record(db, row):
    if not row:
        return None
    result = json.loads(row['result_json'])
    return dict(check_id=row['id'], status=result['status'], valid=bool(row['valid']),
                extent=result.get('extent'), diffs=result.get('diffs'),
                ndiff_insn=result.get('ndiff_insn'), checker=result.get('checker'),
                flags=json.loads(row['flags_json']), source_sha256=row['source_hash'],
                pack_sha256=row['pack_hash'], evidence_path=row['evidence_path'],
                metric_semantics='checker_extent_uninterpreted',
                diagnostic=not result.get('normal_flags', False))


def has_attempt(db_path, claim):
    path = Path(db_path)
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        return db.execute('SELECT 1 FROM attempts WHERE claim_id=?', (claim,)).fetchone() is not None


def setup_failure_record(folder, worker, state, ack, db_path):
    """A missing ledger is valid ONLY for an evidenced pre-registration failure."""
    folder = Path(folder)
    failure = state.get('setup_failure')
    if (state.get('state') != 'closed' or state.get('setup_state') != 'failed' or
            not isinstance(failure, dict) or failure.get('stage') not in ('claim', 'packet', 'pack', 'register') or
            state.get('outcome') != 'given_up' or ack.get('operation') != 'giveup' or
            ack.get('claim_id') != state.get('claim_id') or ack.get('address') != state.get('address') or
            ack.get('source_sha256') or not ack.get('no_source_reason') or
            acknowledgement(ack['response'], 'giveup')['lease_closed'] is not True):
        raise ValueError('missing ledger is not an acknowledged pre-registration setup failure')
    if life.load(folder/worker/'SETUP_FAILED.json') != failure:
        raise ValueError('setup failure marker differs from host lifecycle')
    if has_attempt(db_path, state['claim_id']):
        raise ValueError('registered attempts require ordinary ledger/check evidence')
    if not life.timestamp(state['worker_start_utc']) <= life.timestamp(failure['failed_utc']) <= life.timestamp(state['closed_utc']):
        raise ValueError('invalid setup failure timestamp')
    confirmed = False
    for name in ('claim-status.json', 'reconcile-status.json'):
        path = folder/worker/name
        if not path.is_file():
            continue
        snapshot = life.load(path)
        try:
            lease = owned(snapshot['status'], state['claim_id'], state['address'])
            when = life.timestamp(snapshot['checked_at'])
            if (life.timestamp(state['worker_start_utc']) <= when <= life.timestamp(state['closed_utc']) and
                    abs((life.timestamp(lease['expires'])-life.timestamp(state['deadline_utc'])).total_seconds()) <= 0.001):
                confirmed = True
        except (ValueError, KeyError, TypeError):
            continue
    if not confirmed:
        raise ValueError('setup failure lacks a saved official own-claim confirmation')
    return failure


def report(folder, worker, db_path=ledger.DEFAULT_DB):
    folder = Path(folder)
    state = life.snapshot(folder, worker)
    assignment = life.load(folder / worker / 'assignment.json')
    if state['state'] != 'closed':
        raise ValueError('closure acknowledgement required before generating a completed report')
    if state.get('outcome') == 'blocked' and state.get('claim_attempted') is False:
        value = dict(schema_version=REPORT_VERSION, address=state['address'], claim_id=None, session_id=state['session_id'],
                     lane=assignment.get('target', {}).get('lane', 'research'), model=assignment['claim_body']['model'],
                     variant=assignment['claim_body'].get('effort', ''), harness=assignment['harness'], outcome='blocked',
                     worker_start_utc=state['worker_start_utc'], closed_utc=state['closed_utc'],
                     elapsed_seconds=state['elapsed_seconds'],
                     elapsed_estimated=False, candidate_path=None, source_sha256=None, pack_path=None, pack_sha256=None,
                     checked_flags=[], baseline=None, final=None, latest_valid=None,
                      service=dict(submission_id=None, checkpoint_id=None, verdict=None, acknowledgement_path=None,
                                   source_retention='not_applicable'), remaining=[state['blocked_reason']],
                      setup_failure=None, closing_notes=closing_notes_record(folder, worker, state),
                     provenance='generated_from_lifecycle_and_ledger')
        life.save(folder / worker / 'report.json', value)
        return value
    notes = closing_notes_record(folder, worker, state)
    ack = life.load(state['service_ack_path'])
    with closing(ledger.connect(db_path)) as db:
        row = db.execute('SELECT * FROM attempts WHERE claim_id=?', (state['claim_id'],)).fetchone()
        if row is None:
            failure = setup_failure_record(folder, worker, state, ack, db_path)
            result = dict(schema_version=REPORT_VERSION, address=state['address'], claim_id=state['claim_id'], session_id=state['session_id'],
                          lane=assignment['target']['lane'], model=assignment['claim_body']['model'],
                          variant=assignment['claim_body'].get('effort', ''), harness=assignment['harness'], outcome='given_up',
                          worker_start_utc=state['worker_start_utc'], closed_utc=state['closed_utc'],
                          elapsed_seconds=state['elapsed_seconds'], elapsed_estimated=False,
                          candidate_path=None, source_sha256=None, pack_path=None, pack_sha256=None,
                          checked_flags=[], baseline=None, final=None, latest_valid=None,
                          service=dict(submission_id=None, checkpoint_id=None, verdict=None,
                                       acknowledgement_path=state['service_ack_path'], source_retention='not_applicable'),
                          setup_failure=failure, closing_notes=notes, remaining=[ack['no_source_reason']],
                          provenance='generated_from_lifecycle_and_service_receipts')
            life.save(folder/worker/'report.json', result)
            return result
        checks = db.execute('SELECT * FROM checks WHERE claim_id=? ORDER BY id', (state['claim_id'],)).fetchall()
        baseline = next((c for c in reversed(checks) if c['valid'] and c['stage'] == 'baseline'), None)
        final = next((c for c in reversed(checks) if c['valid'] and c['stage'] == 'final'), None)
        latest = next((c for c in reversed(checks) if c['valid']), None)
        source = folder / worker / 'closure-source.cpp'
        source_hash = ledger.digest(source) if source.exists() else None
        retained = next((c for c in reversed(checks) if c['valid'] and c['source_hash'] == source_hash), None)
        elapsed = state['elapsed_seconds']
        result = dict(schema_version=REPORT_VERSION, address=state['address'], claim_id=state['claim_id'],
                      lane=row['lane'], model=assignment['claim_body']['model'],
                      variant=assignment['claim_body'].get('effort', ''), harness=assignment['harness'],
                      outcome=state['outcome'], session_id=state['session_id'],
                      worker_start_utc=state['worker_start_utc'], closed_utc=state['closed_utc'],
                      elapsed_seconds=elapsed, elapsed_estimated=False,
                      candidate_path=str(source) if source.exists() else None,
                       source_sha256=source_hash,
                      pack_path=row['pack_path'], pack_sha256=row['pack_hash'],
                      baseline=check_record(db, baseline), final=check_record(db, final),
                       latest_valid=check_record(db, latest),
                       retained_check=check_record(db, retained),
                      checked_flags=json.loads(latest['flags_json']) if latest else [],
                      service=dict(submission_id=ack['response'].get('submission_id'), checkpoint_id=ack.get('checkpoint_id'),
                                   flag_id=ack['response'].get('flag_id'),
                                   verdict=None, acknowledgement_path=state['service_ack_path'],
                                   source_retention=state['handoff']),
                      remaining=[], provenance='generated_from_lifecycle_and_ledger')
        result.update(closing_notes=notes, setup_failure=None)
        life.save(folder / worker / 'report.json', result)
    return result


def validate_check(item, claim, pack_hash, folder, errors, db_path, stage):
    if item is None:
        return
    if not isinstance(item, dict) or not isinstance(item.get('check_id'), int):
        errors.append('check record lacks a ledger ID')
        return
    try:
        raw = life.load(resolve(item.get('evidence_path'), folder))
        cmd = raw['command']
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            recorded = db.execute('SELECT * FROM checks WHERE id=? AND claim_id=?', (item['check_id'], claim)).fetchone()
        if (not recorded or not recorded['valid'] or recorded['pack_hash'] != pack_hash or
                recorded['source_hash'] != item.get('source_sha256') or
                json.loads(recorded['command_json']) != cmd or
                json.loads(recorded['flags_json']) != item.get('flags') or
                (stage not in ('latest_valid', 'retained_check') and recorded['stage'] != stage)):
            raise ValueError('ledger check ID/provenance/flags/stage mismatch')
        if cmd.count('--claim') != 1 or cmd[cmd.index('--claim')+1] != claim or '--json' not in cmd:
            raise ValueError('checker invocation does not name this claim and JSON mode')
        if raw.get('pack_hash') != pack_hash or raw.get('source_hash') != item.get('source_sha256'):
            raise ValueError('checker hashes differ from report')
        snapshot = raw.get('source_snapshot_path')
        if snapshot or ('source_snapshot_path' in recorded.keys() and recorded['source_snapshot_path']):
            if ('source_snapshot_path' not in recorded.keys() or recorded['source_snapshot_path'] != snapshot or
                    ledger.digest(snapshot) != recorded['source_hash']):
                raise ValueError('source snapshot hash/path differs from check evidence')
        result = ledger.diagnose_result(raw['stdout'], raw['stderr'], raw['exit_code'])
        if raw.get('unchanged') is not True or result.get('status') != item.get('status'):
            raise ValueError('checker result is invalid or inconsistent')
        if item.get('valid') is not True or result['status'] in ('tool_error', 'rejected'):
            raise ValueError('check is not valid evidence')
        for key in ('extent', 'diffs', 'ndiff_insn', 'checker'):
            if item.get(key) != result.get(key):
                raise ValueError('checker metric differs: ' + key)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        errors.append('check evidence: ' + str(exc))


def audit_worker(folder, task, state, status, db_path=ledger.DEFAULT_DB):
    folder = Path(folder)
    worker = folder / task['id']
    errors = []
    ack = {}
    claim = state.get('claim_id')
    if state.get('outcome') == 'blocked' and state.get('claim_attempted') is False and not claim:
        try:
            value = life.load(worker / 'report.json')
            errors += schema_errors(value, life.load(Path(__file__).with_name('report.schema.json')))
            if (value.get('outcome') != 'blocked' or value.get('address') != task['address'] or
                    value.get('claim_id') is not None or state['address'] != task['address']):
                errors.append('blocked report differs from unclaimed assignment')
            if value.get('closing_notes') != closing_notes_record(folder, task['id'], state):
                errors.append('blocked closing notes state differs from unclaimed assignment')
            if not life.timestamp(state['worker_start_utc']) <= life.timestamp(state['closed_utc']) <= life.utc():
                errors.append('blocked timestamps inconsistent')
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append('blocked evidence: ' + str(exc))
        return dict(lease_closure='not_claimed', evidence_integrity='invalid' if errors else 'valid',
                    source_handoff='not_applicable', verification='not_submitted', evidence_errors=errors)
    opened = {c['claim_id'] for c in status['open_claims']}
    lease = 'open' if claim in opened else 'absent' if claim else 'unknown'
    handoff, verification = 'unknown', 'unknown'
    if state.get('address') != task['address']:
        errors.append('ownership address differs from assignment')
    session = state.get('session_id')
    if not isinstance(session, str) or not re.fullmatch(r'ses_[A-Za-z0-9]+', session):
        errors.append('missing or placeholder session ID')
    for key in ('worker_start_utc', 'deadline_utc', 'closed_utc'):
        if key not in state:
            errors.append('missing timestamp: ' + key)
    try:
        start = life.timestamp(state['worker_start_utc'])
        end = life.timestamp(state['closed_utc'])
        deadline = life.timestamp(state['deadline_utc'])
        if not start <= end <= life.utc() or end > deadline:
            errors.append('inconsistent ownership timestamp order')
    except (ValueError, KeyError, TypeError):
        errors.append('invalid ownership timestamps')
    try:
        ack = life.load(resolve(state.get('service_ack_path'), worker))
        if ack.get('claim_id') != claim or ack.get('address') != task['address']:
            raise ValueError('acknowledgement claim/address mismatch or missing')
        response = ack['response']
        assessment = acknowledgement(response, ack['operation'], bool(ack.get('source_sha256')))
        if not assessment['lease_closed']:
            raise ValueError('response did not acknowledge closure')
        handoff = assessment['source_retention']
        if handoff == 'failed':
            errors.append('service did not retain the source')
        if handoff == 'not_applicable' and not ack.get('no_source_reason'):
            errors.append('no source and no explicit reason')
        if lease == 'absent':
            lease = 'closed_acknowledged'
        observed = life.timestamp(ack['observed_utc'])
        if observed > life.utc():
            errors.append('acknowledgement observed in the future')
        verification = assessment['verification']
        if assessment.get('submission_id'):
            matches = [r for r in status.get('recent', []) if r['id'] == assessment['submission_id']]
            if matches:
                if matches[0]['addr'] != task['address']:
                    errors.append('submission status address mismatch')
                else:
                    verification = matches[0].get('verdict') or 'pending'
    except (ValueError, KeyError, TypeError, OSError) as exc:
        errors.append('acknowledgement: ' + str(exc))
    notes = None
    try:
        notes = closing_notes_record(folder, task['id'], state)
    except ValueError as exc:
        errors.append(str(exc))
    try:
        value = life.load(worker / 'report.json')
        schema = life.load(Path(__file__).with_name('report.schema.json'))
        violations = schema_errors(value, schema)
        if violations:
            raise ValueError('; '.join(violations))
        if value['claim_id'] != claim or value['address'] != task['address']:
            raise ValueError('report assignment mismatch')
        if value['session_id'] != session:
            errors.append('report session differs from lifecycle')
        expected_model = task['model'].split('#')[0]
        if value['model'] != expected_model:
            errors.append('report model differs from assignment')
        start = life.timestamp(value['worker_start_utc'])
        end = life.timestamp(value['closed_utc'])
        if (value['worker_start_utc'] != state['worker_start_utc'] or
                value['closed_utc'] != state['closed_utc'] or
                abs(state['elapsed_seconds'] - value['elapsed_seconds']) > 0.01 or
                abs((end-start).total_seconds() - value['elapsed_seconds']) > 5):
            errors.append('report timestamps/duration differ from lifecycle')
        if value['outcome'] != state.get('outcome'):
            errors.append('report outcome differs from lifecycle')
        if value.get('closing_notes') != notes:
            errors.append('report closing notes differ from acknowledged receipt')
        if value.get('setup_failure') is not None:
            expected = setup_failure_record(folder, task['id'], state, ack, db_path)
            if value['setup_failure'] != expected or value['provenance'] != 'generated_from_lifecycle_and_service_receipts':
                errors.append('report setup failure differs from host evidence')
            if (any(value.get(k) is not None for k in ('candidate_path', 'source_sha256', 'pack_path', 'pack_sha256',
                                                       'baseline', 'final', 'latest_valid')) or value['checked_flags'] or
                    value['service']['submission_id'] is not None or value['service']['checkpoint_id'] is not None or
                    value['service']['source_retention'] != 'not_applicable' or
                    value['service']['acknowledgement_path'] != state['service_ack_path']):
                errors.append('setup failure invents source/pack/check/submission evidence')
            return dict(lease_closure=lease, evidence_integrity='invalid' if errors else 'valid',
                        source_handoff=handoff, verification=verification, evidence_errors=errors)
        if value['provenance'] != 'generated_from_lifecycle_and_ledger':
            errors.append('ordinary attempt requires ledger provenance')
        pack = resolve(value['pack_path'], worker)
        assignment = life.load(worker / 'assignment.json')
        if pack.resolve() != (Path(assignment['workspace']) / 'packs' / claim).resolve():
            errors.append('pack path does not belong to this exact claim')
        if ledger.tree_digest(pack) != value['pack_sha256']:
            errors.append('pack hash changed')
        metadata = life.load(pack / 'pack.json')
        if ledger.address(metadata['address']) != task['address']:
            errors.append('pack belongs to a different address')
        if value['candidate_path']:
            source = resolve(value['candidate_path'], worker)
            if ledger.digest(source) != value['source_sha256']:
                errors.append('candidate hash changed')
            if value['source_sha256'] != ack.get('source_sha256'):
                errors.append('handoff source hash differs from candidate')
        for key in ('baseline', 'final', 'latest_valid'):
            validate_check(value.get(key), claim, value['pack_sha256'], worker, errors, db_path, key)
        if 'retained_check' in value:
            retained = value['retained_check']
            validate_check(retained, claim, value['pack_sha256'], worker, errors, db_path, 'retained_check')
            if retained and retained['source_sha256'] != value['source_sha256']:
                errors.append('retained check does not describe the handed-off source')
            with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
                found = db.execute('SELECT id FROM checks WHERE claim_id=? AND source_hash=? AND valid=1',
                                   (claim, value['source_sha256'])).fetchone()
            if found and not retained:
                errors.append('report omits the retained source check linkage')
        else:
            with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(checks)')}
                found = ('source_snapshot_path' in columns and db.execute(
                    'SELECT id FROM checks WHERE claim_id=? AND source_hash=? AND valid=1 AND source_snapshot_path IS NOT NULL',
                    (claim, value['source_sha256'])).fetchone())
            if found:
                errors.append('new checked-source handoff requires retained_check linkage')
        if value['outcome'] == 'submitted':
            final = value.get('final') or {}
            if (final.get('status') != 'match' or final.get('source_sha256') != value['source_sha256'] or
                    not any(ledger.same_flags(final.get('flags', []), f) for f in ledger.accepted_sets(metadata))):
                errors.append('submission lacks an unchanged accepted-flags final MATCH')
            if value['service']['submission_id'] != ack['response'].get('submission_id'):
                errors.append('report submission ID differs from acknowledgement')
        if value['outcome'] == 'flag_review':
            final = value.get('final') or {}
            if (final.get('status') != 'match' or final.get('source_sha256') != value['source_sha256'] or
                    any(ledger.same_flags(final.get('flags', []), f) for f in ledger.accepted_sets(metadata)) or
                    not value['service'].get('checkpoint_id') or
                    value['service'].get('checkpoint_id') != ack.get('checkpoint_id') or
                    value['service'].get('flag_id') != ack['response'].get('flag_id')):
                errors.append('flag proof lacks exact diagnostic MATCH/checkpoint/receipt linkage')
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        errors.append('report: ' + str(exc))
    return dict(lease_closure=lease, evidence_integrity='invalid' if errors else 'valid',
                source_handoff=handoff, verification=verification, evidence_errors=errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ledger.DEFAULT_DB))
    parser.add_argument('command', choices=['report'])
    parser.add_argument('folder'); parser.add_argument('worker')
    args = parser.parse_args()
    try:
        value = report(args.folder, args.worker, args.db)
        print(json.dumps(dict(report=str(Path(args.folder)/args.worker/'report.json'), outcome=value['outcome'])))
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
