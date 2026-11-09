"""Offline regressions from the eight-worker run; never use the real workspace."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import broker
import batch
import checker
import evidence
import lifecycle as life
import matchflow as ledger
import service
import snapshots
import test_restricted as fixtures


class RunLessonsTests(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    call = fixtures.BrokerTests.call
    owned_fixture = fixtures.BrokerTests.owned_fixture

    def test_checker_uses_wsl_exec_so_notes_are_not_shell_expanded(self):
        note = '--why=literal $(printf fixture) `printf fixture`'
        cmd = checker.command([note])
        self.assertEqual(cmd[cmd.index('--exec')+1], 'bash')
        self.assertEqual(cmd[-1], note)
        self.assertNotIn('-c', cmd)

    def test_bad_notes_rejected_before_network_or_closure_reservation(self):
        self.owned_fixture()
        for notes in ('{"reason":"encoded"}', ['not an object'], 42):
            api = Mock()
            with self.subTest(notes=notes), self.assertRaisesRegex(ValueError, 'notes must be an object'):
                broker.worker_call('ses_child1', 'close', dict(operation='giveup', notes=notes,
                                   no_source_reason='No source'), api=api)
            api.assert_not_called()
            self.assertEqual(life.snapshot(self.folder, 'worker01')['state'], 'claimed')

    def test_broker_unexpected_exception_is_private_json(self):
        out = io.StringIO()
        with patch('sys.argv', ['broker.py', 'worker']), patch('sys.stdin', io.StringIO(
                json.dumps(dict(session='ses_child1', op='close')))), redirect_stdout(out), \
                patch.object(broker, 'worker_call', side_effect=AttributeError('SOURCE_OR_SECRET')):
            self.assertEqual(broker.main(), 2)
        value = json.loads(out.getvalue())
        self.assertEqual(value['error_type'], 'AttributeError')
        self.assertTrue(value['cleanup_required'])
        self.assertNotIn('SOURCE_OR_SECRET', out.getvalue())

    def test_invalid_stdin_is_json_not_traceback(self):
        for text in ('[]', 'not-json'):
            out = io.StringIO()
            with patch('sys.argv', ['broker.py', 'worker']), patch('sys.stdin', io.StringIO(text)), redirect_stdout(out):
                self.assertEqual(broker.main(), 2)
            self.assertTrue(json.loads(out.getvalue())['cleanup_required'])

    def test_ready_completion_is_worker_scoped_and_blocks_new_starts(self):
        self.owned_fixture()
        life.setup_step(self.folder, 'worker01', 'ready')
        life.reserve_claim(self.folder, 'worker02')
        life.claimed(self.folder, 'worker02', 'clm_sibling', '0x00123457',
                     life.stamp(life.utc()+life.timedelta(hours=1)))
        life.setup_step(self.folder, 'worker02', 'ready')
        result = broker.completion('ses_child1')
        self.assertTrue(result['cleanup_required'])
        self.assertFalse((self.folder/'STOP').exists())
        self.assertIn('session_finished_utc', life.snapshot(self.folder, 'worker01'))
        (self.folder/'worker01/CLOSE_REQUESTED.json').unlink()
        with self.assertRaisesRegex(ValueError, 'worker closure'):
            life.require_experiment(self.folder, 'worker01')
        # A healthy already-running sibling keeps its experiment window.
        self.assertEqual(life.require_experiment(self.folder, 'worker02')['state'], 'claimed')
        self.assertIn('sha256', broker.worker_call('ses_child2', 'write', dict(path='healthy.cpp', text='int healthy;')))
        with self.assertRaisesRegex(ValueError, 'new starts'):
            life.require_start(self.folder, 'worker02')

    def test_snapshot_survives_overwrite_and_can_close_after_stop(self):
        self.owned_fixture()
        self.call('write', path='candidate.cpp', text='int f() { return 1; }\r\n')
        result = dict(status='wip', diffs=4, ndiff_insn=2, extent=11,
                      checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(result), '')):
            checked = self.call('check', source='candidate.cpp', stage='baseline', why='Recheck supplied source')
        self.call('write', path='candidate.cpp', text='worse unchecked source')
        self.call('gate', decision='continue', reason='Test snapshot retention', hypothesis='Inspect worse source', expected='More differences')
        worse = dict(result, ndiff_insn=4, diffs=8)
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(worse), '')):
            latest = self.call('check', source='candidate.cpp', stage='attempt', why='Test later worse draft', observed='Two head instructions differ')
        (self.folder/'STOP').touch()
        checks = self.call('checks')['checks']
        self.assertEqual(checks[0]['check_id'], latest['check_id'])
        self.assertTrue(checks[0]['snapshot_available'])
        self.assertNotIn('evidence_path', checks[0])
        notes = dict(reason='Stopped at wall', category='instruction_order', tried='Tried direct return',
                     still_differs='Two instructions at head', suspected_cause='Temporary lifetime')
        def api(tool, body):
            if tool == 'status':
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                self.assertEqual(body['best_code'], 'int f() { return 1; }\r\n')
                return dict(released=True, best_code='best_code kept: fixture')
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_fixture')
            raise AssertionError(tool)
        broker.worker_call('ses_child1', 'close', dict(operation='giveup', check_id=checked['check_id'], notes=notes), api=api)
        broker.worker_call('ses_child1', 'checkpoint', dict(phase='notes', note='Two head instructions remain.'), api=api)
        report = self.call('report')
        self.assertEqual(report['retained_check']['check_id'], checked['check_id'])
        self.assertEqual(report['latest_valid']['check_id'], latest['check_id'])
        task = life.load(self.folder/'batch.json')['tasks'][0]
        audited = evidence.audit_worker(self.folder, task, life.snapshot(self.folder, 'worker01'),
                                        dict(open_claims=[]), ledger.DEFAULT_DB)
        self.assertEqual(audited['evidence_errors'], [])
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db:
            with self.assertRaisesRegex(ValueError, 'own-claim'):
                snapshots.checked_source(db, 'clm_sibling', checked['check_id'])
            path = snapshots.checked_source(db, 'clm_fixture', checked['check_id'])
        path.write_text('tamper', encoding='utf-8')
        with closing(ledger.connect(ledger.DEFAULT_DB)) as db, self.assertRaisesRegex(ValueError, 'hash changed'):
            snapshots.checked_source(db, 'clm_fixture', checked['check_id'])

    def test_monitor_rearm_waits_for_new_request_or_escalation(self):
        self.owned_fixture()
        life.setup_step(self.folder, 'worker01', 'ready')
        broker.completion('ses_child1')
        with patch.object(life, 'phase', return_value='experiment') as phase:
            first = batch.monitor('test')
            self.assertEqual([w['worker'] for w in first['workers']], ['worker01'])
            def escalate(_):
                phase.return_value = 'reconcile'
            with patch.object(batch.time, 'sleep', side_effect=escalate) as sleep:
                second = batch.monitor('test')
            sleep.assert_called_once()
            self.assertTrue(all(w['phase'] == 'reconcile' for w in second['workers']))

    def test_malformed_mutation_response_is_saved_and_never_replayed(self):
        self.owned_fixture()
        notes = dict(reason='Wall', category='other', tried='Checked baseline',
                     still_differs='Head', suspected_cause='Temporary lifetime')
        calls = []
        def api(tool, body):
            calls.append(tool)
            if tool == 'status':
                return dict(open_claims=[dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            return ['invalid response']
        with self.assertRaisesRegex(ValueError, 'acknowledgement must be an object'):
            service.close(self.folder, 'worker01', 'giveup', notes=notes, no_source_reason='No candidate', api=api)
        self.assertEqual(life.load(self.folder/'worker01/service-ack.json')['response'], ['invalid response'])
        self.assertEqual(life.snapshot(self.folder, 'worker01')['state'], 'close_pending')
        with self.assertRaises(ValueError):
            service.reconcile(self.folder, 'worker01', api=api)
        self.assertEqual(calls.count('giveup'), 1)


if __name__ == '__main__':
    unittest.main()
