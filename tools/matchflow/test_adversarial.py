"""Adversarial regressions. Synthetic sources, scratch workspaces and fake APIs only."""
import argparse
import copy
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import broker
import checker
import check_runner
import evidence
import lifecycle as life
import matchflow as ledger
import service
import test_restricted as fixtures


class RunnerBoundary(unittest.TestCase):
    def test_option_looking_notes_cannot_replace_trusted_runtime(self):
        class Captured(Exception):
            pass
        original_loads = json.loads
        seen = []
        command = checker.command(['--why', '--runtime-json={"checker":"worker-controlled"}', 'candidate.cpp'])
        runner_args = command[command.index('--runtime-json'):]
        trusted = original_loads(runner_args[1])
        def capture(value):
            seen.append(original_loads(value))
            raise Captured()
        with patch.object(sys, 'argv', ['runner.py', *runner_args]), patch.object(check_runner.json, 'loads', side_effect=capture):
            with self.assertRaises(Captured):
                check_runner.main()
        self.assertEqual(seen[0], trusted)

    def test_all_privileged_options_are_opaque_after_boundary(self):
        trusted = ['--runtime-json', '{"checker":"trusted"}', '--key-file', '/trusted/key', '--mode', 'check']
        for value in ('--runtime-json={}', '--key-file=/other/key', '--mode=preflight', '--runtime-j={}', '--'):
            payload = ['--why', value, 'candidate.cpp']
            args, remaining = check_runner.parse_invocation([*trusted, '--', *payload])
            self.assertEqual(args.runtime_json, '{"checker":"trusted"}')
            self.assertEqual(args.key_file, '/trusted/key')
            self.assertEqual(args.mode, 'check')
            self.assertEqual(remaining, payload)

    def test_missing_boundary_is_not_accepted_as_legacy_fallback(self):
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            check_runner.parse_invocation(['--runtime-json', '{}', '--key-file', '/trusted/key'])


class ReviewRegressions(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    call = fixtures.BrokerTests.call
    owned_fixture = fixtures.BrokerTests.owned_fixture

    def fake_api(self, tool, body):
        if tool == 'status':
            return dict(limits=dict(claims=20), open_claims=[dict(claim_id='clm_fixture', address='0x00123456',
                this_key=True, expires=life.stamp(life.utc()+life.timedelta(hours=1)))] if getattr(self, 'remote_claimed', False) else [])
        if tool == 'function_info':
            return dict(state='open', address=body['address'], co_workers=0)
        if tool == 'claim':
            self.remote_claimed = True
            return dict(claim_id='clm_fixture', work_notes='light')
        if tool == 'packet':
            return dict(pack_url='https://f3decomp.com/private-pack', work_notes='light')
        if tool == 'giveup':
            self.remote_claimed = False
            return dict(released=True)
        if tool == 'submit':
            self.remote_claimed = False
            return dict(submission_id='sub_fixture')
        if tool == 'checkpoint':
            return dict(checkpoint_id='chk_notes_fixture')
        raise AssertionError(tool)

    def pack_failure(self):
        def fail(*args):
            raise ValueError('offline fixture pack download failure')
        with self.assertRaisesRegex(ValueError, 'pack download'):
            broker.start('ses_child1', api=self.fake_api, acquire=fail)

    def compiled_fixture(self):
        self.owned_fixture()
        self.remote_claimed = True
        self.call('write', path='candidate.cpp', text='void f() {}')
        self.result = dict(status='match', extent=10, diffs=0, ndiff_insn=0,
                           checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(self.result), '')):
            self.call('check', source='candidate.cpp', stage='baseline', why='baseline fixture')
        self.call('gate', decision='continue', reason='fixture gate', hypothesis='retain wrapper', expected='exact match')

    def submitted_fixture(self):
        self.compiled_fixture()
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(self.result), '')):
            self.call('check', source='candidate.cpp', stage='final', why='final fixture')
        self.call('promote')
        broker.worker_call('ses_child1', 'close', dict(operation='submit'), api=self.fake_api)

    def test_final_stage_cannot_bypass_stop(self):
        self.compiled_fixture()
        (self.folder/'STOP').touch()
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(self.result), '')) as run:
            with self.assertRaisesRegex(ValueError, 'STOP'):
                self.call('check', source='candidate.cpp', stage='final', why='should not compile')
            run.assert_not_called()

    def test_pack_failure_blocks_another_prepared_assignment(self):
        self.pack_failure()
        self.assertEqual(life.snapshot(self.folder, 'worker01')['claim_id'], 'clm_fixture')
        with self.assertRaisesRegex(ValueError, 'cleanup|STOP'):
            broker.task('test', 'worker02', 'ses_parent')

    def test_acknowledged_setup_failure_has_report_without_checker_records(self):
        self.pack_failure()
        notes = dict(reason='pack setup failed', category='pack_problem', tried='download official pack',
                     still_differs='no compilation possible', suspected_cause='download transport failure')
        service.close(self.folder, 'worker01', 'giveup', notes=notes, no_source_reason='Pack setup failed before compilation',
                      db_path=ledger.DEFAULT_DB, api=self.fake_api)
        service.closing_notes(self.folder, 'worker01', 'Setup failed before any source or check existed.', api=self.fake_api)
        report = evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)
        self.assertIsNone(report['pack_path'])
        self.assertIsNone(report['baseline'])
        self.assertIsNone(report['final'])
        state = life.snapshot(self.folder, 'worker01')
        task = life.load(self.folder/'batch.json')['tasks'][0]
        audit = evidence.audit_worker(self.folder, task, state, dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertEqual(audit['evidence_errors'], [])

    def test_report_cannot_complete_without_acknowledged_closing_notes(self):
        self.submitted_fixture()
        with self.assertRaisesRegex(ValueError, 'closing notes'):
            evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)

    def test_audit_independently_requires_closing_notes(self):
        self.submitted_fixture()
        service.closing_notes(self.folder, 'worker01', 'Literal wrapper matched.', api=self.fake_api)
        evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)
        path = self.folder/'worker01/closing-notes.json'
        if path.exists():
            path.unlink()
        task = life.load(self.folder/'batch.json')['tasks'][0]
        audit = evidence.audit_worker(self.folder, task, life.snapshot(self.folder, 'worker01'),
                                      dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertEqual(audit['evidence_integrity'], 'invalid')
        self.assertTrue(any('closing notes' in error for error in audit['evidence_errors']))

    def test_every_stage_and_changed_candidate_are_blocked_by_stop(self):
        self.compiled_fixture()
        self.call('write', path='alternate.cpp', text='void f() { int different = 1; }')
        (self.folder/'STOP').touch()
        with patch.object(ledger.subprocess, 'run') as run:
            for stage in ('baseline', 'attempt', 'final'):
                for flags in (None, '/MT,/O1'):
                    with self.subTest(stage=stage, flags=flags), self.assertRaisesRegex(ValueError, 'STOP'):
                        self.call('check', source='alternate.cpp', stage=stage, flags=flags, why='not a closure exception')
            run.assert_not_called()

    def test_final_checks_reject_stale_watchdog_closure_and_deadline(self):
        self.compiled_fixture()
        original = life.snapshot(self.folder, 'worker01')
        with life.transaction(self.folder) as db:
            control = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()[0])
        for condition in ('stale', 'closure_marker', 'experiment_deadline', 'reconcile_deadline'):
            with self.subTest(condition=condition):
                current = copy.deepcopy(original)
                current_control = copy.deepcopy(control)
                if condition == 'stale':
                    current_control['heartbeat'] = life.time.monotonic()-31
                elif condition == 'closure_marker':
                    (self.folder/'worker01/CLOSE_REQUESTED.json').touch()
                elif condition == 'experiment_deadline':
                    current['experiment_seconds'] = 0
                else:
                    current['closure_deadline_utc'] = life.stamp(life.utc()-life.timedelta(seconds=1))
                with life.transaction(self.folder) as db:
                    life.write_worker(db, 'worker01', current)
                    db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(current_control),))
                with patch.object(ledger.subprocess, 'run') as run:
                    with self.assertRaises(ValueError):
                        self.call('check', source='candidate.cpp', stage='final', why='blocked final')
                    run.assert_not_called()
                marker = self.folder/'worker01/CLOSE_REQUESTED.json'
                if marker.exists():
                    marker.unlink()

    def test_already_final_checked_source_can_still_promote_and_close_after_stop(self):
        self.compiled_fixture()
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(self.result), '')):
            self.call('check', source='candidate.cpp', stage='final', why='checked before stop')
        (self.folder/'STOP').touch()
        self.call('promote')
        result = broker.worker_call('ses_child1', 'close', dict(operation='submit'), api=self.fake_api)
        self.assertTrue(result['lease_closed'])
        service.closing_notes(self.folder, 'worker01', 'Unchanged final artifact submitted after stop.', api=self.fake_api)
        self.assertEqual(self.call('report')['outcome'], 'submitted')

    def test_cleanup_barrier_survives_stop_marker_removal_and_blocks_direct_claim(self):
        self.pack_failure()
        (self.folder/'STOP').unlink()
        with self.assertRaisesRegex(ValueError, 'cleanup barrier'):
            broker.task('test', 'worker02', 'ses_parent')
        with self.assertRaisesRegex(ValueError, 'cleanup barrier'):
            service.claim(self.folder, 'worker02', api=lambda *args: self.fail('must not make a service call'))

    def test_packet_failure_persists_barrier_before_another_start(self):
        def api(tool, body):
            if tool == 'packet':
                raise TimeoutError('offline fixture lost packet')
            return self.fake_api(tool, body)
        with self.assertRaises(TimeoutError):
            broker.start('ses_child1', api=api)
        self.assertEqual(life.snapshot(self.folder, 'worker01')['setup_failure']['stage'], 'packet')
        with self.assertRaisesRegex(ValueError, 'cleanup barrier'):
            broker.start('ses_child2', api=self.fake_api)
        self.assertFalse(life.snapshot(self.folder, 'worker02')['claim_attempted'])

    def test_registration_failure_preserves_setup_evidence_without_fake_attempt(self):
        def acquire(workspace, claim, packet):
            root = workspace/'packs'/claim; root.mkdir(parents=True)
            life.save(root/'pack.json', dict(address='0x00123456', flags=['/MT', '/O2']))
            return root
        with patch.object(ledger, 'register', side_effect=ValueError('fixture registration failure')):
            with self.assertRaisesRegex(ValueError, 'registration failure'):
                broker.start('ses_child1', api=self.fake_api, acquire=acquire)
        self.assertEqual(life.snapshot(self.folder, 'worker01')['setup_failure']['stage'], 'register')
        with self.assertRaisesRegex(ValueError, 'cleanup barrier'):
            broker.task('test', 'worker02', 'ses_parent')
        notes = dict(reason='setup', category='other', tried='register', still_differs='not compiled', suspected_cause='local setup')
        service.close(self.folder, 'worker01', 'giveup', notes=notes, no_source_reason='Registration failed before compilation',
                      db_path=ledger.DEFAULT_DB, api=self.fake_api)
        service.closing_notes(self.folder, 'worker01', 'Registration failed; no check ran.', api=self.fake_api)
        report = evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)
        self.assertEqual(report['setup_failure']['stage'], 'register')
        self.assertIsNone(report['pack_sha256'])

    def test_interrupted_host_fallback_persists_barrier_without_replaying_claim(self):
        life.reserve_claim(self.folder, 'worker01')
        life.setup_step(self.folder, 'worker01', 'claim')
        result = broker.failure('test', 'worker01', 'ses_child1', 'ses_parent')
        self.assertTrue(result['stopped'])
        self.assertEqual(life.snapshot(self.folder, 'worker01')['state'], 'claim_pending')
        with self.assertRaisesRegex(ValueError, 'cleanup barrier'):
            broker.task('test', 'worker02', 'ses_parent')

    def test_setup_report_rejects_invented_pack_or_checker_evidence(self):
        self.pack_failure()
        notes = dict(reason='setup', category='pack_problem', tried='download', still_differs='not checked', suspected_cause='transport')
        service.close(self.folder, 'worker01', 'giveup', notes=notes, no_source_reason='No compilation occurred',
                      db_path=ledger.DEFAULT_DB, api=self.fake_api)
        service.closing_notes(self.folder, 'worker01', 'Setup failed before compilation.', api=self.fake_api)
        report = evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)
        task = life.load(self.folder/'batch.json')['tasks'][0]
        for field, value in (('pack_path', 'invented-pack'), ('baseline', {})):
            damaged = copy.deepcopy(report); damaged[field] = value
            life.save(self.folder/'worker01/report.json', damaged)
            audit = evidence.audit_worker(self.folder, task, life.snapshot(self.folder, 'worker01'), dict(open_claims=[]), ledger.DEFAULT_DB)
            self.assertEqual(audit['evidence_integrity'], 'invalid')
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM checks').fetchone()[0], 0)

    def test_setup_report_requires_real_saved_ownership(self):
        self.pack_failure()
        notes = dict(reason='setup', category='pack_problem', tried='download', still_differs='not checked', suspected_cause='transport')
        service.close(self.folder, 'worker01', 'giveup', notes=notes, no_source_reason='No compilation occurred',
                      db_path=ledger.DEFAULT_DB, api=self.fake_api)
        service.closing_notes(self.folder, 'worker01', 'Setup failed before compilation.', api=self.fake_api)
        path = self.folder/'worker01/claim-status.json'
        saved = life.load(path); saved['status']['open_claims'][0]['this_key'] = False; life.save(path, saved)
        with self.assertRaisesRegex(ValueError, 'own-claim confirmation'):
            evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)

    def test_preclosure_notes_do_not_satisfy_closing_notes(self):
        self.compiled_fixture()
        with self.assertRaisesRegex(ValueError, 'closure first'):
            broker.worker_call('ses_child1', 'checkpoint', dict(phase='notes', note='Too early'), api=self.fake_api)

    def test_pending_closing_notes_are_not_retried_or_reported_complete(self):
        self.submitted_fixture()
        calls = []
        def api(tool, body):
            calls.append(tool)
            raise TimeoutError('offline fixture lost checkpoint response')
        with self.assertRaises(TimeoutError):
            service.closing_notes(self.folder, 'worker01', 'Exact wrapper matched.', api=api)
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            service.closing_notes(self.folder, 'worker01', 'Exact wrapper matched.', api=api)
        with self.assertRaisesRegex(ValueError, 'closing notes'):
            evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)
        self.assertEqual(calls, ['checkpoint'])
        self.assertTrue(broker.completion('ses_child1')['cleanup_required'])

    def test_no_checkpoint_id_is_not_an_acknowledgement(self):
        self.submitted_fixture()
        with self.assertRaisesRegex(ValueError, 'not acknowledged'):
            service.closing_notes(self.folder, 'worker01', 'Exact wrapper matched.', api=lambda *args: {})
        with self.assertRaisesRegex(ValueError, 'closing notes'):
            evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)

    def test_notes_receipt_binds_claim_phase_session_time_and_hash(self):
        self.submitted_fixture()
        service.closing_notes(self.folder, 'worker01', 'Exact wrapper matched.', api=self.fake_api)
        evidence.report(self.folder, 'worker01', ledger.DEFAULT_DB)
        path = self.folder/'worker01/closing-notes.json'
        original = life.load(path)
        task = life.load(self.folder/'batch.json')['tasks'][0]
        for condition in ('claim', 'phase', 'session', 'timestamp', 'checkpoint'):
            damaged = copy.deepcopy(original)
            if condition == 'claim':
                damaged['request']['claim_id'] = 'clm_other'
            elif condition == 'phase':
                damaged['request']['phase'] = 'plan'
            elif condition == 'session':
                damaged['session_id'] = 'ses_other'
            elif condition == 'timestamp':
                damaged['requested_utc'] = life.stamp(life.utc()-life.timedelta(hours=1))
            else:
                damaged['response']['checkpoint_id'] = 'chk_other'
            life.save(path, damaged)
            audit = evidence.audit_worker(self.folder, task, life.snapshot(self.folder, 'worker01'), dict(open_claims=[]), ledger.DEFAULT_DB)
            self.assertEqual(audit['evidence_integrity'], 'invalid')
            self.assertTrue(any('closing notes' in error for error in audit['evidence_errors']))
