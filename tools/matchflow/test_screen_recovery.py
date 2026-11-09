from contextlib import closing
import json
from pathlib import Path
import unittest
import subprocess
from unittest.mock import Mock, patch

import broker
import batch
import evidence
import lifecycle as life
import matchflow as ledger
import recovery
import screen
from transport import ServiceError
import test_restricted as fixtures


class ScreenRecoveryTests(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    call = fixtures.BrokerTests.call
    owned_fixture = fixtures.BrokerTests.owned_fixture

    def catalog(self):
        return [dict(address='0x00123456', size=100, band='<=255', give_ups=1,
                     open_problem_reports=0, file_problem_reports=1, failed_verifications=0),
                dict(address='0x00123457', size=120, band='<=255', give_ups=2,
                     open_problem_reports=0, file_problem_reports=0, failed_verifications=0)]

    def test_screen_is_bounded_read_only_and_not_a_selection(self):
        calls = []
        def api(tool, body):
            calls.append((tool, body))
            if tool == 'start_here':
                return dict(notices=['fixture notice'])
            if tool == 'status':
                return dict(open_claims=[])
            if tool == 'list_bands':
                return dict(bands=[dict(band='<=255', allowed=True, open=2)])
            if tool == 'list_functions':
                return dict(functions=self.catalog(), next=None)
            if tool == 'function_info':
                f = next(f for f in self.catalog() if f['address'] == body['address'])
                return dict(f, state='open', co_workers=0)
            raise AssertionError('unexpected mutation '+tool)
        result = screen.screen(self.root/'screen', count=1, api=api)
        self.assertEqual([c[0] for c in calls], ['start_here', 'status', 'list_bands', 'list_functions', 'function_info'])
        self.assertTrue(result['review_required'])
        review = life.load(self.root/'screen/review.json')
        self.assertFalse(review['shortlist'][0]['history_reviewed'])
        self.assertNotIn('new_hypothesis', review['shortlist'][0])
        self.assertNotIn('targets', review)
        self.assertEqual(result['notices'], ['fixture notice'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            screen.screen(self.root/'screen', api=Mock())

    def test_screen_cooldown_stops_and_preserves_earlier_responses(self):
        calls = []
        def api(tool, body):
            calls.append(tool)
            if tool == 'list_bands':
                raise ServiceError(503, '60')
            return dict(open_claims=[])
        with self.assertRaises(ServiceError):
            screen.screen(self.root/'screen', api=api)
        self.assertEqual(calls, ['start_here', 'status', 'list_bands'])
        self.assertTrue((self.root/'screen/status.json').exists())
        self.assertFalse((self.root/'screen/review.json').exists())
        self.assertEqual(life.load(self.root/'screen/service-error.json')['retry_after'], '60')

    def test_screen_does_not_refill_raced_target(self):
        def api(tool, body):
            if tool == 'list_bands':
                return dict(bands=[dict(band='<=255', allowed=True, open=2)])
            if tool == 'list_functions':
                return dict(functions=self.catalog(), next=None)
            if tool == 'function_info':
                return dict(address=body['address'], state='held')
            return dict(open_claims=[])
        result = screen.screen(self.root/'screen', count=1, api=api)
        self.assertFalse(result['complete'])
        self.assertEqual(result['count'], 0)

    def recovery_action(self):
        return dict(batch='test', workers=[dict(worker='worker01', address='0x00123456', claim_id='clm_fixture',
                session_id='ses_child1', session_finished=True, operation='giveup', source='best.cpp',
                source_sha256=ledger.digest(self.folder/'worker01/files/best.cpp'),
                notes=dict(reason='Register wall', category='register_allocation', tried='Direct return',
                           still_differs='Two instructions', suspected_cause='Local lifetime'),
                closing_note='Two head instructions still differ; try shorter local lifetime next.')])

    def test_recovery_inventory_never_mutates_or_releases_unrelated_claim(self):
        self.owned_fixture()
        status = dict(open_claims=[dict(claim_id='clm_unrelated', address='0x00999999', this_key=False)])
        result = recovery.inventory(self.folder, status, ledger.DEFAULT_DB)
        self.assertEqual(result['unrelated_open_claims'], ['clm_unrelated'])
        self.assertEqual(life.snapshot(self.folder, 'worker01')['state'], 'claimed')

    def test_recovery_requires_finished_session_and_exact_review_identity(self):
        self.owned_fixture()
        self.call('write', path='best.cpp', text='int f() { return 1; }')
        api = Mock()
        with self.assertRaisesRegex(ValueError, 'completion evidence'):
            recovery.apply(self.folder, self.recovery_action(), ledger.DEFAULT_DB, api)
        api.assert_not_called()
        actions = self.recovery_action(); actions['workers'][0]['claim_id'] = 'clm_unrelated'
        with self.assertRaisesRegex(ValueError, 'identity'):
            recovery.apply(self.folder, actions, ledger.DEFAULT_DB, api)
        api.assert_not_called()

    def test_reviewed_recovery_close_notes_report_and_audit_without_retries(self):
        self.owned_fixture()
        self.call('write', path='best.cpp', text='int f() { return 1; }')
        life.setup_step(self.folder, 'worker01', 'ready')
        broker.completion('ses_child1')
        calls = []
        closed = False
        def api(tool, body):
            nonlocal closed
            calls.append(tool)
            if tool == 'status':
                return dict(open_claims=[] if closed else [dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                closed = True
                return dict(released=True, best_code='best_code kept: fixture')
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_fixture')
            raise AssertionError(tool)
        result = recovery.apply(self.folder, self.recovery_action(), ledger.DEFAULT_DB, api)
        self.assertTrue(result['ok'])
        self.assertEqual(calls.count('giveup'), 1)
        self.assertEqual(calls.count('checkpoint'), 1)
        report = life.load(self.folder/'worker01/report.json')
        self.assertEqual(report['service']['source_retention'], 'retained')
        before = (self.folder/'worker01/report.json').read_bytes()
        finish = self.recovery_action()
        finish['workers'][0]['operation'] = 'finish'
        del finish['workers'][0]['closing_note']
        recovery.apply(self.folder, finish, ledger.DEFAULT_DB, api)
        self.assertEqual(calls.count('giveup'), 1)
        self.assertEqual(calls.count('checkpoint'), 1)
        self.assertEqual((self.folder/'worker01/report.json').read_bytes(), before)

    def test_pending_recovery_is_not_replayed(self):
        self.owned_fixture()
        self.call('write', path='best.cpp', text='int f() { return 1; }')
        life.setup_step(self.folder, 'worker01', 'ready'); broker.completion('ses_child1')
        life.begin_close(self.folder, 'worker01')
        api = Mock()
        with self.assertRaisesRegex(ValueError, 'pending closure'):
            recovery.apply(self.folder, self.recovery_action(), ledger.DEFAULT_DB, api)
        api.assert_not_called()

    def test_recovery_rejects_changed_prepared_assignment_before_mutation(self):
        self.owned_fixture()
        self.call('write', path='best.cpp', text='int f() { return 1; }')
        path = self.folder/'worker01/assignment.json'
        data = life.load(path); data['target']['address'] = '0x00999999'; life.save(path, data)
        api = Mock()
        with self.assertRaisesRegex(ValueError, 'immutable recovery assignment'):
            recovery.apply(self.folder, self.recovery_action(), ledger.DEFAULT_DB, api)
        api.assert_not_called()

    def test_batch_recover_is_inventory_unless_explicitly_applied(self):
        self.owned_fixture()
        api = Mock(return_value=dict(open_claims=[]))
        result = batch.recover('test', api=api)
        self.assertEqual(len(result['workers']), 2)
        api.assert_called_once_with('status', {})
        with self.assertRaisesRegex(ValueError, 'requires an explicit'):
            batch.recover('test', apply=True, api=Mock())

    def test_recovery_closes_selected_checked_snapshot_not_overwritten_file(self):
        self.owned_fixture()
        self.call('write', path='best.cpp', text='int f() { return 1; }')
        data = dict(status='wip', ndiff_insn=1, diffs=2, extent=11,
                    checker='checker: f3check test, compiler VS2005 SP1')
        with patch.object(ledger.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, json.dumps(data), '')):
            checked = self.call('check', source='best.cpp', stage='baseline', why='Inspect exact source')
        actions = self.recovery_action()
        selected = actions['workers'][0]
        del selected['source']; del selected['source_sha256']
        selected['check_id'] = checked['check_id']
        self.call('write', path='best.cpp', text='overwritten')
        life.setup_step(self.folder, 'worker01', 'ready'); broker.completion('ses_child1')
        closed = False
        def api(tool, body):
            nonlocal closed
            if tool == 'status':
                return dict(open_claims=[] if closed else [dict(claim_id='clm_fixture', address='0x00123456', this_key=True)])
            if tool == 'giveup':
                self.assertEqual(body['best_code'], 'int f() { return 1; }')
                closed = True
                return dict(released=True, best_code='best_code kept: fixture')
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_fixture')
            raise AssertionError(tool)
        self.assertTrue(recovery.apply(self.folder, actions, ledger.DEFAULT_DB, api)['ok'])
        self.assertEqual(life.load(self.folder/'worker01/report.json')['retained_check']['check_id'], checked['check_id'])


if __name__ == '__main__':
    unittest.main()
