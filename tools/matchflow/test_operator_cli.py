from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import lifecycle as life
import matchflow as ledger
import operator_cli as cli
import screen
from transport import ServiceError


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.patch = patch.object(cli, 'workspace_root', return_value=self.root)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.screen = self.root/'screen'
        self.screen.mkdir()
        self.info = dict(address='0x00123456', size=100, state='open', co_workers=0,
                         give_ups=3, open_problem_reports=0, file_problem_reports=0)
        self.stamp = life.stamp(life.utc()-timedelta(minutes=2))
        life.save(self.screen/'info-01.json', dict(checked_at=self.stamp,
                  request=dict(address=self.info['address']), response=self.info))
        life.save(self.screen/'review.json', dict(shortlist=[dict(metadata=self.info)]))

    def annotations(self):
        return dict(targets=[dict(address=self.info['address'], reason='Reviewed structural lead',
            review=dict(history_reviewed=True, history_sources=['Saved function_info and history'],
                        prior_verdicts=[], verdict_history='reviewed', report_scope='none',
                        report_evidence=None, prior_unfit=False,
                        next_experiment='Shorten receiver lifetime at first difference', integration_plan=None))])

    def test_status_only_reads_and_preserves_actual_response_and_notices(self):
        status = dict(open_claims=[], tier='fixture', notices=['status notice'])
        api = Mock(side_effect=[dict(notices=['start notice']), status])
        result = cli.status_snapshot('status.json', api)
        self.assertEqual([c.args[0] for c in api.call_args_list], ['start_here', 'status'])
        self.assertEqual(result['notices'], ['start notice', 'status notice'])
        self.assertEqual(life.load(result['path'])['status'], status)
        with self.assertRaises(ValueError):
            cli.status_snapshot('status.json', api)
        self.assertEqual(api.call_count, 2)

    def test_status_failure_preserves_start_notices_without_retry(self):
        api = Mock(side_effect=[dict(notices=['one shot']), ServiceError(503, '60')])
        with self.assertRaises(ServiceError):
            cli.status_snapshot('failed.json', api)
        self.assertFalse((self.root/'work/matchflow/failed.json').exists())
        self.assertEqual(life.load(self.root/'work/matchflow/failed.json.start.json')['response']['notices'], ['one shot'])
        self.assertEqual(api.call_count, 2)

    def test_template_requires_review_and_selection_keeps_original_timestamp(self):
        template = cli.annotation_template(self.screen)
        with self.assertRaises(ValueError):
            cli.reviewed_selection(self.screen, template)
        result = cli.reviewed_selection(self.screen, self.annotations())
        self.assertEqual(result['checked_at'], self.stamp)
        self.assertEqual(result['targets'][0]['size'], self.info['size'])
        self.assertEqual(len(result['evidence'][0]['sha256']), 64)

    def test_offline_screen_review_selection_and_prepare_need_no_external_scripts(self):
        folder = self.root/'fresh-screen'
        def api(tool, body):
            if tool == 'start_here':
                return dict(notices=['fixture notice'])
            if tool == 'status':
                return dict(open_claims=[])
            if tool == 'list_bands':
                return dict(bands=[dict(band='<=255', allowed=True, open=1)])
            if tool == 'list_functions':
                return dict(functions=[dict(self.info, band='<=255', failed_verifications=0)], next=None)
            if tool == 'function_info':
                return dict(self.info, failed_verifications=0)
            raise AssertionError('unexpected mutation '+tool)
        screen.screen(folder, count=1, api=api)
        annotations = cli.annotation_template(folder)
        annotations['targets'][0] = self.annotations()['targets'][0]
        result = cli.reviewed_selection(folder, annotations)
        selection = cli.write_new('selection.json', result)
        with patch.object(cli.batch, 'ROOT', self.root), patch.object(cli.batch, 'BATCHES', self.root/'work/batches'):
            prepared = cli.batch.prepare('fixture', selection['path'])
        assignment = life.load(Path(prepared['batch'])/'worker01/assignment.json')
        self.assertEqual(assignment['target']['review']['history_reviewed'], True)
        self.assertEqual(assignment['selection_checked_at'], result['checked_at'])
        self.assertEqual(life.load(Path(prepared['batch'])/'batch.json')['selection']['evidence'], result['evidence'])

    def test_stale_info_cannot_be_refreshed_by_review_timestamp(self):
        value = life.load(self.screen/'info-01.json')
        value['checked_at'] = life.stamp(life.utc()-timedelta(minutes=16))
        life.save(self.screen/'info-01.json', value)
        with self.assertRaisesRegex(ValueError, 'Refresh service metadata'):
            cli.reviewed_selection(self.screen, self.annotations())

    def test_nested_ownership_screen_to_prepared_assignment_preserves_raw_evidence(self):
        folder = self.root/'nested-screen'
        info = dict(self.info, band='<=255', failed_verifications=0, regions=dict(co_workers=0))
        del info['co_workers']
        def api(tool, body):
            if tool == 'list_bands':
                return dict(bands=[dict(band='<=255', allowed=True, open=1)])
            if tool == 'list_functions':
                return dict(functions=[info], next=None)
            if tool == 'function_info':
                return info
            if tool in ('start_here', 'status'):
                return dict(open_claims=[])
            raise AssertionError('unexpected mutation '+tool)
        self.assertEqual(screen.screen(folder, count=1, api=api)['count'], 1)
        receipt_path = folder/'info-01.json'
        original = receipt_path.read_bytes()
        self.assertEqual(life.load(receipt_path)['response'], info)
        self.assertEqual(life.load(folder/'review.json')['shortlist'][0]['metadata'], info)
        result = cli.reviewed_selection(folder, self.annotations())
        self.assertEqual(result['checked_at'], life.load(receipt_path)['checked_at'])
        self.assertEqual(result['evidence'][0]['sha256'], ledger.digest(receipt_path))
        selection = cli.write_new('nested-selection.json', result)
        with patch.object(cli.batch, 'ROOT', self.root), patch.object(cli.batch, 'BATCHES', self.root/'work/batches'):
            prepared = cli.batch.prepare('nested-fixture', selection['path'])
        assignment = life.load(Path(prepared['batch'])/'worker01/assignment.json')
        self.assertEqual(assignment['target']['co_workers'], 0)
        self.assertEqual(receipt_path.read_bytes(), original)
        self.assertNotIn('co_workers', info)

    def test_selection_rejects_duplicates_missing_evidence_metadata_and_review_overrides(self):
        annotations = self.annotations()
        annotations['targets'] *= 2
        with self.assertRaises(ValueError):
            cli.reviewed_selection(self.screen, annotations)
        for change in (dict(address='0x00999999'), dict(size=99), dict(review={})):
            annotations = self.annotations()
            annotations['targets'][0].update(change)
            with self.assertRaises(ValueError):
                cli.reviewed_selection(self.screen, annotations)
        info = life.load(self.screen/'info-01.json')
        info['response']['size'] = 101
        life.save(self.screen/'info-01.json', info)
        with self.assertRaisesRegex(ValueError, 'unchanged metadata'):
            cli.reviewed_selection(self.screen, self.annotations())

    def test_selection_uses_oldest_of_multiple_info_receipts(self):
        second = dict(self.info, address='0x00123457')
        later = life.stamp()
        life.save(self.screen/'info-02.json', dict(checked_at=later,
                  request=dict(address=second['address']), response=second))
        life.save(self.screen/'review.json', dict(shortlist=[dict(metadata=i) for i in (self.info, second)]))
        annotations = self.annotations()
        annotations['targets'].append(dict(annotations['targets'][0], address=second['address']))
        self.assertEqual(cli.reviewed_selection(self.screen, annotations)['checked_at'], self.stamp)

    def test_runtime_output_is_external_workspace_only_and_never_overwrites(self):
        result = cli.write_new('selection.json', {'fixture': True})
        with self.assertRaises(FileExistsError):
            cli.write_new('selection.json', {})
        with self.assertRaises(ValueError):
            cli.write_new(self.root.parent/'not-in-workspace.json', {})
        with self.assertRaises(ValueError):
            cli.write_new(Path(cli.__file__).parent/'runtime.json', {})
        self.assertTrue(life.load(result['path'])['fixture'])

    def test_inspection_never_creates_or_migrates_database(self):
        missing = self.root/'missing.sqlite3'
        with self.assertRaises(ValueError):
            cli.inspect_ledger(missing, 'claims')
        self.assertFalse(missing.exists())
        dbpath = self.root/'ledger.sqlite3'
        with closing(ledger.connect(dbpath)) as db:
            db.execute('INSERT INTO server_results VALUES(?,?,?,?,?,?)',
                       ('sub_fixture', self.info['address'], 'verified', 'fixture', life.stamp(), 'hash'))
            db.commit()
        before = dbpath.read_bytes()
        result = cli.inspect_ledger(dbpath, 'verdicts', address=self.info['address'], limit=1)
        self.assertEqual(result['rows'][0]['verdict'], 'verified')
        self.assertEqual(cli.inspect_ledger(dbpath, 'verdicts', offset=1)['rows'], [])
        self.assertEqual(before, dbpath.read_bytes())
        with closing(cli.read_database(dbpath)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE forbidden(x)')
        for kw in (dict(limit=101), dict(offset=-1), dict(address="' OR 1=1"), dict(claim='x')):
            with self.assertRaises(ValueError):
                cli.inspect_ledger(dbpath, 'verdicts', **kw)

    def test_checks_and_claims_are_filtered_and_do_not_expose_raw_payloads(self):
        path = self.root/'fixture.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE attempts(claim_id,address,state,model,variant,created,deadline,final_check_id,submission_id)')
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)',
                       ('clm_fixture', self.info['address'], 'given_up', 'fixture/model', 'max', 'now', 'later', None, None))
            db.execute('CREATE TABLE checks(id,claim_id,stage,created,valid,exit_code,result_json)')
            db.execute('INSERT INTO checks VALUES(1,?,?,?,?,?,?)', ('clm_fixture', 'baseline', 'now', 1, 1,
                       json.dumps(dict(status='wip', ndiff_insn=2, disassembly='PRIVATE'))))
        self.assertEqual(len(cli.inspect_ledger(path, 'claims', claim='clm_fixture')['rows']), 1)
        result = cli.inspect_ledger(path, 'checks', address=self.info['address'])
        self.assertEqual(result['rows'][0]['metrics']['ndiff_insn'], 2)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(cli.inspect_ledger(path, 'checks', claim='other')['rows'], [])

    def test_lifecycle_is_bounded_filtered_and_read_only(self):
        with closing(sqlite3.connect(self.root/'lifecycle.sqlite3')) as db, db:
            db.execute('CREATE TABLE workers(id,payload)')
            for i in range(2):
                db.execute('INSERT INTO workers VALUES(?,?)', (f'worker{i+1:02}', json.dumps(
                    dict(state='closed', claim_id=f'clm_{i}', private_field='PRIVATE'))))
        before = (self.root/'lifecycle.sqlite3').read_bytes()
        result = cli.inspect_lifecycle(self.root, 'worker02', limit=1)
        self.assertEqual(result['rows'][0]['claim_id'], 'clm_1')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(before, (self.root/'lifecycle.sqlite3').read_bytes())


if __name__ == '__main__':
    unittest.main()
