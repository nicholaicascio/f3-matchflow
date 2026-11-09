import argparse
from contextlib import ExitStack, closing
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import batch
import broker
import lifecycle as life
import matchflow as ledger
import packs
import preflight
import security
import transport
import twins


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_rejects_traversal_absolute_unc_drive_ads_devices(self):
        for value in ('../tool.py', 'a/../../x', '/etc/passwd', 'C:\\x', 'C:x', '\\\\host\\share',
                      'x:secret', 'a//b', 'x.', 'NUL.txt', 'COM1.cpp', 'a\x00.cpp'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                security.scoped(self.root, value)

    def test_rejects_symlink(self):
        target = self.root/'real'; target.mkdir()
        try:
            (self.root/'link').symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege not available')
        with self.assertRaises(ValueError):
            security.scoped(self.root, 'link/a.cpp')

    def test_rejects_windows_junction(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows-only reparse check')
        target = self.root/'real'; target.mkdir()
        link = self.root/'link'
        process = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
        if process.returncode:
            self.skipTest('junction creation unavailable')
        with self.assertRaises(ValueError):
            security.scoped(self.root, 'link/a.cpp')
        link.rmdir()

    def test_safe_archive_and_escapes(self):
        def archive(name, link=False):
            value = io.BytesIO()
            with tarfile.open(fileobj=value, mode='w:gz') as tar:
                entry = tarfile.TarInfo(name)
                data = b'{"address":"0x00123456","flags":[]}'
                if link:
                    entry.type = tarfile.SYMTYPE; entry.linkname = '/outside'
                    tar.addfile(entry)
                else:
                    entry.size = len(data); tar.addfile(entry, io.BytesIO(data))
            return value.getvalue()
        for i, name in enumerate(('../outside', '/outside', 'C:/outside', 'pack.json')):
            with self.subTest(name=name):
                if name == 'pack.json':
                    packs.unpack(archive(name), self.root/str(i))
                else:
                    with self.assertRaises(ValueError):
                        packs.unpack(archive(name), self.root/str(i))
        with self.assertRaises(ValueError):
            packs.unpack(archive('pack.json', True), self.root/'linkpack')


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for module, name, value in ((batch, 'ROOT', self.root), (batch, 'BATCHES', self.root/'work/batches'),
                                    (broker, 'workspace_root', lambda: self.root),
                                    (ledger, 'DEFAULT_DB', self.root/'work/matchflow/ledger.sqlite3')):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch('preflight.require'))
        item = dict(address='0x00123456', size=100, state='open', co_workers=0,
                    give_ups=0, open_problem_reports=0, file_problem_reports=0,
                    reason='Reviewed current metadata',
                    review=dict(history_reviewed=True, history_sources=['fixture'],
                                prior_verdicts=[], verdict_history='reviewed', report_scope='none',
                                report_evidence=None, prior_unfit=False,
                                next_experiment=None, integration_plan=None))
        selection = self.root/'selection.json'
        life.save(selection, dict(checked_at=life.stamp(), targets=[item, dict(item, address='0x00123457')]))
        self.folder = Path(batch.prepare('test', selection)['batch'])
        life.dispatch(self.folder, 'subagents'); life.tick(self.folder, 'fixture')
        broker.bind('test', 'worker01', 'ses_child1', 'ses_parent')
        broker.bind('test', 'worker02', 'ses_child2', 'ses_parent')

    def call(self, op, **data):
        return broker.worker_call('ses_child1', op, data)

    def test_only_own_files_can_be_edited(self):
        result = self.call('write', path='candidate.cpp', text='int f() { return 0; }')
        self.assertIn('sha256', result)
        self.assertIn('return 0', self.call('read', path='candidate.cpp')['text'])
        for path in ('../assignment.json', '../../worker02/files/x.cpp',
                     str(batch.CODE/'service.py'), 'opencode.json', '.opencode/a.json', 'AGENTS.md', 'x.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.call('write', path=path, text='untrusted')
        with self.assertRaises(ValueError):
            broker.worker_call('ses_child2', 'read', dict(path='candidate.cpp'))

    def test_schema_and_stage_enum_discovery(self):
        schema = self.call('schema')
        self.assertEqual(schema['input']['properties']['data']['properties']['stage']['enum'],
                         ['baseline', 'attempt', 'final'])
        self.assertIn('check_id', schema['operations']['diff']['required'])
        with self.assertRaisesRegex(ValueError, 'baseline, attempt or final'):
            self.call('check', source='x.cpp', stage='experiment', change='x', why='x', expect='x')

    def test_help_delivered_on_tools_acknowledged_and_blocks_voluntary_close(self):
        self.owned_fixture()
        self.call('escalate', reason='Measured issue', need='A concrete answer')
        response = batch.answer('test', 'worker01', 'Use the named member.')
        value = self.call('list')
        self.assertEqual(value['unread_parent_help'][0]['request_id'], response['request_id'])
        with self.assertRaisesRegex(ValueError, 'unread parent help'):
            life.begin_close(self.folder, 'worker01', require_help_read=True)
        self.call('help_ack', request_ids=[response['request_id']])
        self.assertEqual(life.unread_help(self.folder, 'worker01'), [])
        life.begin_close(self.folder, 'worker01', require_help_read=True)

    def test_forced_close_ignores_unread_help(self):
        self.owned_fixture()
        self.call('escalate', reason='Measured issue', need='Answer')
        batch.answer('test', 'worker01', 'Read this.')
        (self.folder/'STOP').touch()
        life.begin_close(self.folder, 'worker01', require_help_read=True)

    def test_answer_during_close_does_not_freeze_source_or_review(self):
        self.owned_fixture()
        self.call('escalate', reason='Measured issue', need='Answer')
        self.call('write', path='a.cpp', text='void f() { return; }')
        notes = dict(reason='No concrete next lead', category='other', tried='baseline',
                     still_differs='register selection', suspected_cause='allocation')
        answered = []
        def api(tool, body):
            if tool == 'status':
                if not answered:
                    answered.append(batch.answer('test', 'worker01', 'Try the named local.'))
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                return dict(released=True, best_code='best_code kept: fixture')
            raise AssertionError(tool)
        with self.assertRaisesRegex(ValueError, 'unread parent help'):
            broker.worker_call('ses_child1', 'close', dict(operation='giveup', source='a.cpp', notes=notes), api=api)
        self.assertFalse((self.folder/'worker01/closure-source.cpp').exists())
        self.assertFalse((self.folder/'worker01/near-miss-review.json').exists())
        self.assertEqual(life.snapshot(self.folder, 'worker01')['state'], 'claimed')
        self.call('help_ack', request_ids=[answered[0]['request_id']])
        self.call('write', path='b.cpp', text='void f() {}')
        closed = broker.worker_call('ses_child1', 'close', dict(operation='giveup', source='b.cpp', notes=notes), api=api)
        self.assertTrue(closed['lease_closed'])
        self.assertEqual((self.folder/'worker01/closure-source.cpp').read_text(), 'void f() {}')

    def test_invalid_reports_never_complete_or_invent_submissions(self):
        for worker, session in (('worker01', 'ses_child1'), ('worker02', 'ses_child2')):
            state = life.blocked(self.folder, worker, 'fixture setup blocked')
            life.save(self.folder/worker/'report.json', dict(address=state['address'], session_id=session,
                       claim_id=None, outcome='submitted', retained_check=dict(ndiff_insn=0)))
        result = batch.monitor('test')
        self.assertNotIn('report_complete', [e['event'] for e in result['events']])
        with self.assertRaisesRegex(ValueError, 'evidence invalid'):
            batch.debrief('test')
        # The experiment watchdog legitimately stops at claim closure; the parent
        # monitor must keep waiting for notes/reports without a stale-health error.
        with life.transaction(self.folder) as db:
            control = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()['payload'])
            control['heartbeat'] = -1000
            db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(control),))
        with patch.object(batch.time, 'sleep', side_effect=RuntimeError('still monitoring reports')):
            with self.assertRaisesRegex(RuntimeError, 'still monitoring reports'):
                batch.monitor('test')

    def test_full_diff_captured_paginated_and_hash_checked(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() {}')
        result = dict(status='wip', diffs=1, ndiff_insn=1, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        def runner(cmd, **kwargs):
            output = cmd[cmd.index('--full-diff')+1]
            # The production runner maps to WSL; this fixture uses native paths.
            Path(output).write_text('\n'.join('line %s' % n for n in range(400)), encoding='utf-8')
            return subprocess.CompletedProcess(cmd, 1, json.dumps(result), '')
        with patch('matchflow.wsl_path', side_effect=str), patch.object(ledger.subprocess, 'run', side_effect=runner):
            checked = self.call('check', source='candidate.cpp', stage='baseline', change='start', why='inspect', expect='diff')
        page = self.call('diff', check_id=checked['check_id'], offset=301, limit=10)
        self.assertTrue(page['available'])
        self.assertIn('line 300', page['text'])
        self.assertEqual(self.call('diff', check_id=checked['check_id'], limit=1500)['total_lines'], 400)
        with self.assertRaises(ValueError):
            broker.worker_call('ses_child2', 'diff', dict(check_id=checked['check_id']))
        raw = life.load(checked['evidence'])
        Path(raw['full_diff']['path']).write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.call('diff', check_id=checked['check_id'])

    def test_audit_assertions_reject_wrong_batch_claim_or_session(self):
        self.owned_fixture()
        status = self.root/'status.json'
        life.save(status, dict(checked_at=life.stamp(), status=dict(open_claims=[])))
        expected = dict(batch='other', workers=[dict(worker='worker01', address='0x00123456',
                                                   claim_id='clm_fixture', session_id='ses_child1')])
        with self.assertRaisesRegex(ValueError, 'expected batch'):
            batch.audit('test', status, expected)
        expected['batch'] = 'test'
        expected['workers'][0]['session_id'] = 'ses_wrong'
        with self.assertRaisesRegex(ValueError, 'exact assignment'):
            batch.audit('test', status, expected)

    def test_monitor_closed_is_not_report_complete(self):
        self.owned_fixture()
        life.begin_close(self.folder, 'worker01')
        life.closed(self.folder, 'worker01', 'given_up', self.folder/'ack.json', 'retained')
        result = batch.monitor('test')
        self.assertEqual(result['state'], 'worker_milestones')
        self.assertEqual([e['event'] for e in result['events']], ['claim_closed'])

    def test_unbound_session_and_changed_assignment_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'unbound'):
            broker.worker_call('ses_forged', 'status', {})
        path = self.folder/'worker01/assignment.json'
        value = life.load(path); value['target']['address'] = '0x00999999'; life.save(path, value)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.call('status')

    def test_binding_uniqueness_and_workers_cannot_spawn(self):
        with self.assertRaises((ValueError, broker.sqlite3.IntegrityError)):
            broker.bind('test', 'worker01', 'ses_other', 'ses_parent')
        with self.assertRaisesRegex(ValueError, 'workers cannot'):
            broker.task('test', 'worker02', 'ses_child1')

    def test_no_claim_or_shell_or_reclassification_worker_operation(self):
        for op in ('claim', 'shell', 'reclassify', 'launch', 'bind', 'start'):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.call(op, address='0x00999999', session='ses_child2')

    def test_host_claim_and_pack_registration_use_exact_assignment(self):
        self.calls = []
        def api(tool, body):
            self.calls.append((tool, body))
            if tool == 'status':
                return dict(limits=dict(claims=20), open_claims=[] if not any(t == 'claim' for t, _ in self.calls)
                    else [dict(claim_id='clm_fixture', address='0x00123456', this_key=True,
                               expires=life.stamp(life.utc()+life.timedelta(hours=1)))])
            if tool == 'function_info':
                return dict(state='open', regions=dict(co_workers=0))
            if tool == 'claim':
                self.assertEqual(body['address'], '0x00123456')
                self.assertNotIn('band', body)
                return dict(claim_id='clm_fixture')
            if tool == 'packet':
                return dict(pack_url='https://f3decomp.com/private-pack')
            raise AssertionError(tool)
        def acquire(workspace, cid, packet):
            root = workspace/'packs'/cid; root.mkdir(parents=True)
            life.save(root/'pack.json', dict(address='0x00123456', flags=['/O2']))
            return root
        result = broker.start('ses_child1', api=api, acquire=acquire)
        self.assertTrue(result['ready'])
        with self.assertRaises(ValueError):
            broker.start('ses_child1', api=api, acquire=acquire)
        self.assertEqual(sum(t == 'claim' for t, _ in self.calls), 1)

    def test_stop_blocks_writes_and_claims(self):
        (self.folder/'STOP').touch()
        with self.assertRaisesRegex(ValueError, 'STOP'):
            self.call('write', path='x.cpp', text='x')
        with self.assertRaisesRegex(ValueError, 'STOP'):
            broker.task('test', 'worker01', 'ses_parent')

    def owned_fixture(self):
        life.reserve_claim(self.folder, 'worker01')
        life.claimed(self.folder, 'worker01', 'clm_fixture', '0x00123456',
                     life.stamp(life.utc()+life.timedelta(hours=1)))
        state = life.snapshot(self.folder, 'worker01')
        root = self.root/'packs/clm_fixture'; root.mkdir(parents=True)
        life.save(root/'pack.json', dict(address='0x00123456', flags=['/MT', '/O2'], size=10))
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            ledger.register(db, argparse.Namespace(claim='clm_fixture', address='0x00123456',
                review=dict(history_reviewed=True, history_sources=['fixture'], prior_verdicts=[],
                            verdict_history='reviewed', report_scope='none', report_evidence=None,
                            prior_unfit=False, next_experiment=None, integration_plan=None),
                model='test/model', variant='max', harness='Offline Test', pack=str(root), deadline=state['deadline_utc'],
                assignment=str(self.folder/'worker01/assignment.json')))

    def test_mocked_scoped_check_promote_submit_notes_report_and_audit(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() {}')
        result = dict(status='match', diffs=0, ndiff_insn=0, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='recheck baseline')
            self.call('gate', decision='continue', reason='baseline exact', hypothesis='retain exact source', expected='MATCH')
            self.call('check', source='candidate.cpp', stage='final', why='freeze exact source', observed='baseline exact')
        self.assertEqual(self.call('promote')['state'], 'submit_ready')
        def api(tool, body):
            self.assertEqual(body.get('claim_id', 'clm_fixture'), 'clm_fixture')
            if tool == 'status':
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'submit':
                self.assertEqual(body['source'], 'void f() {}')
                return dict(submission_id='sub_fixture')
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_fixture')
            raise AssertionError(tool)
        closed = broker.worker_call('ses_child1', 'close', dict(operation='submit'), api=api)
        self.assertTrue(closed['lease_closed'])
        broker.worker_call('ses_child1', 'checkpoint', dict(phase='notes', note='Literal wrapper matched.'), api=api)
        report = self.call('report')
        self.assertEqual(report['final']['status'], 'match')
        task = life.load(self.folder/'batch.json')['tasks'][0]
        audit = broker.evidence.audit_worker(self.folder, task, life.snapshot(self.folder, 'worker01'),
                    dict(open_claims=[], recent=[dict(id='sub_fixture', addr='0x00123456', verdict='verified')]), ledger.DEFAULT_DB)
        self.assertEqual(audit['evidence_errors'], [])
        self.assertEqual(audit['verification'], 'verified')
        summary = batch.debrief('test')
        self.assertEqual(summary['counts']['submitted'], 1)
        self.assertEqual(summary['workers'][0]['verification'], 'unknown_without_fresh_status')
        self.assertEqual(summary['workers'][0]['retained_check_id'], report['retained_check']['check_id'])
        self.assertEqual(summary['workers'][0]['handoff_note'], 'Literal wrapper matched.')
        self.assertEqual(summary['workers'][0]['remaining'], report['remaining'])
        milestones = batch.monitor('test')['events']
        self.assertEqual([m['event'] for m in milestones], ['claim_closed', 'notes_complete', 'report_complete'])
        saved = life.load(self.folder/'worker01/report.json')
        saved['claim_id'] = 'clm_wrong'
        life.save(self.folder/'worker01/report.json', saved)
        with self.assertRaisesRegex(ValueError, 'report evidence invalid'):
            batch.debrief('test')
        with self.assertRaises(ValueError):
            self.call('write', path='candidate.cpp', text='changed')

    def test_permute_is_bounded_verifies_best_and_keeps_evidence(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() { return 0; }')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        self.call('gate', decision='continue', reason='Near miss', hypothesis='Permuter can finish it', expected='MATCH')
        calls = []
        outer = self
        class Search:
            returncode = 0
            stdout = 'MATCH: written to /mnt/f/private/candidate.permuted.cpp (check it once more with plain f3check, then submit that file)'
            stderr = ''
            pid = 4242
            def __init__(self, cmd, **kwargs):
                calls.append(cmd)
                candidate = next((outer.folder/'worker01/permute').rglob('candidate.cpp'))
                candidate.with_name('candidate.permuted.cpp').write_text('void f() { return 1; }')
                handle = kwargs.get('stdout')
                if hasattr(handle, 'write'):
                    handle.write(self.stdout.encode())
                    handle.flush()
            def poll(self): return self.returncode
            def communicate(self): return self.stdout, self.stderr
            def kill(self): self.returncode = -1
            def wait(self, timeout=None): return self.returncode
        def runner(cmd, **kwargs):
            matched = dict(status='match', diffs=0, ndiff_insn=0, extent=10,
                           checker='checker: f3check test, compiler VS2005 SP1')
            return subprocess.CompletedProcess(cmd, 0, json.dumps(matched), '')
        with patch.object(ledger.subprocess, 'Popen', side_effect=Search), \
                patch.object(ledger.subprocess, 'run', side_effect=runner):
            result = self.call('permute', source='candidate.cpp', stage='attempt', trials=20, jobs=1,
                               timeout_seconds=60, change='official search', why='Near miss',
                               expect='MATCH', observed='two instructions differ')
        permute_call = next(cmd for cmd in calls if '--permute' in cmd)
        self.assertIn('--trials', permute_call)
        self.assertIn('20', permute_call)
        self.assertNotIn('--claim', permute_call)
        self.assertNotIn('--json', permute_call)
        self.assertEqual(result['score'], 0)
        self.assertTrue(result['matched'])
        self.assertEqual(result['verification']['result']['status'], 'match')
        self.assertTrue(Path(result['output']).is_file())
        self.assertEqual(Path(result['output']).read_text(), 'void f() { return 1; }')
        record = life.load(Path(result['record_path']))
        self.assertEqual(record['claim_id'], 'clm_fixture')
        self.assertEqual(record['output_sha256'], ledger.digest(result['output']))
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            self.assertEqual(len(ledger.permute_runs(db, 'clm_fixture')), 1)
        # Forced closure keeps the permute evidence without demanding a fresh review.
        (self.folder/'STOP').touch()
        notes = dict(reason='Forced stop after bounded search', category='out_of_time',
                     tried='Official permuter best source', still_differs='None; verification matched',
                     suspected_cause='Not applicable')
        def api(tool, body):
            if tool == 'status':
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                return dict(released=True, best_code='best_code kept: fixture')
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_notes')
            raise AssertionError(tool)
        broker.worker_call('ses_child1', 'close', dict(operation='giveup', source=Path(result['output']).name,
                           notes=notes), api=api)
        broker.worker_call('ses_child1', 'checkpoint', dict(phase='notes', note='Permute evidence retained.'), api=api)
        report = self.call('report')
        self.assertEqual(report['permute_runs'][0]['run_id'], result['run_id'])
        audited = broker.evidence.audit_worker(self.folder, life.load(self.folder/'batch.json')['tasks'][0],
                                               life.snapshot(self.folder, 'worker01'),
                                               dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertEqual(audited['evidence_errors'], [])
        raw_file = Path(result['record_path']).with_name('raw.json')
        raw_original = raw_file.read_bytes()
        raw_file.write_text('{}', encoding='utf-8')
        tampered_raw = broker.evidence.audit_worker(self.folder, life.load(self.folder/'batch.json')['tasks'][0],
                                                    life.snapshot(self.folder, 'worker01'),
                                                    dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertTrue(any('permutation' in e for e in tampered_raw['evidence_errors']))
        raw_file.write_bytes(raw_original)
        record_file = Path(result['record_path'])
        damaged = life.load(record_file)
        damaged.pop('source_path')
        life.save(record_file, damaged)
        tampered = broker.evidence.audit_worker(self.folder, life.load(self.folder/'batch.json')['tasks'][0],
                                                life.snapshot(self.folder, 'worker01'),
                                                dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertTrue(any('permutation' in e for e in tampered['evidence_errors']))

    def test_permute_stops_at_stop_and_does_not_publish_candidate(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() { return 0; }')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        self.call('gate', decision='continue', reason='Near miss', hypothesis='Search', expected='MATCH')
        outer = self
        class Search:
            returncode = 0
            stdout = 'MATCH: written to /mnt/f/private/candidate.permuted.cpp'
            stderr = ''
            pid = 4243
            stdout = 'SYNTHETIC_DISASSEMBLY_SECRET_SENTINEL\n' + 'Z' * 200000
            stderr = 'SYNTHETIC_SECRET_SENTINEL'
            def __init__(self, cmd, **kwargs):
                self.polls = 0
                kwargs['stdout'].write(self.stdout.encode('utf-8'))
                kwargs['stderr'].write(self.stderr.encode('utf-8'))
                (outer.folder/'STOP').touch()
            def poll(self):
                self.polls += 1
                return 0 if self.polls > 1 else None
            def communicate(self): return self.stdout, self.stderr
            def kill(self): self.returncode = -1
            def wait(self, timeout=None): return self.returncode
        with patch.object(ledger.subprocess, 'Popen', side_effect=Search), \
                patch.object(ledger.subprocess, 'run') as verification:
            failed = self.call('permute', source='candidate.cpp', stage='attempt', trials=20, jobs=1,
                          timeout_seconds=60, change='search', why='Near miss', expect='MATCH',
                          observed='two instructions differ')
            self.assertFalse(failed['succeeded'])
            self.assertEqual(failed['diagnostics']['disposition'], 'stopped')
            verification.assert_not_called()
        self.assertFalse(list((self.folder/'worker01/files').glob('permuted-*.cpp')))
        record = life.load(next((self.folder/'worker01/permute').rglob('run.json')))
        self.assertTrue(record['stopped_mid_run'])
        self.assertEqual(record['verification_disposition'], 'stopped')
        self.assertIsNone(record['usable_path'])
        notes = dict(reason='STOP during bounded search', category='out_of_time',
                     tried='Official permuter', still_differs='Two instructions',
                     suspected_cause='Compiler register choice')
        def api(tool, body):
            if tool == 'status':
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                return dict(released=True, best_code='best_code kept: fixture')
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_notes')
            raise AssertionError(tool)
        broker.worker_call('ses_child1', 'close', dict(operation='giveup', source='candidate.cpp', notes=notes), api=api)
        broker.worker_call('ses_child1', 'checkpoint', dict(phase='notes', note='Stopped search evidence retained.'), api=api)
        report = self.call('report')
        self.assertEqual(report['permute_runs'][0]['verification_disposition'], 'stopped')
        self.assertNotIn('SYNTHETIC_', json.dumps(report))
        self.assertNotIn('ZZZZZZ', json.dumps(report))
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            payloads = ''.join(r[0] for r in db.execute("SELECT payload FROM events WHERE kind='permute'"))
            self.assertNotIn('SYNTHETIC_', payloads)
            self.assertNotIn('ZZZZZZ', payloads)
        raw = life.load(record['raw_path'])
        self.assertIn('SYNTHETIC_', raw['stdout'])
        audited = broker.evidence.audit_worker(self.folder, life.load(self.folder/'batch.json')['tasks'][0],
                                               life.snapshot(self.folder, 'worker01'),
                                               dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertEqual(audited['evidence_errors'], [])

    def test_permute_rejects_before_gate_and_out_of_bounds(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() { return 0; }')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        with patch.object(ledger.subprocess, 'run') as runner:
            with self.assertRaisesRegex(ValueError, 'continue gate'):
                self.call('permute', source='candidate.cpp', trials=20, jobs=1, timeout_seconds=60)
            runner.assert_not_called()
        self.call('gate', decision='continue', reason='Near miss', hypothesis='Search', expected='MATCH')
        for data in (dict(trials=5), dict(trials=401), dict(jobs=0), dict(jobs=9),
                     dict(timeout_seconds=10), dict(timeout_seconds=601), dict(flags='/FIsecret')):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.call('permute', source='candidate.cpp', **data)
        with self.assertRaises(ValueError):
            self.call('permute', source='../assignment.json', trials=20, jobs=1, timeout_seconds=60)

    def test_timeout_diagnostics_reach_worker_without_raw_output(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() {}')
        result = dict(status='wip', diffs=2, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        self.call('gate', decision='continue', reason='near miss', hypothesis='search', expected='MATCH')
        started = [False]
        clock = ledger.time.monotonic
        class Search:
            returncode = 124
            def __init__(self, cmd, **kwargs):
                self.polls = 0
                kwargs['stdout'].write(b'SECRET_SENTINEL\n')
                started[0] = True
            def poll(self):
                self.polls += 1
                return None if self.polls == 1 else 124
            def wait(self, timeout=None):
                return 124
        with patch.object(ledger.time, 'monotonic', side_effect=lambda: clock() + (100 if started[0] else 0)), \
                patch.object(ledger.subprocess, 'Popen', side_effect=Search):
            failed = self.call('permute', source='candidate.cpp', stage='attempt', trials=20,
                               jobs=1, timeout_seconds=60, why='search', expect='MATCH', observed='2 differ')
        self.assertEqual(failed['verification_disposition'], 'timeout')
        self.assertFalse(failed['succeeded'])
        self.assertEqual(failed['diagnostics']['effective_timeout_seconds'], 60)
        self.assertIn('run_id', failed)
        self.assertIsNone(failed['output'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(failed))

    def test_escalate_wakes_monitor_and_parent_answer_is_read(self):
        self.owned_fixture()
        first = self.call('escalate', reason='Toolchain inspection needed',
                          need='Check whether the configured pack has the documented helper',
                          evidence='The missing declaration blocks the next experiment')
        second = self.call('escalate', reason='Second decision needed',
                           need='Choose whether to keep the supplied draft shape')
        self.assertEqual(first['state'], 'pending')
        self.assertEqual(life.snapshot(self.folder, 'worker01')['state'], 'claimed')
        listed = batch.help_requests('test')
        self.assertEqual(len(listed['requests']), 2)
        self.assertEqual(batch.monitor('test')['state'], 'worker_help_requested')
        answered = batch.answer('test', 'worker01', 'Inspected: the helper is absent from this pack.',
                                first['request_id'])
        self.assertEqual(answered['request_id'], first['request_id'])
        remaining = [r for r in batch.help_requests('test')['requests'] if r.get('state') == 'pending']
        self.assertEqual([r['request_id'] for r in remaining], [second['request_id']])
        batch.answer('test', 'worker01', 'Keep the supplied draft shape for now.')
        status = self.call('status')
        self.assertEqual(status['help']['responses'][0]['text'], 'Inspected: the helper is absent from this pack.')
        self.assertEqual(status['help']['responses'][1]['text'], 'Keep the supplied draft shape for now.')
        with self.assertRaisesRegex(ValueError, 'no matching pending'):
            batch.answer('test', 'worker01', 'Third answer')

    def test_voluntary_near_miss_giveup_requires_review(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() { return 0; }')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            checked = self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        self.call('gate', decision='continue', reason='Near miss', hypothesis='Search', expected='MATCH')
        notes = dict(reason='Wall', category='register_allocation', tried='Tested two source shapes',
                     still_differs='Two instructions at head', suspected_cause='Temporary lifetime')
        api = unittest.mock.Mock(return_value=dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)]))
        with self.assertRaisesRegex(ValueError, 'near_miss_review'):
            broker.worker_call('ses_child1', 'close', dict(operation='giveup', source='candidate.cpp', notes=notes), api=api)
        self.assertEqual(life.snapshot(self.folder, 'worker01')['state'], 'claimed')
        self.assertFalse((self.folder/'worker01/closure-source.cpp').exists())
        notes['near_miss_review'] = dict(best_check_id=checked['check_id'],
                                         tested='Two declaration orders and a named temporary',
                                         untried='none; both plausible source shapes were checked',
                                         stop_reason='The remaining difference is a compiler register choice, not a source shape')
        closed = False
        def api(tool, body):
            nonlocal closed
            if tool == 'status':
                return dict(open_claims=[] if closed else [dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                closed = True
                return dict(released=True, best_code='best_code kept: fixture')
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_notes')
            raise AssertionError(tool)
        broker.worker_call('ses_child1', 'close', dict(operation='giveup', source='candidate.cpp', notes=notes), api=api)
        record = life.load(self.folder/'worker01/near-miss-review.json')
        self.assertEqual(record['status'], 'accepted')
        self.assertEqual(record['request']['best_check_id'], checked['check_id'])
        broker.worker_call('ses_child1', 'checkpoint', dict(phase='notes', note='Near-miss review recorded.'), api=api)
        report = self.call('report')
        self.assertEqual(report['outcome'], 'given_up')
        self.assertEqual(report['near_miss']['status'], 'accepted')
        audit = broker.evidence.audit_worker(self.folder, life.load(self.folder/'batch.json')['tasks'][0],
                                             life.snapshot(self.folder, 'worker01'), dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertEqual(audit['evidence_errors'], [])

    def test_forced_giveup_does_not_require_near_miss_review(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() { return 0; }')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        self.call('gate', decision='continue', reason='Near miss', hypothesis='Search', expected='MATCH')
        (self.folder/'STOP').touch()
        notes = dict(reason='STOP', category='out_of_time', tried='Tested source shapes',
                     still_differs='Two instructions', suspected_cause='Temporary lifetime')
        def api(tool, body):
            if tool == 'status':
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                return dict(released=True, best_code='best_code kept: fixture')
            raise AssertionError(tool)
        broker.worker_call('ses_child1', 'close', dict(operation='giveup', source='candidate.cpp', notes=notes), api=api)
        record = life.load(self.folder/'worker01/near-miss-review.json')
        self.assertEqual(record['status'], 'not_required')
        self.assertEqual(record['reason'], 'forced_or_host_closure')

    def test_supervisor_loss_makes_near_miss_closure_forced(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() { return 0; }')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        self.call('gate', decision='continue', reason='Near miss', hypothesis='Search', expected='MATCH')
        with life.transaction(self.folder) as db:
            value = json.loads(db.execute('SELECT payload FROM control').fetchone()['payload'])
            value['heartbeat'] = life.time.monotonic()-31
            db.execute('UPDATE control SET payload=?', (json.dumps(value),))
        notes = dict(reason='Supervisor lost', category='out_of_time', tried='Source shapes',
                     still_differs='Two instructions', suspected_cause='Temporary lifetime')
        def api(tool, body):
            if tool == 'status':
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                return dict(released=True, best_code='best_code kept: fixture')
            raise AssertionError(tool)
        broker.worker_call('ses_child1', 'close', dict(operation='giveup', source='candidate.cpp', notes=notes), api=api)
        record = life.load(self.folder/'worker01/near-miss-review.json')
        self.assertEqual(record['status'], 'not_required')
        self.assertFalse(record['host']['voluntary'])

    def test_permute_unconfirmed_termination_blocks_publication_and_latches_cleanup(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() { return 0; }')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=10,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='Baseline near miss')
        self.call('gate', decision='continue', reason='Near miss', hypothesis='Search', expected='MATCH')
        outer = self
        class Search:
            returncode = 0
            stdout = 'MATCH: written to /mnt/f/private/candidate.permuted.cpp'
            stderr = ''
            pid = 4244
            def __init__(self, cmd, **kwargs):
                candidate = next((outer.folder/'worker01/permute').rglob('candidate.cpp'))
                candidate.with_name('candidate.permuted.cpp').write_text('void f() { return 1; }')
                (candidate.parent/'CANCEL.unconfirmed').write_text('group kill failed')
            def poll(self): return 0
            def communicate(self): return self.stdout, self.stderr
            def kill(self): self.returncode = -1
            def wait(self, timeout=None): return self.returncode
        with patch.object(ledger.subprocess, 'Popen', side_effect=Search), \
                patch.object(ledger.subprocess, 'run') as verification:
            failed = self.call('permute', source='candidate.cpp', stage='attempt', trials=20, jobs=1,
                          timeout_seconds=60, change='search', why='Near miss', expect='MATCH',
                          observed='two instructions differ')
            self.assertFalse(failed['succeeded'])
            self.assertEqual(failed['diagnostics']['disposition'], 'termination_unconfirmed')
            verification.assert_not_called()
        self.assertFalse(list((self.folder/'worker01/files').glob('permuted-*.cpp')))
        state = life.snapshot(self.folder, 'worker01')
        self.assertTrue(state.get('closure_requested'))
        self.assertTrue((self.folder/'worker01/CLOSE_REQUESTED.json').exists())
        record = life.load(next((self.folder/'worker01/permute').rglob('run.json')))
        self.assertTrue(record['termination_unconfirmed'])
        self.assertTrue(record['cleanup_latched'])
        self.assertEqual(record['verification_disposition'], 'termination_unconfirmed')

    def test_worker_cannot_supply_include_or_output_compiler_options(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='void f() {}')
        for flags in ('/FIsecret', '/FoC:/outside.obj', '/O2,/link', ['any'], ''):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, 'flags'):
                self.call('check', source='candidate.cpp', stage='baseline', flags=flags)

    def test_raw_completion_usage_and_incomplete_session_stop(self):
        import usage
        metadata = dict(id='ses_child1', tokens=dict(input=12, output=3, reasoning=2, cache=dict(read=8, write=0)))
        result = broker.completion('ses_child1', metadata)
        self.assertTrue(result['cleanup_required'])
        self.assertTrue((self.folder/'STOP').exists())
        collected = usage.collect(self.folder, 'subagents')
        self.assertEqual(collected['workers'][0]['raw']['input'], 12)
        self.assertIsNone(collected['workers'][0]['service_usage'])
        self.assertIsNone(collected['billed_cost'])
        self.assertIsNone(collected['workers'][1]['raw'])

    def test_target_inspection_forces_assigned_address_and_extent(self):
        process = subprocess.CompletedProcess([], 0, '{"instructions":[]}', '')
        with patch('subprocess.run', return_value=process) as run:
            self.call('target', address='0x00999999', size=100000, offset=0, limit=12)
        command = run.call_args.args[0]
        query = json.loads(command[command.index('--target-json')+1])
        self.assertEqual(query['address'], 0x00123456)
        self.assertEqual(query['size'], 100)
        self.assertEqual(query['limit'], 12)

    def test_ambiguous_checkpoint_not_retried(self):
        self.owned_fixture()
        calls = []
        def api(tool, body):
            calls.append(tool)
            raise ValueError('ambiguous transport')
        for _ in range(2):
            with self.assertRaises(ValueError):
                broker.worker_call('ses_child1', 'checkpoint', dict(phase='plan', note='FRAME 0x0 esp'), api=api)
        self.assertEqual(calls, ['checkpoint'])

    def test_legacy_launches_refused_before_process_or_network(self):
        for method in ('headless', 'workflow'):
            with self.assertRaisesRegex(ValueError, 'restricted'):
                batch.dispatch('test', method, self.root/'nonexistent.json')


class TransportTests(unittest.TestCase):
    def test_errors_are_classified_without_source_or_key_in_message(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder)/'private'; key.write_text('fake-test-key')
            for code, body, expected in ((403, b'Cloudflare error 1010 SOURCE_SECRET', 'edge_rejection'),
                                          (403, b'permission SOURCE_SECRET', 'access_denied'),
                                          (401, b'SOURCE_SECRET', 'authentication'), (429, b'', 'rate_limit')):
                error = urllib.error.HTTPError('https://f3decomp.com', code, 'error', {'Retry-After': '5'}, io.BytesIO(body))
                class Opener:
                    def open(self, req, timeout):
                        self.request = req
                        raise error
                with patch.object(transport, 'key_file', return_value=key):
                    with self.assertRaises(transport.ServiceError) as raised:
                        transport.fetch('https://f3decomp.com/api/v1/status', {}, opener=Opener())
                self.assertEqual(raised.exception.kind, expected)
                self.assertNotIn('SOURCE_SECRET', str(raised.exception))
                self.assertNotIn('fake-test-key', str(raised.exception))

    def test_redirect_and_foreign_origin_rejected(self):
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test'))
        for url in ('http://f3decomp.com', 'https://evil.test', 'https://f3decomp.com@evil.test', 'https://f3decomp.com:443'):
            with self.assertRaises(ValueError):
                transport.fetch(url)

    def test_checker_note_arguments_are_on_check_not_reclassify(self):
        args = ledger.parser().parse_args(['check', '--claim', 'clm_fake', '--source', 'x.cpp', '--stage', 'baseline',
                                          '--why', 'actual hypothesis', '--change', 'initial draft', '--expect', 'baseline'])
        self.assertEqual(args.why, 'actual hypothesis')

    def test_wrapper_command_is_repo_owned_and_never_contains_key(self):
        import checker
        command = checker.command(['--check-image'])
        self.assertIn(checker.wsl_path(checker.REPOSITORY/'tools/matchflow/check.sh'), command)
        self.assertNotIn(checker.wsl_path(batch.ROOT/'check.sh'), command)

    def test_twins_missing_cache_is_explicit_and_private(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(twins, 'workspace_root', return_value=Path(folder)):
            result = twins.lookup('0x00123456')
            self.assertFalse(result['available'])
            self.assertNotIn(twins.REPOSITORY, twins.cache_root().parents)

    def test_preflight_fails_on_stale_or_changed_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'preflight.json'
            life.save(path, dict(checked_at=life.stamp(), ready=True, fingerprint='old'))
            with patch.object(preflight, 'fingerprint', return_value='new'), self.assertRaises(ValueError):
                preflight.require(path)

    def test_twins_changed_inputs_and_output_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            life.save(root/'inputs.json', dict(version='old'))
            with patch.object(twins, 'cache_root', return_value=root), patch.object(twins, 'identity', return_value=dict(version='new')):
                with self.assertRaisesRegex(ValueError, 'stale'):
                    twins.lookup('0x00123456')
            life.save(root/'inputs.json', dict(version='new'))
            life.save(root/'result-hash.json', dict(sha256='not-actual'))
            life.save(root/'twins.json', {})
            with patch.object(twins, 'cache_root', return_value=root), patch.object(twins, 'identity', return_value=dict(version='new')):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    twins.lookup('0x00123456')

    def test_credential_configuration_cannot_point_inside_code_or_workspace(self):
        import config
        for value in (str(config.REPOSITORY/'private-key'), str(batch.ROOT/'private-key'), 'relative-key'):
            with patch.object(config, 'settings', return_value=dict(key_file=value)):
                with self.assertRaisesRegex(ValueError, 'credential'):
                    config.key_file()


if __name__ == '__main__':
    unittest.main()
