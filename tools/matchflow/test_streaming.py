"""Offline streaming discovery and handoff contracts. No real service or children."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import batch
import broker
import lifecycle as life
import matchflow as ledger
import preflight
import streaming


class FakeService:
    """The claim mutation can succeed remotely even when its response is lost."""

    def __init__(self):
        self.calls = []
        self.claims = []
        self.pages = 0
        self.lose_claim_response = False
        self.info_overrides = {}
        self.last_page = False

    def __call__(self, tool, body):
        self.calls.append((tool, deepcopy(body)))
        if tool == 'start_here':
            return {'next_step': 'status'}
        if tool == 'list_bands':
            return {'bands': [{'band': 'small', 'allowed': True, 'open': 100}]}
        if tool == 'status':
            return dict(limits=dict(claims=50, subsPerDay=1000, pending=1000),
                        open_claims=deepcopy(self.claims), submissions_today=0,
                        awaiting_verification=0)
        if tool == 'list_functions':
            self.pages += 1
            return dict(functions=[self.info('0x00123456')], next=None if self.last_page else 'page%d' % self.pages)
        if tool == 'function_info':
            return deepcopy(self.info_overrides.get(body['address'], self.info(body['address'])))
        if tool == 'claim':
            cid = 'clm_offline%d' % (len(self.claims) + 1)
            self.claims.append(dict(claim_id=cid, address=body['address'], this_key=True,
                                    expires=life.stamp(life.utc()+timedelta(hours=2))))
            if self.lose_claim_response:
                raise ConnectionError('offline simulated response loss')
            return dict(claim_id=cid, address=body['address'])
        raise AssertionError('unexpected service operation: '+tool)

    @staticmethod
    def info(address):
        return dict(address=address, size=160, state='open', co_workers=0,
                    give_ups=0, open_problem_reports=0, file_problem_reports=0,
                    symbol='OfflineFixture', source_file='offline.cpp')

    def count(self, tool):
        return sum(name == tool for name, _ in self.calls)


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module, name, value in (
                (batch, 'ROOT', self.root),
                (batch, 'BATCHES', self.root/'work/batches'),
                (broker, 'workspace_root', lambda: self.root),
                (streaming, 'workspace_root', lambda: self.root),
                (ledger, 'DEFAULT_DB', self.root/'work/matchflow/ledger.sqlite3')):
            self.stack.enter_context(patch.object(module, name, value))
        self.require = self.stack.enter_context(patch.object(preflight, 'require'))
        self.fingerprint = self.stack.enter_context(
            patch.object(preflight, 'fingerprint', return_value={'model': 'offline/model#high'}))
        self.popen = self.stack.enter_context(
            patch('streaming.subprocess.Popen', return_value=Mock(pid=4242)))
        self.api = FakeService()
        self.parent = 'ses_parent'
        self.producer = 'ses_discovery'
        self.name = 'offline-stream'
        self.folder = batch.batch_path(self.name)

    def open(self, requested=3, backlog=2):
        result = streaming.open_run(self.name, requested, self.parent,
                                    model='offline/model#high', backlog=backlog)
        streaming.bind_discovery(self.name, self.producer, self.parent)
        return result

    def discover(self, op, data=None):
        return streaming.discover(self.producer, op, data or {}, api=self.api)

    def review(self, address='0x00123456'):
        return dict(address=address, reason='Reviewed fixture history and concrete source lead',
                    review=dict(history_reviewed=True, history_sources=['offline function_info'],
                                prior_verdicts=[], verdict_history='reviewed', report_scope='none',
                                report_evidence=None, prior_unfit=False,
                                next_experiment='Test named receiver lifetime', integration_plan=None))

    def enqueue(self, address='0x00123456'):
        self.discover('inspect', dict(address=address))
        return self.discover('enqueue', self.review(address))

    def update_control(self, **updates):
        with life.transaction(self.folder) as db:
            value = streaming.control(db)
            value.update(updates)
            streaming.put_control(db, value)

    def test_open_initializes_empty_supervised_run_and_freezes_model(self):
        result = self.open()
        self.require.assert_called_once_with()
        self.assertEqual(result['model'], 'offline/model#high')
        self.assertEqual(life.manifest(self.folder)['tasks'], [])
        self.assertEqual(streaming.status(self.folder)['producer_state'], 'running')
        with life.transaction(self.folder) as db:
            value = streaming.control(db)
            self.assertIsNotNone(value['heartbeat'])
            token = value['watcher']
        args = self.popen.call_args.args[0]
        self.assertEqual(args[2:4], ['watch', str(self.folder)])
        self.assertEqual(args[-2:], ['--token', token])
        self.assertEqual(life.load(self.folder/'launch.json')['watchdog_pid'], 4242)
        self.assertEqual(self.api.calls, [])
        # Zero workers must not make the watchdog exit while discovery is open.
        with patch.object(life.time, 'sleep', side_effect=RuntimeError('sleep reached')) as sleep:
            with self.assertRaisesRegex(RuntimeError, 'sleep reached'):
                life.watch(self.folder, token)
            sleep.assert_called_once_with(5)
        self.api.last_page = True
        self.discover('catalog')
        self.discover('finish', dict(reason='No suitable candidates remain in the pool'))
        with patch.object(life.time, 'sleep') as sleep:
            life.watch(self.folder, token)
            sleep.assert_not_called()

    def test_open_rejects_invalid_bounds_before_starting_watchdog(self):
        for requested, backlog in ((0, 2), (51, 2), (True, 2), (1, 0), (1, 5), (1, True)):
            with self.subTest(requested=requested, backlog=backlog), self.assertRaises(ValueError):
                streaming.open_run(self.name, requested, self.parent, 'offline/model', backlog)
        self.popen.assert_not_called()
        self.assertFalse(self.folder.exists())

    def test_catalog_required_and_continues_beyond_four_pages(self):
        self.open()
        with self.assertRaisesRegex(ValueError, 'start catalog first'):
            self.discover('inspect', dict(address='0x00123456'))
        self.assertEqual(self.api.calls, [])
        for _ in range(7):
            self.discover('catalog')
        self.assertEqual([name for name, _ in self.api.calls[:4]],
                         ['start_here', 'status', 'list_bands', 'list_functions'])
        for tool in ('start_here', 'status', 'list_bands'):
            self.assertEqual(self.api.count(tool), 1)
        bodies = [body for tool, body in self.api.calls if tool == 'list_functions']
        self.assertEqual(bodies[0], dict(order='give_ups', limit=500, include_held=False))
        self.assertEqual([body.get('cursor') for body in bodies],
                         [None]+['page%d' % i for i in range(1, 7)])
        self.assertEqual(self.api.count('list_functions'), 7)

    def test_catalog_stops_when_service_has_no_next_cursor(self):
        self.open()
        api = self.api

        def last_page(tool, body):
            result = api(tool, body)
            if tool == 'list_functions':
                result['next'] = None
            return result

        streaming.discover(self.producer, 'catalog', {}, api=last_page)
        with self.assertRaisesRegex(ValueError, 'catalog exhausted'):
            self.discover('catalog')
        self.assertEqual(self.api.count('list_functions'), 1)

    def test_inspections_continue_beyond_four_times_quota_and_require_canonical_addresses(self):
        self.open(requested=1)
        self.discover('catalog')
        for address in ('0X00123456', '0x123456', '../outside'):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, 'canonical'):
                self.discover('inspect', dict(address=address))
        for i in range(13):
            self.discover('inspect', dict(address='0x%08x' % (0x00123456+i)))
        self.assertEqual(self.api.count('function_info'), 13)
        self.assertEqual(self.api.count('claim'), 0)

    def test_inspect_review_enqueue_first_claim_reaches_done(self):
        self.open(requested=1)
        self.discover('catalog')
        with self.assertRaisesRegex(ValueError, 'inspect candidate'):
            self.discover('enqueue', self.review())
        receipt = self.discover('inspect', dict(address='0x00123456'))
        self.assertEqual(receipt['response']['address'], '0x00123456')
        invalid = self.review()
        invalid['review']['history_reviewed'] = False
        with self.assertRaisesRegex(ValueError, 'review the target history'):
            self.discover('enqueue', invalid)
        self.assertEqual(self.api.count('claim'), 0)
        result = self.discover('enqueue', self.review())
        self.assertEqual(result['claim_id'], 'clm_offline1')
        self.assertEqual(self.api.count('claim'), 1)
        body = next(body for tool, body in self.api.calls if tool == 'claim')
        self.assertEqual(body, dict(address='0x00123456', cowork=False, context_mode='resume',
                                    model='offline/model', harness='OpenCode', effort='high'))
        status = self.discover('status')
        self.assertEqual((status['claimed'], status['producer_state']), (1, 'done'))
        self.assertEqual(status['tasks'][0]['state'], 'queued')
        state = life.snapshot(self.folder, 'worker01')
        self.assertEqual(state['state'], 'claimed')
        self.assertEqual(state['deadline_utc'], self.api.claims[0]['expires'])
        self.assertIn('claim_ack_utc', state)
        with self.assertRaisesRegex(ValueError, 'producer finished'):
            self.discover('enqueue', self.review('0x00123457'))

    def test_quota_reached_after_searching_beyond_old_limits(self):
        self.open(requested=1)
        for i in range(6):
            self.discover('catalog')
            for j in range(3):
                address = '0x%08x' % (0x00123456+3*i+j)
                self.discover('inspect', dict(address=address))
        result = self.discover('enqueue', self.review(address))
        self.assertEqual(result['address'], address)
        self.assertEqual(self.api.count('claim'), 1)
        self.assertEqual(self.discover('status')['producer_state'], 'done')

    def test_finish_requires_catalog_exhaustion_and_explicit_reason(self):
        self.open()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'catalog not exhausted'):
                self.discover('finish', dict(reason='shortlist rejected'))
            self.discover('catalog')
        self.api.last_page = True
        self.discover('catalog')
        with self.assertRaisesRegex(ValueError, 'explicit pool exhaustion reason'):
            self.discover('finish')
        self.assertEqual(self.discover('status')['producer_state'], 'running')
        self.discover('finish', dict(reason='All catalog candidates reviewed or excluded'))
        self.assertEqual(self.discover('status')['producer_state'], 'done')
        self.assertEqual(self.api.count('claim'), 0)

    def test_repeated_cursor_does_not_repeat_service_page(self):
        self.open()
        def api(tool, body):
            result = self.api(tool, body)
            if tool == 'list_functions':
                result['next'] = 'cycle'
            return result
        for _ in range(2):
            streaming.discover(self.producer, 'catalog', {}, api=api)
        with self.assertRaisesRegex(ValueError, 'cursor repeated'):
            streaming.discover(self.producer, 'catalog', {}, api=api)
        self.assertEqual(self.api.count('list_functions'), 2)

    def test_supervised_discovery_has_no_total_time_cutoff(self):
        self.open()
        with life.transaction(self.folder) as db:
            value = streaming.control(db)
            value['stream']['producer_started'] = life.time.monotonic()-86400
            streaming.put_control(db, value)
            self.assertIsNone(streaming.supervise(db, self.folder))
        self.assertEqual(self.discover('status')['producer_state'], 'running')
        self.assertFalse((self.folder/'STOP').exists())

    def test_producer_clock_discontinuity_still_stops_discovery(self):
        self.open()
        with life.transaction(self.folder) as db:
            value = streaming.control(db)
            value['stream']['producer_started'] = life.time.monotonic()+100
            streaming.put_control(db, value)
            self.assertEqual(streaming.supervise(db, self.folder), 'producer_clock_discontinuity')
        self.assertTrue((self.folder/'STOP').exists())

    def test_duplicate_reservation_is_not_replayed(self):
        self.open()
        self.discover('catalog')
        self.enqueue()
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            self.discover('enqueue', self.review())
        self.assertEqual(self.api.count('claim'), 1)
        self.assertEqual(len(life.manifest(self.folder)['tasks']), 1)

    def test_enqueue_accepts_both_ownership_locations_without_rewriting_receipts(self):
        self.open(requested=3, backlog=4)
        self.discover('catalog')
        for i, shape in enumerate(('flat', 'nested', 'both')):
            address = '0x%08x' % (0x00123456+i)
            info = self.api.info(address)
            if shape != 'flat':
                info['regions'] = dict(count=9, co_workers=0)
            if shape == 'nested':
                del info['co_workers']
            self.api.info_overrides[address] = info
            receipt = self.discover('inspect', dict(address=address))
            result = self.discover('enqueue', self.review(address))
            assignment = life.load(self.folder/result['worker']/'assignment.json')
            self.assertEqual(assignment['target']['co_workers'], 0)
            with life.transaction(self.folder) as db:
                saved = db.execute('SELECT payload FROM stream_info WHERE address=?', (address,)).fetchone()[0]
            self.assertEqual(json.loads(saved), receipt)
            self.assertEqual(receipt['response'], info)
        self.assertEqual(self.api.count('claim'), 3)
        self.assertEqual(self.discover('status')['producer_state'], 'done')

    def test_unsafe_ownership_fails_before_reservation_or_claim(self):
        self.open()
        self.discover('catalog')
        cases = [({}, 'missing co_workers'),
                 (dict(co_workers=0, regions=dict(co_workers=1)), 'conflicting'),
                 (dict(regions=dict(co_workers=False)), 'nonnegative integer'),
                 (dict(regions=dict(co_workers=1)), 'unheld')]
        for fields, error in cases:
            info = self.api.info('0x00123456')
            del info['co_workers']
            info.update(fields)
            self.api.info_overrides['0x00123456'] = info
            self.discover('inspect', dict(address='0x00123456'))
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, error):
                self.discover('enqueue', self.review())
            self.assertEqual(self.api.count('claim'), 0)
            self.assertEqual(life.manifest(self.folder)['tasks'], [])

    def test_lost_claim_response_leaves_cleanup_barrier_and_never_retries(self):
        self.open()
        self.discover('catalog')
        self.api.lose_claim_response = True
        with self.assertRaisesRegex(ConnectionError, 'response loss'):
            self.enqueue()
        state = life.snapshot(self.folder, 'worker01')
        self.assertTrue(state['claim_attempted'])
        self.assertEqual(state['state'], 'claim_pending')
        self.assertIsNone(state['claim_id'])
        self.assertTrue(state['cleanup_required'])
        self.assertTrue((self.folder/'STOP').exists())
        with life.transaction(self.folder) as db:
            self.assertIn('cleanup_barrier', streaming.control(db))
        # Removing the wake-up file does not remove the durable barrier.
        (self.folder/'STOP').unlink()
        for op, data in (('enqueue', self.review()), ('catalog', {})):
            with self.assertRaisesRegex(ValueError, 'run stopped'):
                self.discover(op, data)
        with self.assertRaises(ValueError):
            streaming.next_task(self.name, self.parent)
        self.assertEqual(self.api.count('claim'), 1)
        self.assertEqual(len(self.api.claims), 1)
        self.assertEqual(self.discover('status')['claimed'], 0)

    def test_preclaimed_worker_setup_never_claims_again_and_cannot_be_replayed(self):
        self.open()
        self.discover('catalog')
        self.enqueue()
        streaming.next_task(self.name,self.parent)
        broker.bind(self.name,'worker01','ses_worker1',self.parent)
        def api(tool,body):
            if tool == 'packet':
                return dict(pack_url='https://f3decomp.com/offline-fixture')
            return self.api(tool,body)
        def acquire(workspace,cid,packet):
            root=workspace/'packs'/cid
            root.mkdir(parents=True)
            life.save(root/'pack.json',dict(address='0x00123456',flags=['/O2']))
            return root
        self.assertTrue(broker.start('ses_worker1',api=api,acquire=acquire)['ready'])
        streaming.assigned(self.folder,'worker01')
        with self.assertRaisesRegex(ValueError,'never replay'):
            broker.start('ses_worker1',api=api,acquire=acquire)
        self.assertEqual(self.api.count('claim'),1)
        state=life.snapshot(self.folder,'worker01')
        self.assertGreaterEqual(state['review_to_claim_seconds'],0)
        self.assertGreaterEqual(state['claim_to_worker_seconds'],0)

    def test_controller_loss_stops_even_an_empty_run_with_live_watchdog(self):
        self.open()
        with life.transaction(self.folder) as db:
            value=streaming.control(db)
            value['stream']['producer_heartbeat']=life.time.monotonic()-31
            streaming.put_control(db,value)
        life.tick(self.folder,value['watcher'])
        self.assertEqual(self.discover('status')['producer_state'],'failed')
        self.assertTrue((self.folder/'STOP').exists())
        self.assertEqual(self.api.count('claim'),0)

    def test_abandoned_launch_reservation_stops_without_waiting_worker_deadline(self):
        self.open()
        self.discover('catalog')
        self.enqueue()
        streaming.next_task(self.name,self.parent)
        with life.transaction(self.folder) as db:
            value=streaming.control(db)
            state=life.read_worker(db,'worker01')
            state['launch_reserved_monotonic']=life.time.monotonic()-121
            life.write_worker(db,'worker01',state)
        life.tick(self.folder,value['watcher'])
        self.assertTrue((self.folder/'STOP').exists())
        self.assertEqual(self.discover('status')['failure_reason'],'abandoned_launch_reservation')

    def test_partial_binding_is_explicitly_reconciled_and_closed_without_session_execution(self):
        import recovery
        self.open()
        self.discover('catalog')
        self.enqueue()
        streaming.next_task(self.name,self.parent)
        with patch.object(life,'bind',side_effect=OSError('crash after registry commit')):
            with self.assertRaises(OSError):
                broker.bind(self.name,'worker01','ses_partial',self.parent)
        self.assertIsNone(life.snapshot(self.folder,'worker01')['session_id'])
        self.assertEqual(recovery.inventory(self.folder,self.api('status',{}))['workers'][0]['partial_binding_session_id'],'ses_partial')
        with self.assertRaisesRegex(ValueError,'stopped'):
            broker.reconcile_unprompted_binding(self.folder,'worker01','ses_partial')
        streaming.stop(self.folder,'partial binding')
        action=dict(worker='worker01',address='0x00123456',claim_id='clm_offline1',
            session_id='ses_partial',binding_session_id='ses_partial',unprompted_binding=True,
            operation='giveup',no_source_reason='Host binding interrupted before setup or prompt.',
            notes=dict(reason='Partial binding',category='other',tried='No experiments or prompt',
                       still_differs='No source',suspected_cause='Host interrupted between registry and lifecycle commits'),
            closing_note='Recovered exact registry session after interrupted binding; never prompted, no source exists.')
        def api(tool,body):
            if tool=='giveup':
                self.api.claims.clear();return dict(released=True)
            if tool=='checkpoint':
                return dict(checkpoint_id='ckp_partial')
            return self.api(tool,body)
        result=recovery.apply(self.folder,dict(batch=self.name,workers=[action]),db_path=ledger.DEFAULT_DB,api=api)
        self.assertTrue(result['ok'])
        self.assertEqual(life.snapshot(self.folder,'worker01')['session_id'],'ses_partial')

    def test_concurrent_pending_reservation_counts_toward_requested_quota(self):
        self.open(requested=1,backlog=2)
        self.discover('catalog')
        for address in ('0x00123456','0x00123457'):
            self.discover('inspect',dict(address=address))
        def api(tool,body):
            if tool == 'claim':
                # Simulates the second request arriving while the first network
                # mutation is in flight, before its acknowledged count increments.
                with self.assertRaisesRegex(ValueError,'requested claims'):
                    streaming.enqueue(self.folder,self.review('0x00123457'),self.api)
            return self.api(tool,body)
        streaming.enqueue(self.folder,self.review(),api)
        self.assertEqual(self.api.count('claim'),1)

    def test_cooldown_is_durable_and_preserves_retry_after_without_retry(self):
        import service
        self.open()
        def api(tool,body):
            raise service.ServiceError(429,retry_after='120')
        with self.assertRaises(service.ServiceError):
            streaming.discover(self.producer,'catalog',{},api=api)
        self.assertTrue((self.folder/'STOP').exists())
        receipt=life.load(self.folder/'SERVICE_ERROR.json')
        self.assertEqual(receipt['retry_after'],'120')
        self.assertEqual(self.discover('status')['producer_state'],'failed')
        with self.assertRaises(ValueError):
            self.discover('catalog')

    def test_explicit_unassigned_recovery_closes_retained_claim_without_fake_session(self):
        import recovery
        self.open()
        self.discover('catalog')
        self.enqueue()
        streaming.stop(self.folder,'offline child startup failure')
        action = dict(worker='worker01', address='0x00123456', claim_id='clm_offline1',
            session_id=None, unassigned=True, operation='giveup',
            no_source_reason='No child was bound or prompted; startup failed before assignment.',
            notes=dict(reason='Startup failed',category='other',tried='No experiments; child never bound',
                       still_differs='No candidate exists',suspected_cause='Local startup failure'),
            closing_note='Claim secured by discovery, but no worker capability was created. Closed without source.')
        def api(tool,body):
            if tool == 'giveup':
                self.api.claims.clear()
                return dict(released=True)
            if tool == 'checkpoint':
                return dict(checkpoint_id='ckp_offline')
            return self.api(tool,body)
        result = recovery.apply(self.folder,dict(batch=self.name,workers=[action]),
                                db_path=ledger.DEFAULT_DB, api=api)
        self.assertTrue(result['ok'])
        self.assertIsNone(life.snapshot(self.folder,'worker01')['session_id'])
        self.assertEqual(result['workers'][0]['evidence_integrity'],'valid')

    def test_queue_is_fifo_and_handoffs_are_reserved_exactly_once(self):
        self.open()
        self.discover('catalog')
        self.enqueue('0x00123457')
        self.enqueue('0x00123456')
        with self.assertRaisesRegex(ValueError, 'wrong stream owner'):
            streaming.next_task(self.name, 'ses_other')
        first = streaming.next_task(self.name, self.parent)
        second = streaming.next_task(self.name, self.parent)
        self.assertEqual([first['address'], second['address']], ['0x00123457', '0x00123456'])
        self.assertTrue(first['preclaimed'])
        self.assertEqual(first['worker'], 'worker01')
        self.assertTrue(streaming.next_task(self.name, self.parent)['empty'])
        with self.assertRaisesRegex(ValueError, 'no ready bound session'):
            streaming.assigned(self.folder, 'worker01')
        broker.bind(self.name, 'worker01', 'ses_worker1', self.parent)
        life.setup_step(self.folder, 'worker01', 'ready')
        streaming.assigned(self.folder, 'worker01')
        with self.assertRaisesRegex(ValueError, 'no replay'):
            streaming.assigned(self.folder, 'worker01')
        self.assertEqual(self.api.count('claim'), 2)

    def test_backlog_includes_launching_and_only_assignment_releases_capacity(self):
        self.open(backlog=1)
        self.discover('catalog')
        self.enqueue()
        self.discover('inspect', dict(address='0x00123457'))
        for reserve in (False, True):
            if reserve:
                streaming.next_task(self.name, self.parent)
            result = self.discover('enqueue', self.review('0x00123457'))
            self.assertTrue(result['backpressure'])
            self.assertEqual(self.api.count('claim'), 1)
        broker.bind(self.name, 'worker01', 'ses_worker1', self.parent)
        life.setup_step(self.folder, 'worker01', 'ready')
        streaming.assigned(self.folder, 'worker01')
        self.discover('enqueue', self.review('0x00123457'))
        self.assertEqual(self.api.count('claim'), 2)

    def test_finish_records_exhaustion_and_queued_work_is_still_consumable(self):
        self.open(requested=3)
        self.api.last_page = True
        self.discover('catalog')
        self.enqueue()
        self.assertEqual(self.discover('finish', dict(reason='reviewed shortlist exhausted')),
                         dict(producer_state='done'))
        self.discover('finish', dict(reason='must not overwrite original reason'))
        self.assertEqual(self.discover('status')['finish_reason'], 'reviewed shortlist exhausted')
        for op in ('catalog', 'inspect', 'enqueue'):
            with self.subTest(op=op), self.assertRaisesRegex(ValueError, 'producer finished'):
                self.discover(op, self.review())
        self.assertEqual(streaming.next_task(self.name, self.parent)['worker'], 'worker01')
        self.assertEqual(streaming.next_task(self.name, self.parent), dict(empty=True, producer_state='done'))
        self.assertEqual(self.api.count('claim'), 1)

    def test_stop_and_stale_heartbeat_block_discovery_handoff_and_worker_checks(self):
        self.open()
        self.discover('catalog')
        self.enqueue()
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                if stopped:
                    self.update_control(heartbeat=life.time.monotonic())
                    (self.folder/'STOP').touch()
                else:
                    self.update_control(heartbeat=life.time.monotonic()-31)
                before = len(self.api.calls)
                for op in ('catalog', 'inspect', 'enqueue'):
                    with self.assertRaises(ValueError):
                        self.discover(op, self.review('0x00123457'))
                with self.assertRaises(ValueError):
                    streaming.next_task(self.name, self.parent)
                with self.assertRaises(ValueError):
                    life.require_experiment(self.folder, 'worker01')
                self.assertEqual(len(self.api.calls), before)
                self.assertEqual(len(self.discover('status')['tasks']), 1)

    def test_model_fingerprint_change_blocks_without_refresh_or_claim(self):
        self.open()
        self.discover('catalog')
        self.enqueue()
        self.fingerprint.return_value = {'model': 'offline/different#low'}
        before = len(self.api.calls)
        with self.assertRaisesRegex(ValueError, 'configuration/tooling changed'):
            self.discover('catalog')
        with self.assertRaisesRegex(ValueError, 'configuration/tooling changed'):
            streaming.next_task(self.name, self.parent)
        self.require.assert_called_once_with()
        self.assertEqual(len(self.api.calls), before)

    def test_adding_tasks_preserves_bound_sibling_and_immutable_manifest(self):
        self.open()
        self.discover('catalog')
        self.enqueue()
        streaming.next_task(self.name, self.parent)
        broker.bind(self.name, 'worker01', 'ses_worker1', self.parent)
        life.setup_step(self.folder, 'worker01', 'ready')
        streaming.assigned(self.folder, 'worker01')
        before = broker.binding('ses_worker1')
        manifest_bytes = (self.folder/'batch.json').read_bytes()
        self.enqueue('0x00123457')
        after = broker.binding('ses_worker1')
        self.assertEqual(before, after)
        self.assertEqual((self.folder/'batch.json').read_bytes(), manifest_bytes)
        self.assertEqual(len(life.manifest(self.folder)['tasks']), 2)
        self.assertEqual(self.api.count('claim'), 2)

    def test_failed_discovery_cancels_request_without_fallback_runs(self):
        self.open()
        self.discover('catalog')
        # The producer exits without finish, as in the unreadable-catalog incident.
        self.assertEqual(broker.stream_call(dict(op='producer_end', batch=self.name, parent=self.parent)),
                         dict(cleanup_required=True))
        self.assertEqual(streaming.cancelled_runs(self.parent), [self.name])
        with self.assertRaisesRegex(ValueError, 'request cancelled'):
            streaming.open_run('offline-fallback', 1, self.parent, model='offline/model#high')
        self.assertFalse(batch.batch_path('offline-fallback').exists())
        with self.assertRaisesRegex(ValueError, 'request cancelled'):
            broker.task('offline-prepared', 'worker01', self.parent)
        self.require.assert_called_once_with()
        self.assertEqual(self.api.count('claim'), 0)
        # Another human request in a new session is not affected.
        self.assertEqual(streaming.cancelled_runs('ses_otherparent'), [])
        streaming.open_run('offline-new-session', 1, 'ses_otherparent', model='offline/model#high')

    def test_host_fail_and_watchdog_failures_also_cancel(self):
        self.open()
        broker.stream_call(dict(op='fail', batch=self.name, parent=self.parent))
        self.assertEqual(streaming.cancelled_runs(self.parent), [self.name])
        self.parent = 'ses_parenttwo'
        self.producer = 'ses_discoverytwo'
        self.name = 'offline-stream-two'
        self.folder = batch.batch_path(self.name)
        self.open()
        self.update_control(heartbeat=life.time.monotonic())
        with life.transaction(self.folder) as db:
            value = streaming.control(db)
            value['stream']['producer_heartbeat'] = life.time.monotonic()-31
            streaming.put_control(db, value)
            self.assertEqual(streaming.supervise(db, self.folder), 'producer_controller_lost')
        self.assertEqual(streaming.cancelled_runs(self.parent), [self.name])

    def test_finished_run_does_not_cancel_later_requests(self):
        self.open(requested=1)
        self.api.last_page = True
        self.discover('catalog')
        self.discover('finish', dict(reason='reviewed shortlist exhausted'))
        self.assertEqual(broker.stream_call(dict(op='producer_end', batch=self.name, parent=self.parent)),
                         dict(cleanup_required=False))
        self.assertEqual(streaming.cancelled_runs(self.parent), [])
        streaming.open_run('offline-later', 1, self.parent, model='offline/model#high')


if __name__ == '__main__':
    unittest.main()
