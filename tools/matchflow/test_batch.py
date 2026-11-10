import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import batch
import review


class BatchTests(unittest.TestCase):
    def selection(self, review=None, **target):
        item = dict(address='0x00123456', size=100, state='open', co_workers=0,
                    give_ups=0, open_problem_reports=0, file_problem_reports=0,
                    reason='Reviewed current metadata')
        item['review'] = dict(history_reviewed=True, history_sources=['offline fixture'],
                              prior_verdicts=[], verdict_history='reviewed', report_scope='none',
                              report_evidence=None, prior_unfit=False,
                              next_experiment=None, integration_plan=None)
        if review:
            item['review'].update(review)
        item.update(target)
        return dict(checked_at=batch.now(), targets=[item])

    def test_default_model(self):
        self.assertEqual(batch.DEFAULT_MODEL, 'opencode-go/deepseek-v4.1-flash#max')

    def test_prepare_freezes_local_model_and_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selection = root/'selection.json'; batch.save(selection, self.selection())
            with patch.object(batch, 'ROOT', root), patch.object(batch, 'BATCHES', root/'batches'), \
                    patch('config.settings', return_value=dict(worker_model='custom/chat#high')):
                local = batch.prepare('local', selection)
                explicit = batch.prepare('explicit', selection, 'other/model')
            for prepared, model, effort in ((local, 'custom/chat#high', 'high'), (explicit, 'other/model', None)):
                folder = Path(prepared['batch'])
                manifest = batch.load(folder/'batch.json')
                assignment = batch.load(folder/'worker01/assignment.json')
                self.assertEqual(manifest['model'], model)
                self.assertEqual(manifest['tasks'][0]['model'], model)
                self.assertEqual(assignment['model'], model)
                self.assertEqual(assignment['claim_body']['model'], model.split('#')[0])
                self.assertEqual(assignment['claim_body'].get('effort'), effort)
            with patch('config.settings', return_value=dict(worker_model='invalid')):
                with self.assertRaisesRegex(ValueError, 'worker_model'):
                    batch.prepare('invalid', selection)

    def test_malformed_prior_verdicts_is_a_clear_value_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selection = root/'selection.json'
            batch.save(selection, self.selection(review=dict(prior_verdicts=None)))
            with patch.object(batch, 'ROOT', root), patch.object(batch, 'BATCHES', root/'batches'):
                with self.assertRaisesRegex(ValueError, 'prior_verdicts'):
                    batch.prepare('malformed', selection)
            self.assertFalse((root/'batches/malformed').exists())

    def test_valid_selection(self):
        targets = batch.validate_selection(self.selection())
        self.assertEqual(len(targets), 1)
        self.assertNotIn('lane', targets[0])
        self.assertEqual(targets[0]['review']['report_scope'], 'none')

    def test_sibling_report_does_not_exclude_an_otherwise_good_target(self):
        targets = batch.validate_selection(self.selection(
            review=dict(report_scope='sibling', report_evidence='Report names another function in this file'),
            file_problem_reports=1))
        self.assertEqual(targets[0]['review']['report_scope'], 'sibling')

    def test_selection_normalizes_nested_ownership(self):
        data = self.selection(regions=dict(co_workers=0))
        del data['targets'][0]['co_workers']
        self.assertEqual(batch.validate_selection(data)[0]['co_workers'], 0)

    def test_selection_missing_fields_error_names_each_field(self):
        data = self.selection()
        for field in ('size', 'reason', 'file_problem_reports'):
            del data['targets'][0][field]
        with self.assertRaisesRegex(ValueError,
                '0x00123456 missing required fields: file_problem_reports, reason, size'):
            batch.validate_selection(data)

    def test_selection_rejects_nested_holder(self):
        data = self.selection(regions=dict(co_workers=1))
        del data['targets'][0]['co_workers']
        with self.assertRaisesRegex(ValueError, 'unheld'):
            batch.validate_selection(data)

    def test_file_reports_need_a_reviewed_scope_not_a_silent_none(self):
        with self.assertRaisesRegex(ValueError, 'file reports'):
            batch.validate_selection(self.selection(file_problem_reports=1))
        targets = batch.validate_selection(self.selection(
            review=dict(report_scope='none', report_evidence='Reviewed; the report concerns a different helper'),
            file_problem_reports=1))
        self.assertEqual(targets[0]['review']['report_scope'], 'none')

    def test_open_function_report_cannot_be_declared_none_or_sibling(self):
        for scope in ('none', 'sibling'):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, 'open report'):
                batch.validate_selection(self.selection(
                    review=dict(report_scope=scope, report_evidence='checked'),
                    open_problem_reports=1))

    def test_function_report_requires_a_concrete_next_experiment(self):
        with self.assertRaisesRegex(ValueError, 'next_experiment'):
            batch.validate_selection(self.selection(
                review=dict(report_scope='function', report_evidence='Report on this exact address')))

    def test_repeated_attempts_require_a_concrete_next_experiment(self):
        with self.assertRaisesRegex(ValueError, 'next_experiment'):
            batch.validate_selection(self.selection(give_ups=4))

    def test_prior_unfit_requires_an_integration_plan_not_a_lane(self):
        with self.assertRaisesRegex(ValueError, 'integration plan'):
            batch.validate_selection(self.selection(review=dict(prior_unfit=True)))
        targets = batch.validate_selection(self.selection(
            review=dict(prior_unfit=True, integration_plan='Reconcile the named full-unit type clash',
                        next_experiment='Repair the named type clash before rechecking')))
        self.assertTrue(targets[0]['review']['prior_unfit'])

    def test_prior_verdict_unfit_requires_an_integration_plan(self):
        with self.assertRaisesRegex(ValueError, 'integration plan'):
            batch.validate_selection(self.selection(review=dict(prior_verdicts=['unfit'])))

    def test_unknown_verdict_history_is_recorded_not_fabricated(self):
        targets = batch.validate_selection(self.selection(review=dict(verdict_history='unknown')))
        self.assertEqual(targets[0]['review']['verdict_history'], 'unknown')

    def test_review_facts_are_type_checked_and_idempotent(self):
        with self.assertRaisesRegex(ValueError, 'next_experiment'):
            batch.validate_selection(self.selection(
                review=dict(prior_verdicts=['unfit'], prior_unfit=False,
                            integration_plan='Repair the full-unit type clash')))
        with self.assertRaisesRegex(ValueError, 'next_experiment'):
            batch.validate_selection(self.selection(review=dict(next_experiment=123)))
        with self.assertRaisesRegex(ValueError, 'next_experiment'):
            batch.validate_selection(self.selection(
                review=dict(prior_verdicts=['unfit '], prior_unfit=False,
                            integration_plan='repair the full-unit type clash')))
        facts = review.normalized(dict(
            review=dict(history_reviewed=True, history_sources=['fixture'], prior_verdicts=[],
                        verdict_history='reviewed', report_scope='sibling',
                        report_evidence='saved report on another function', prior_unfit=False,
                        next_experiment=None, integration_plan=None),
            give_ups=0, open_problem_reports=0))
        self.assertEqual(review.normalized(dict(review=facts, give_ups=0, open_problem_reports=0)), facts)

    def test_legacy_report_facts_do_not_invent_a_plan(self):
        item = dict(address='0x00123456', size=100, state='open', co_workers=0,
                    give_ups=4, open_problem_reports=0, file_problem_reports=0,
                    lane='integration', reason='Legacy', history_reviewed=True,
                    history_sources=['legacy'], prior_verdicts=[], verdict_history='reviewed',
                    prior_unfit=True)
        facts = review.for_report(item)
        self.assertTrue(facts['prior_unfit'])
        self.assertIsNone(facts['integration_plan'])
        self.assertEqual(facts['report_scope'], 'none')

    def test_legacy_lane_selection_is_translated_once(self):
        item = dict(address='0x00123456', size=100, state='open', co_workers=0,
                    give_ups=4, open_problem_reports=0, file_problem_reports=1,
                    lane='research', new_hypothesis='Named source shape from the saved report',
                    reason='Legacy prepared selection', history_reviewed=True,
                    history_sources=['legacy fixture'], prior_verdicts=[], verdict_history='reviewed')
        targets = batch.validate_selection(dict(checked_at=batch.now(), targets=[item]))
        self.assertNotIn('lane', targets[0])
        self.assertEqual(targets[0]['review']['next_experiment'], 'Named source shape from the saved report')
        self.assertEqual(targets[0]['review']['report_scope'], 'unknown')

    def test_review_and_lane_together_are_rejected(self):
        data = self.selection()
        data['targets'][0]['lane'] = 'production'
        with self.assertRaisesRegex(ValueError, 'lane'):
            batch.validate_selection(data)

    def test_prompt_reports_facts_without_a_lane(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(batch, 'BATCHES', Path(directory)/'batches'), \
                patch.object(batch, 'ROOT', Path(directory)):
            selection = Path(directory)/'selection.json'; batch.save(selection, self.selection())
            folder = Path(batch.prepare('test', selection)['batch'])
            prompt = (folder/'worker01/prompt.txt').read_text(encoding='utf-8')
            self.assertIn('prior verdicts: none', prompt)
            self.assertIn("report scope: none", prompt)
            self.assertNotIn('lane', prompt.lower())

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
            self.assertNotIn('lane', assignment['target'])
            self.assertIn('review', assignment['target'])
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
