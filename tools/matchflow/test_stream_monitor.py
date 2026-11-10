"""Offline streaming monitor regressions. No claims, service calls or workers."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import batch
import evidence
import lifecycle
import service


class Waiting(Exception):
    pass


class StreamMonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)/'stream'
        self.folder.mkdir()
        lifecycle.save(self.folder/'batch.json', dict(schema_version=3, batch='stream', streaming=True, tasks=[]))
        with lifecycle.transaction(self.folder) as db:
            db.execute('''CREATE TABLE stream_tasks (seq INTEGER PRIMARY KEY, worker TEXT UNIQUE,
                          address TEXT UNIQUE, payload TEXT, state TEXT)''')
            db.execute('INSERT INTO control VALUES(1,?)', (json.dumps(dict(
                heartbeat=time.monotonic(), stream=dict(requested=2, producer_state='running',
                parent='ses_parent', discovery_session='ses_discovery', backlog=2))),))
        self.patch(batch, 'BATCHES', self.folder.parent)
        # The helper is owned by streaming.py's implementation, not this monitor.
        self.manifest = self.patch(lifecycle, 'manifest', side_effect=self.read_manifest, create=True)
        self.notes = self.patch(service, 'closing_notes_record', return_value=dict(status='acknowledged'))
        self.report = self.patch(evidence, 'local_completion', return_value=dict(complete=True))
        self.patch(lifecycle, 'phase', return_value='experiment')
        self.real_debrief = batch.debrief
        self.debrief = self.patch(batch, 'debrief', return_value=dict(workers=[]))
        self.sleep = self.patch(batch.time, 'sleep', side_effect=Waiting)

    def patch(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def read_manifest(self, folder):
        value = lifecycle.load(folder/'batch.json')
        with lifecycle.transaction(folder) as db:
            value['tasks'] = [json.loads(row[0]) for row in db.execute('SELECT payload FROM stream_tasks ORDER BY seq')]
        return value

    def control(self, **changes):
        with lifecycle.transaction(self.folder) as db:
            value = json.loads(db.execute('SELECT payload FROM control WHERE id=1').fetchone()[0])
            stream = changes.pop('stream', {})
            value['stream'].update(stream)
            value.update(changes)
            db.execute('UPDATE control SET payload=? WHERE id=1', (json.dumps(value),))
        return value

    def task(self, number=1, queue='assigned', state='claimed', **extra):
        worker = 'worker%02d' % number
        task = dict(id=worker, address='0x%08x' % number, assignment_sha256='fixture', model='fixture/model')
        (self.folder/worker).mkdir(exist_ok=True)
        with lifecycle.transaction(self.folder) as db:
            db.execute('INSERT INTO stream_tasks VALUES(?,?,?,?,?)',
                       (number, worker, task['address'], json.dumps(task), queue))
            if state is not None:
                payload = dict(state=state, claim_id='clm_%s' % number, session_id='ses_%s' % number, **extra)
                db.execute('INSERT INTO workers VALUES(?,?)', (worker, json.dumps(payload)))
        return task

    def monitor(self):
        return batch.monitor('stream')

    def waiting(self):
        with self.assertRaises(Waiting):
            self.monitor()

    def test_empty_open_producer_is_not_complete(self):
        self.waiting()
        self.debrief.assert_not_called()

    def test_tasks_are_reloaded_every_iteration(self):
        def append(_):
            self.task(queue='queued', state=None)
        self.sleep.side_effect = append
        result = self.monitor()
        self.assertEqual(result['state'], 'queue_available')
        self.assertEqual(result['workers'][0]['worker'], 'worker01')
        self.assertEqual(self.manifest.call_count, 2)

    def test_queue_is_transactionally_deduplicated_across_rearms(self):
        self.task(queue='queued', state=None)
        self.assertEqual(self.monitor()['state'], 'queue_available')
        self.waiting()
        self.task(2, queue='queued', state=None)
        self.assertEqual([w['worker'] for w in self.monitor()['workers']], ['worker02'])
        self.assertEqual(len(self.control()['monitor_seen_queue']), 2)

    def test_only_queued_entries_are_available(self):
        for number, state in enumerate(('preparing', 'claiming', 'launching', 'assigned', 'blocked', 'failed'), 1):
            self.task(number, queue=state, state='claimed' if state == 'launching' else None)
        result = self.monitor()
        self.assertEqual(result['state'], 'stream_tasks_unresolved')
        self.assertEqual([w['worker'] for w in result['workers']], ['worker06'])
        self.waiting()
        self.assertNotIn('monitor_seen_queue', self.control())

    def test_stop_and_barriers_preempt_queue(self):
        self.task(queue='queued', state=None)
        for barrier in ('STOP', 'cleanup_barrier', 'start_barrier'):
            with self.subTest(barrier=barrier):
                if barrier == 'STOP':
                    (self.folder/'STOP').touch()
                else:
                    self.control(**{barrier: dict(reason=barrier)})
                result = self.monitor()
                self.assertTrue(result['cleanup_required'])
                self.assertIn(barrier.lower(), [event['event'] for event in result['events']])
                self.assertNotIn('monitor_seen_queue', self.control())
                if barrier == 'STOP':
                    (self.folder/'STOP').unlink()
        self.waiting()  # Durable barriers still suppress queued starts after notification.

    def test_stale_supervisor_preempts_queue_even_without_workers(self):
        self.task(queue='queued', state=None)
        self.control(heartbeat=time.monotonic()-31)
        self.assertEqual(self.monitor()['state'], 'supervisor_lost')
        self.assertNotIn('monitor_seen_queue', self.control())

    def test_worker_closure_preempts_queue_and_completed_sibling(self):
        self.task(1, state='closed')
        self.task(2, closure_requested=True)
        self.task(3, queue='queued', state=None)
        result = self.monitor()
        self.assertEqual(result['state'], 'closure_requested')
        self.assertEqual([w['worker'] for w in result['workers']], ['worker02'])
        self.notes.assert_not_called()

    def test_worker_help_is_immediate_and_deduplicated(self):
        self.task(queue='queued', state=None)
        with lifecycle.transaction(self.folder) as db:
            db.execute('INSERT INTO escalations(id,worker,created,state,payload) VALUES(?,?,?,?,?)',
                       ('req1', 'worker01', lifecycle.stamp(), 'pending', json.dumps(dict(request_id='req1', need='Help'))))
        self.assertEqual(self.monitor()['state'], 'worker_help_requested')
        self.assertEqual(self.monitor()['state'], 'queue_available')
        self.waiting()

    def test_one_worker_complete_only_after_notes_and_report(self):
        self.task(state='closed')
        self.notes.return_value = dict(status='pending')
        self.waiting()
        self.report.assert_not_called()
        self.notes.return_value = dict(status='acknowledged')
        self.report.return_value = dict(complete=False)
        self.waiting()
        self.report.return_value = dict(complete=True)
        result = self.monitor()
        self.assertEqual([event['event'] for event in result['events']], ['worker_complete'])
        self.waiting()  # Producer is still open, despite every existing worker closing.

    def test_done_waits_for_all_reports_including_missing_workers(self):
        self.control(stream=dict(producer_state='done', requested=1))
        self.task(1, state='closed')
        self.task(2, queue='failed', state=None)
        self.assertEqual(self.monitor()['state'], 'stream_tasks_unresolved')
        self.assertEqual(self.monitor()['state'], 'worker_milestones')
        self.waiting()
        self.debrief.assert_not_called()

    def test_complete_requires_terminal_producer_and_reports(self):
        self.control(stream=dict(producer_state='done', requested=1))
        self.task(state='closed')
        self.assertEqual(self.monitor()['state'], 'worker_milestones')
        self.assertEqual(self.monitor()['state'], 'all_complete')

    def test_producer_failure_with_no_tasks_is_surfaced_before_completion(self):
        self.control(stream=dict(producer_state='failed'))
        self.assertEqual(self.monitor()['state'], 'producer_failed')
        self.debrief.assert_not_called()
        self.assertEqual(self.monitor()['state'], 'all_complete')

    def test_exhaustion_is_durable_and_does_not_hide_unresolved_tasks(self):
        self.control(stream=dict(producer_state='done'))
        self.task(queue='claiming', state=None)
        self.assertEqual(self.monitor()['state'], 'stream_tasks_unresolved')
        result = self.monitor()
        self.assertEqual(result['state'], 'producer_exhausted')
        self.assertEqual(result['prepared'], 1)
        self.assertEqual(result['claimed'], 0)
        self.waiting()
        self.debrief.assert_not_called()

    def test_completion_revalidates_producer_after_evidence_checks(self):
        self.control(stream=dict(producer_state='done', requested=1))
        self.task(state='closed')
        def reopen(*args):
            self.control(stream=dict(producer_state='running'))
            return dict(complete=True)
        self.report.side_effect = reopen
        self.assertEqual(self.monitor()['state'], 'worker_milestones')
        self.waiting()
        self.debrief.assert_not_called()

    def test_completion_revalidates_newly_appended_tasks(self):
        self.control(stream=dict(producer_state='done', requested=1))
        self.task(state='closed')
        def append(*args):
            self.task(2, queue='preparing', state=None)
            self.report.side_effect = None
            return dict(complete=True)
        self.report.side_effect = append
        self.assertEqual(self.monitor()['state'], 'stream_tasks_unresolved')
        self.assertEqual(self.monitor()['state'], 'worker_milestones')
        self.waiting()
        self.debrief.assert_not_called()

    def test_legacy_closed_worker_milestones_are_unchanged(self):
        task = self.task(state='closed')
        lifecycle.save(self.folder/'batch.json', dict(schema_version=3, batch='stream', tasks=[task]))
        self.report.return_value = dict(complete=False)
        result = self.monitor()
        self.assertEqual([event['event'] for event in result['events']], ['claim_closed', 'notes_complete'])

    def test_preparing_without_assignment_hash_is_an_urgent_terminal_error(self):
        task = self.task(queue='preparing', state=None)
        task.pop('assignment_sha256')
        with lifecycle.transaction(self.folder) as db:
            db.execute('UPDATE stream_tasks SET payload=?', (json.dumps(task),))
        self.control(stream=dict(producer_state='done'))
        result = self.monitor()
        self.assertEqual(result['state'], 'stream_tasks_unresolved')
        self.assertTrue(result['cleanup_required'])
        self.report.assert_not_called()

    def test_exhaustion_uses_secured_claims_not_task_count(self):
        self.control(stream=dict(producer_state='done', claimed=0, requested=1))
        self.task(queue='blocked', state='closed')
        result = self.monitor()
        self.assertEqual(result['state'], 'producer_exhausted')
        self.assertEqual(result['claimed'], 0)

    def test_help_requests_reads_stream_tasks_not_empty_header(self):
        self.task()
        with patch.object(lifecycle, 'escalations', return_value=dict(requests=[], responses=[])) as requests:
            batch.help_requests('stream', 'worker01')
        requests.assert_called_once_with(self.folder, 'worker01')

    def test_answer_recognizes_worker_from_stream_manifest(self):
        self.task()
        with patch.object(lifecycle, 'answer_escalation', return_value=dict(request_id='req1')) as answer:
            self.assertTrue(batch.answer('stream', 'worker01', 'Concrete help')['answered'])
        answer.assert_called_once()

    def test_audit_reads_stream_manifest(self):
        self.task()
        status = self.folder/'status.json'
        lifecycle.save(status, dict(checked_at=lifecycle.stamp(), status=dict(open_claims=[])))
        with patch.object(evidence, 'audit_worker', return_value=dict(evidence_integrity='valid')):
            result = batch.audit('stream', status)
        self.assertEqual([row['worker'] for row in result['workers']], ['worker01'])

    def test_debrief_reads_stream_manifest(self):
        self.task(state='closed')
        result = self.real_debrief('stream')
        self.assertEqual(result['counts']['assignments'], 1)
        self.assertEqual(result['workers'][0]['worker'], 'worker01')


if __name__ == '__main__':
    unittest.main()
