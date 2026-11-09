import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import batch


class BatchTests(unittest.TestCase):
    def selection(self, **target):
        item = dict(address='0x00123456', size=100, state='open', co_workers=0,
                    give_ups=0, open_problem_reports=0, file_problem_reports=0,
                    lane='production', reason='Reviewed current metadata', history_reviewed=True,
                    history_sources=['offline fixture'], prior_verdicts=[], verdict_history='reviewed')
        item.update(target)
        return dict(checked_at=batch.now(), targets=[item])

    def test_default_model(self):
        self.assertEqual(batch.DEFAULT_MODEL, 'opencode-go/deepseek-v4.1-flash#max')

    def test_valid_selection(self):
        self.assertEqual(len(batch.validate_selection(self.selection())), 1)

    def test_prior_verdict_routes_integration_even_without_prior_unfit_flag(self):
        with self.assertRaisesRegex(ValueError, 'integration'):
            batch.validate_selection(self.selection(prior_verdicts=['unfit']))

    def test_unknown_history_not_clean_production(self):
        with self.assertRaisesRegex(ValueError, 'unknown verdict'):
            batch.validate_selection(self.selection(verdict_history='unknown'))

    def test_research_requires_specific_hypothesis(self):
        with self.assertRaisesRegex(ValueError, 'new_hypothesis'):
            batch.validate_selection(self.selection(lane='research', give_ups=4))

    def test_daily_and_pending_capacity_checked_before_launch(self):
        status = dict(limits=dict(claims=20, subsPerDay=10, pending=10), open_claims=[], submissions_today=9)
        with self.assertRaisesRegex(ValueError, 'daily'):
            batch.capacity(dict(checked_at=batch.now(), status=status), 2)
        status.update(submissions_today=0, awaiting_verification=9)
        with self.assertRaisesRegex(ValueError, 'pending'):
            batch.capacity(dict(checked_at=batch.now(), status=status), 2)

    def test_all_methods_share_dispatch_lock_and_watchdog(self):
        for method in ('subagents',):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory, \
                    patch.object(batch, 'ROOT', Path(directory)), \
                    patch.object(batch, 'BATCHES', Path(directory)/'batches'):
                selection = Path(directory)/'selection.json'
                batch.save(selection, self.selection())
                folder = Path(batch.prepare('test', selection)['batch'])
                status = Path(directory)/'status.json'
                batch.save(status, dict(checked_at=batch.now(), status=dict(limits=dict(claims=20), open_claims=[])))
                with patch.object(batch.subprocess, 'Popen') as process, patch('preflight.require'):
                    process.return_value.pid = 123
                    batch.dispatch('test', method, status)
                    process.assert_called_once()
                    with self.assertRaisesRegex(ValueError, 'already dispatched'):
                        batch.dispatch('test', method, status)
                self.assertEqual(batch.lifecycle.snapshot(folder, 'worker01')['method'], method)
                self.assertEqual(batch.lifecycle.require_experiment(folder, 'worker01')['state'], 'dispatched')

    def test_duplicate_rejected(self):
        data = self.selection(); data['targets'] *= 2
        with self.assertRaises(ValueError): batch.validate_selection(data)

    def test_band_rejected(self):
        with self.assertRaises(ValueError): batch.validate_selection(self.selection(address='<=255'))

    def test_no_address_specific_exclusion(self):
        targets = batch.validate_selection(self.selection(address='0x00ad1250'))
        self.assertEqual(targets[0]['address'], '0x00ad1250')

    def test_held_rejected(self):
        with self.assertRaises(ValueError): batch.validate_selection(self.selection(co_workers=1))

    def test_stale_rejected(self):
        data = self.selection(); data['checked_at'] = '2020-01-01T00:00:00Z'
        with self.assertRaises(ValueError): batch.validate_selection(data)

    def test_no_silent_research_as_production(self):
        with self.assertRaises(ValueError): batch.validate_selection(self.selection(give_ups=3))

    def test_unfit_requires_integration(self):
        with self.assertRaises(ValueError): batch.validate_selection(self.selection(prior_unfit=True, lane='research'))

    def test_cap_cannot_be_bypassed(self):
        snapshot = dict(checked_at=batch.now(), status=dict(limits=dict(claims=20), open_claims=[{}]))
        with self.assertRaises(ValueError): batch.capacity(snapshot, 20)

    def test_pause_rejected(self):
        snapshot = dict(checked_at=batch.now(), status=dict(limits=dict(claims=20), open_claims=[], submissions_paused=True))
        with self.assertRaises(ValueError): batch.capacity(snapshot, 1)

    def test_prepare_and_audit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(batch, 'BATCHES', Path(directory)/'batches'), \
                patch.object(batch, 'ROOT', Path(directory)):
            selection = Path(directory)/'selection.json'; batch.save(selection, self.selection())
            result = batch.prepare('test', selection)
            folder = Path(result['batch'])
            assignment = batch.load(folder/'worker01/assignment.json')
            self.assertEqual(assignment['claim_body']['address'], '0x00123456')
            self.assertNotIn('band', assignment['claim_body'])
            self.assertEqual(assignment['claim_body']['effort'], 'max')
            self.assertEqual(batch.load(folder/'batch.json')['tasks'][0]['agent'], 'match-worker')
            self.assertFalse((folder/'workflow-args.json').exists())
            with self.assertRaises(FileExistsError): batch.prepare('test', selection)
            status = Path(directory)/'status.json'
            batch.save(status, dict(checked_at=batch.now(), status=dict(open_claims=[dict(claim_id='unrelated')])) )
            audit = batch.audit('test', status)
            self.assertTrue(audit['workers'][0]['cleanup_required'])
            self.assertEqual(audit['unrelated_open_claims'], ['unrelated'])
            batch.save(folder/'worker01/ownership.json',dict(address='0x00123456',claim_id=None,outcome='blocked'))
            self.assertFalse(batch.audit('test', status)['workers'][0]['cleanup_required'])
            self.assertTrue(batch.audit('test', status)['workers'][0]['evidence_review_required'])

    def test_slug_rejects_escape(self):
        with self.assertRaises(ValueError): batch.batch_path('../escape')

    def test_headless_disabled(self):
        class FakeProcess:
            pid = 123
            stdout = io.StringIO('{"sessionID":"ses_test","part":{"type":"text","text":"RESULT mock"}}\n')
            def wait(self, timeout): return 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); (path/'worker01').mkdir()
            task = dict(id='worker01', address='0x00123456', model=batch.DEFAULT_MODEL, prompt='mock')
            batch.save(path/'batch.json', dict(tasks=[task]))
            batch.lifecycle.dispatch(path, 'headless')
            batch.lifecycle.tick(path, 'test-watcher')
            with patch.object(batch.subprocess, 'Popen', return_value=FakeProcess()) as launch:
                with self.assertRaisesRegex(ValueError, 'disabled'):
                    batch.run_worker(path, task, Path('bundled.exe'))
                launch.assert_not_called()

    def test_stop_marker_prevents_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); (path/'STOP').touch()
            with patch.object(batch.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'disabled'):
                    batch.run_worker(path, dict(id='worker01'), Path('bundled.exe'))
                launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
