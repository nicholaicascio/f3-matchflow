"""Regression fixtures contain no private game data. All network calls are fake."""
import argparse
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import batch
import evidence
import lifecycle as life
import matchflow as m
import service
import usage

REVIEW = dict(history_reviewed=True, history_sources=['fixture'], prior_verdicts=[],
              verdict_history='reviewed', report_scope='none', report_evidence=None,
              prior_unfit=False, next_experiment=None, integration_plan=None)


class Controls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worker = 'worker01'
        (self.root/self.worker).mkdir()
        self.task = dict(id=self.worker, address='0x00123456', model='test/model#max')
        life.save(self.root/'batch.json', dict(tasks=[self.task]))
        self.assignment = dict(schema_version=2, harness='Offline Test', workspace=str(self.root),
                               claim_body=dict(address=self.task['address'], model='test/model',
                                               harness='Offline Test', cowork=False, context_mode='resume', effort='max'))
        life.save(self.root/self.worker/'assignment.json', self.assignment)
        life.dispatch(self.root, 'subagents')
        life.tick(self.root, 'watcher')
        life.bind(self.root, self.worker, 'ses_fixture')

    def claim(self):
        life.reserve_claim(self.root, self.worker)
        return life.claimed(self.root, self.worker, 'clm_fixture', self.task['address'],
                            life.stamp(life.utc()+timedelta(hours=2)))

    def change(self, **values):
        with life.transaction(self.root) as db:
            state = life.read_worker(db, self.worker)
            state.update(values)
            life.write_worker(db, self.worker, state)

    def test_duplicate_dispatch_and_claim_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'already dispatched'):
            life.dispatch(self.root, 'workflow')
        self.claim()
        with self.assertRaisesRegex(ValueError, 'one claim'):
            life.reserve_claim(self.root, self.worker)

    def test_closed_cannot_reclaim(self):
        self.claim()
        life.begin_close(self.root, self.worker)
        life.closed(self.root, self.worker, 'given_up', 'ack.json', 'retained')
        with self.assertRaises(ValueError):
            life.reserve_claim(self.root, self.worker)

    def test_crashed_pending_call_is_not_retried(self):
        life.reserve_claim(self.root, self.worker)
        with self.assertRaises(ValueError):
            life.reserve_claim(self.root, self.worker)

    def test_reconcile_recovers_claim_id_without_new_claim_call(self):
        life.reserve_claim(self.root, self.worker)
        life.save(self.root/self.worker/'claim-response.json', dict(claim_id='clm_fixture'))
        def api(tool, body):
            self.assertEqual(tool, 'status')
            return self.api(tool, body)
        before = life.snapshot(self.root, self.worker)['monotonic_start']
        result = service.reconcile(self.root, self.worker, api=api)
        self.assertEqual(result['claim_id'], 'clm_fixture')
        self.assertEqual(life.snapshot(self.root, self.worker)['monotonic_start'], before)

    def test_unavailable_target_generates_valid_blocked_report_without_claim(self):
        def api(tool, body):
            self.assertEqual(tool, 'function_info')
            return dict(state='paused')
        result = service.claim(self.root, self.worker, api)
        self.assertEqual(result['outcome'], 'blocked')
        evidence.report(self.root, self.worker, self.root/'unused.sqlite3')
        state = life.snapshot(self.root, self.worker)
        audited = evidence.audit_worker(self.root, self.task, state, dict(open_claims=[]))
        self.assertEqual(audited['evidence_errors'], [])
        self.assertEqual(audited['lease_closure'], 'not_claimed')

    def test_stop_marker_prevents_new_experiments(self):
        (self.root/'STOP').touch()
        with self.assertRaisesRegex(ValueError, 'STOP'):
            life.require_experiment(self.root, self.worker)

    def test_changed_claim_body_is_rejected_before_network(self):
        self.assignment['claim_body']['address'] = '0x00123457'
        life.save(self.root/self.worker/'assignment.json', self.assignment)
        with self.assertRaisesRegex(ValueError, 'differs'):
            service.claim(self.root, self.worker, lambda *args: self.fail('network must not be called'))

    def test_dead_watchdog_fails_closed(self):
        with life.transaction(self.root) as db:
            value = json.loads(db.execute('SELECT payload FROM control').fetchone()[0])
            value['heartbeat'] = life.time.monotonic()-31
            db.execute('UPDATE control SET payload=?', (json.dumps(value),))
        with self.assertRaisesRegex(ValueError, 'stale'):
            life.require_experiment(self.root, self.worker)

    def test_watchdog_cannot_restart_window(self):
        with self.assertRaisesRegex(ValueError, 'do not restart'):
            life.tick(self.root, 'replacement-watcher')

    def test_local_wall_clock_backwards_cannot_extend(self):
        state = life.snapshot(self.root, self.worker)
        with patch.object(life.time, 'monotonic', return_value=state['monotonic_start']+2401), \
                patch.object(life, 'utc', return_value=life.utc()-timedelta(hours=7)):
            self.assertNotEqual(life.phase(state), 'experiment')

    def test_clock_discontinuity_requires_reconciliation(self):
        state = life.snapshot(self.root, self.worker)
        with patch.object(life.time, 'monotonic', return_value=state['monotonic_start']-1):
            self.assertEqual(life.phase(state), 'reconcile')

    def test_short_lease_reduces_both_deadlines(self):
        life.reserve_claim(self.root, self.worker)
        state = life.claimed(self.root, self.worker, 'clm_fixture', self.task['address'],
                             life.stamp(life.utc()+timedelta(minutes=30)))
        self.assertLess(state['experiment_seconds'], 601)
        self.assertLess(state['closure_seconds'], 901)

    def test_stagnation_and_register_wall_do_not_reset_on_hypothesis(self):
        self.claim()
        state = life.progress(self.root, self.worker, ['/O2'], 100)
        old = state['last_improvement_monotonic']
        state = life.progress(self.root, self.worker, ['/O2'], 100)
        self.assertEqual(old, state['last_improvement_monotonic'])
        first = life.progress(self.root, self.worker, register_wall=True, evidence='only slots differ')
        second = life.progress(self.root, self.worker, register_wall=True, evidence='another hypothesis')
        self.assertEqual(first['register_wall_monotonic'], second['register_wall_monotonic'])
        self.assertLessEqual(life.experiment_limit(second), 1201)

    def test_only_improvement_in_same_metric_flags_resets_stagnation(self):
        self.claim()
        state = life.progress(self.root, self.worker, ['/Od'], 100)
        before = state['last_improvement_monotonic']
        self.assertEqual(before, life.progress(self.root, self.worker, ['/O2'], 20)['last_improvement_monotonic'])
        self.assertGreater(life.progress(self.root, self.worker, ['/O2'], 19)['last_improvement_monotonic'], before)

    def test_watchdog_requests_and_escalates_without_remote_release(self):
        state = life.snapshot(self.root, self.worker)
        with patch.object(life.time, 'monotonic', return_value=state['monotonic_start']+2701):
            events = life.tick(self.root, 'watcher')
        self.assertTrue(events)
        self.assertTrue((self.root/self.worker/'CLOSE_REQUESTED.json').exists())
        self.assertTrue((self.root/'RECONCILE_REQUIRED.json').exists())
        self.assertTrue(life.snapshot(self.root, self.worker)['cleanup_required'])

    def test_real_seven_digit_powershell_timestamp(self):
        self.assertEqual(life.timestamp('2026-11-08T04:47:34.1194923+00:00').microsecond, 119492)
        with self.assertRaises(ValueError):
            life.timestamp('2026-11-08T05:05:0?Z')
        with self.assertRaises(ValueError):
            life.timestamp('2026-11-08T05:05:00')

    def api(self, tool, body):
        if tool == 'function_info':
            return dict(state='open', regions=dict(co_workers=0))
        if tool == 'claim':
            return dict(claim_id='clm_fixture')
        if tool == 'status':
            return dict(open_claims=[dict(claim_id='clm_fixture', address=self.task['address'],
                                          this_key=True, expires=life.stamp(life.utc()+timedelta(hours=2)))])
        if tool == 'giveup':
            self.assertIn('best_code', body)
            self.assertNotIn('checkpoint_id', body)
            return dict(released=True, best_code='best_code kept: fixture')
        self.fail('unexpected remote action ' + tool)

    def test_gateway_claim_binds_exact_status_and_persists(self):
        result = service.claim(self.root, self.worker, self.api)
        self.assertEqual(result['claim_id'], 'clm_fixture')
        self.assertTrue((self.root/self.worker/'ownership.json').exists())

    def test_ambiguous_transport_consumes_claim_reservation(self):
        def api(tool, body):
            if tool == 'claim':
                raise TimeoutError('fixture')
            return self.api(tool, body)
        with self.assertRaises(TimeoutError):
            service.claim(self.root, self.worker, api)
        self.assertEqual(life.snapshot(self.root, self.worker)['state'], 'claim_pending')

    def test_other_key_claim_cannot_be_closed(self):
        self.claim()
        def api(tool, body):
            return dict(open_claims=[dict(claim_id='clm_fixture', address=self.task['address'], this_key=False)])
        with self.assertRaisesRegex(ValueError, 'this key'):
            service.close(self.root, self.worker, 'giveup', api=api)

    def test_source_handoff_uses_best_code_only_and_frozen_hash(self):
        self.claim()
        source = self.root/'best.cpp'
        source.write_bytes(b'void f() {}\r\n')
        notes = dict(reason='fixture', category='other', tried='one attempt', still_differs='one instruction', suspected_cause='fixture')
        result = service.close(self.root, self.worker, 'giveup', source, notes,
                               db_path=self.root/'ledger.sqlite3', api=self.api)
        self.assertEqual(result['assessment']['source_retention'], 'retained')
        self.assertEqual(result['source_sha256'], m.digest(source))
        self.assertEqual(life.snapshot(self.root, self.worker)['state'], 'closed')

    def test_gateway_reports_failed_retention_even_when_lease_closed(self):
        self.claim()
        source = self.root/'best.cpp'
        source.write_text('void f() {}')
        notes = dict(reason='fixture', category='other', tried='one attempt', still_differs='one instruction', suspected_cause='fixture')
        def api(tool, body):
            if tool == 'giveup':
                return dict(released=True, checkpoint='checkpoint holds a note only')
            return self.api(tool, body)
        result = service.close(self.root, self.worker, 'giveup', source, notes,
                               db_path=self.root/'ledger.sqlite3', api=api)
        self.assertTrue(result['assessment']['lease_closed'])
        self.assertEqual(life.snapshot(self.root, self.worker)['handoff'], 'failed')

    def test_cannot_reconcile_missing_receipt_into_success(self):
        self.claim()
        life.begin_close(self.root, self.worker)
        with self.assertRaisesRegex(ValueError, 'no acknowledgement'):
            service.reconcile(self.root, self.worker, api=lambda tool, body: dict(open_claims=[]))

    def test_monitor_wakes_on_supervisor_loss_without_polling_service(self):
        with life.transaction(self.root) as db:
            value = json.loads(db.execute('SELECT payload FROM control').fetchone()[0])
            value['heartbeat'] = life.time.monotonic()-31
            db.execute('UPDATE control SET payload=?', (json.dumps(value),))
        with patch.object(batch, 'BATCHES', self.root.parent):
            self.assertEqual(batch.monitor(self.root.name)['state'], 'supervisor_lost')

    def test_note_only_ack_is_a_handoff_failure_not_success(self):
        value = service.acknowledgement(dict(released=True, checkpoint='checkpoint holds a note only'), 'giveup', True)
        self.assertTrue(value['lease_closed'])
        self.assertEqual(value['source_retention'], 'failed')

    def test_bad_legacy_report_does_not_pass_strict_audit(self):
        self.claim()
        state = life.snapshot(self.root, self.worker)
        state.update(closed_utc='2026-11-08T05:05:0?Z', session_id='opencode-session-current')
        life.save(self.root/self.worker/'report.json', dict(schema_version=1))
        result = evidence.audit_worker(self.root, self.task, state, dict(open_claims=[]))
        self.assertEqual(result['evidence_integrity'], 'invalid')
        self.assertEqual(result['lease_closure'], 'absent')
        self.assertTrue(any('timestamp' in e for e in result['evidence_errors']))

    def promoted(self, diagnostic=False):
        self.claim()
        pack = self.root/'packs'/'clm_fixture'
        pack.mkdir(parents=True)
        life.save(pack/'pack.json', dict(address=self.task['address'], flags=['/MT', '/O2'], size=10))
        (pack/'unit.cpp').write_text('void f();')
        source = self.root/'candidate.cpp'
        source.write_text('void f() {}')
        db_path = self.root/'ledger.sqlite3'
        db = m.connect(db_path)
        try:
            m.register(db, argparse.Namespace(claim='clm_fixture', address=self.task['address'], review=REVIEW,
                       model='test/model', variant='max', harness='Offline Test', pack=str(pack),
                       deadline=life.stamp(life.utc()+timedelta(hours=2)), assignment=str(self.root/self.worker/'assignment.json')))
            result = dict(status='match', diffs=0, ndiff_insn=0, extent=10,
                          checker='checker: f3check test, compiler VS2005 SP1')
            args = argparse.Namespace(claim='clm_fixture', source=str(source), flags='/MT,/O1' if diagnostic else None, stage='baseline',
                                      evidence_dir=str(self.root/'checks'))
            with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(result), '')):
                m.check(db, args)
                m.gate(db, argparse.Namespace(claim='clm_fixture', decision='continue', reason='fixture',
                       hypothesis='fixture hypothesis', expected='match retained', integration_plan=None))
                args.stage = 'final'
                m.check(db, args)
            m.promote(db, argparse.Namespace(claim='clm_fixture', output=str(self.root/'match.cpp')))
        finally:
            db.close()
        return db_path

    def test_generated_submit_report_passes_and_tampering_fails(self):
        db_path = self.promoted()
        def api(tool, body):
            if tool == 'submit':
                self.assertEqual(body['source'], 'void f() {}')
                return dict(submission_id='sub_fixture')
            return self.api(tool, body)
        service.close(self.root, self.worker, 'submit', db_path=db_path, api=api)
        service.closing_notes(self.root, self.worker, 'Literal wrapper retained exact codegen.',
                              api=lambda tool, body: dict(checkpoint_id='chk_notes_fixture'))
        generated = evidence.report(self.root, self.worker, db_path)
        self.assertEqual(generated['final']['status'], 'match')
        state = life.snapshot(self.root, self.worker)
        status = dict(open_claims=[], recent=[dict(id='sub_fixture', addr=self.task['address'], verdict='verified')])
        audited = evidence.audit_worker(self.root, self.task, state, status, db_path)
        self.assertEqual(audited['evidence_errors'], [])
        self.assertEqual(audited['verification'], 'verified')
        generated['final']['diffs'] = 1
        life.save(self.root/self.worker/'report.json', generated)
        self.assertEqual(evidence.audit_worker(self.root, self.task, state, status, db_path)['evidence_integrity'], 'invalid')

    def test_report_effective_review_records_discovered_reclassification(self):
        self.assignment['target'] = dict(
            address=self.task['address'], size=10, lane='research', give_ups=1,
            open_problem_reports=0, file_problem_reports=0, history_reviewed=True,
            history_sources=['legacy fixture'], prior_verdicts=[], verdict_history='reviewed',
            new_hypothesis=None)
        life.save(self.root/self.worker/'assignment.json', self.assignment)
        db_path = self.promoted()
        with closing(m.connect(db_path)) as db, db:
            row = m.attempt(db, 'clm_fixture')
            facts = json.loads(row['review_json'])
            facts.update(prior_unfit=True,
                         integration_plan='repair the named unit type clash',
                         next_experiment='recheck the repaired unit')
            db.execute('UPDATE attempts SET review_json=? WHERE claim_id=?',
                       (json.dumps(facts), 'clm_fixture'))
        def api(tool, body):
            if tool == 'submit':
                return dict(submission_id='sub_fixture')
            return self.api(tool, body)
        service.close(self.root, self.worker, 'submit', db_path=db_path, api=api)
        service.closing_notes(self.root, self.worker, 'Discovered unit history recorded.',
                              api=lambda tool, body: dict(checkpoint_id='chk_notes_fixture'))
        generated = evidence.report(self.root, self.worker, db_path)
        self.assertTrue(generated['review']['prior_unfit'])
        self.assertFalse(generated['selection_review']['prior_unfit'])
        audited = evidence.audit_worker(self.root, self.task, life.snapshot(self.root, self.worker),
                                        dict(open_claims=[], recent=[dict(id='sub_fixture', addr=self.task['address'],
                                                                          verdict='verified')]), db_path)
        self.assertEqual(audited['evidence_errors'], [])

    def test_alternate_flags_follow_checkpoint_and_flag_proof_route(self):
        db_path = self.promoted(diagnostic=True)
        calls = []
        def api(tool, body):
            calls.append(tool)
            if tool == 'checkpoint':
                self.assertEqual(body['source'], 'void f() {}')
                return dict(checkpoint_id='chk_fixture')
            if tool == 'flag':
                self.assertEqual(body['compile_flags'], ['/MT', '/O1'])
                self.assertEqual(body['checkpoint_id'], 'chk_fixture')
                self.assertFalse(body['keep_claim'])
                return dict(flag_id='flag_fixture', released=True)
            self.assertNotEqual(tool, 'submit')
            return self.api(tool, body)
        service.close(self.root, self.worker, 'flag-proof', notes=dict(note='exact diagnostic proof fixture'),
                      db_path=db_path, api=api)
        self.assertEqual(calls, ['status', 'checkpoint', 'flag'])
        service.closing_notes(self.root, self.worker, 'Exact alternate flags proved the listed discrepancy.',
                              api=lambda tool, body: dict(checkpoint_id='chk_notes_fixture'))
        generated = evidence.report(self.root, self.worker, db_path)
        self.assertEqual(generated['outcome'], 'flag_review')
        audited = evidence.audit_worker(self.root, self.task, life.snapshot(self.root, self.worker),
                                        dict(open_claims=[], recent=[]), db_path)
        self.assertEqual(audited['evidence_errors'], [])

    def test_reclassifying_invalidates_previous_promotion_gate(self):
        db_path = self.promoted()
        db = m.connect(db_path)
        try:
            m.reclassify(db, argparse.Namespace(claim='clm_fixture', integration_plan='repair a concrete unit type conflict'))
            self.assertTrue(json.loads(m.attempt(db, 'clm_fixture')['review_json'])['prior_unfit'])
            with self.assertRaisesRegex(ValueError, 'submit_ready'):
                service.submit_artifact(db, 'clm_fixture')
        finally:
            db.close()

    def test_current_flag_proof_ack_requires_exact_claim_and_address(self):
        response = dict(claim_released='clm_fixture', flagged=[self.task['address']])
        assessment = service.acknowledgement(response, 'flag-proof', True,
                                             claim_id='clm_fixture', address=self.task['address'])
        self.assertTrue(assessment['lease_closed'])
        self.assertIsNone(assessment['flag_id'])
        for bad in (dict(response, claim_released='clm_other'),
                    dict(response, claim_released=True), dict(response, flagged=[]),
                    dict(response, flagged=self.task['address']),
                    dict(response, flagged=['0x00999999'])):
            with self.subTest(response=bad):
                self.assertFalse(service.acknowledgement(bad, 'flag-proof', True,
                                 claim_id='clm_fixture', address=self.task['address'])['lease_closed'])
        self.assertFalse(service.acknowledgement(response, 'flag-proof', True)['lease_closed'])
        contradictory = dict(response, claim_released='clm_other', released=True, flag_id='flag_fixture')
        self.assertFalse(service.acknowledgement(contradictory, 'flag-proof', True,
                         claim_id='clm_fixture', address=self.task['address'])['lease_closed'])

    def test_current_flag_proof_closes_records_and_audits_without_flag_id(self):
        db_path = self.promoted(diagnostic=True)
        def api(tool, body):
            if tool == 'checkpoint':
                return dict(checkpoint_id='chk_fixture')
            if tool == 'flag':
                return dict(claim_released='clm_fixture', flagged=[self.task['address']])
            return self.api(tool, body)
        service.close(self.root, self.worker, 'flag-proof', notes=dict(note='Exact alternate flags.'),
                      db_path=db_path, api=api)
        with closing(m.connect(db_path)) as db:
            self.assertEqual(m.attempt(db, 'clm_fixture')['state'], 'flag_review')
            self.assertEqual(m.attempt(db, 'clm_fixture')['checkpoint_id'], 'chk_fixture')
        service.closing_notes(self.root, self.worker, 'Exact alternate flags proved the discrepancy.',
                              api=lambda tool, body: dict(checkpoint_id='chk_notes_fixture'))
        evidence.report(self.root, self.worker, db_path)
        result = evidence.audit_worker(self.root, self.task, life.snapshot(self.root, self.worker),
                                       dict(open_claims=[], recent=[]), db_path)
        self.assertEqual(result['evidence_errors'], [])

    def test_recovered_closure_does_not_restart_notes_window(self):
        self.claim()
        life.begin_close(self.root, self.worker)
        observed = life.stamp()
        later = life.utc()+timedelta(minutes=31)
        with patch.object(life, 'utc', return_value=later):
            life.closed(self.root, self.worker, 'flag_review', self.root/'ack.json', 'submitted',
                        closed_utc=observed)
            api = unittest.mock.Mock()
            with self.assertRaisesRegex(ValueError, 'window expired'):
                service.closing_notes(self.root, self.worker, 'Recovered proof closing notes.', api=api)
            api.assert_not_called()

    def test_reconcile_flag_proof_uses_saved_receipt_without_replaying(self):
        db_path = self.promoted(diagnostic=True)
        life.begin_close(self.root, self.worker)
        source = self.root/'match.cpp'
        (self.root/self.worker/'closure-source.cpp').write_bytes(source.read_bytes())
        observed = life.stamp()
        ack_path = self.root/self.worker/'service-ack.json'
        life.save(ack_path, dict(claim_id='clm_fixture', address=self.task['address'], operation='flag-proof',
                  observed_utc=observed, source_sha256=m.digest(source), checkpoint_id='chk_fixture',
                  response=dict(claim_released='clm_fixture', flagged=[self.task['address']]),
                  assessment=dict(lease_closed=False)))
        original = ack_path.read_bytes()
        def api(tool, body):
            self.assertEqual(tool, 'status')
            return dict(open_claims=[])
        with patch.object(life, 'utc', return_value=life.utc()+timedelta(minutes=5)):
            service.reconcile(self.root, self.worker, api=api, db_path=db_path)
        state = life.snapshot(self.root, self.worker)
        self.assertEqual(state['closed_utc'], observed)
        self.assertEqual(state['outcome'], 'flag_review')
        self.assertEqual(ack_path.read_bytes(), original)
        with closing(m.connect(db_path)) as db:
            self.assertEqual(m.attempt(db, 'clm_fixture')['state'], 'flag_review')

    def test_managed_registration_rejects_previous_claim_pack(self):
        self.claim()
        old = self.root/'packs'/'clm_previous'
        old.mkdir(parents=True)
        life.save(old/'pack.json', dict(address=self.task['address'], flags=['/MT', '/O2'], size=10))
        db = m.connect(self.root/'ledger.sqlite3')
        try:
            with self.assertRaisesRegex(ValueError, 'earlier pack'):
                m.register(db, argparse.Namespace(claim='clm_fixture', address=self.task['address'], review=REVIEW,
                           model='test/model', variant='max', harness='Offline Test', pack=str(old),
                           deadline=life.stamp(life.utc()+timedelta(hours=2)),
                           assignment=str(self.root/self.worker/'assignment.json')))
        finally:
            db.close()

    def test_changed_handoff_source_fails_audit(self):
        state = self.claim()
        state.update(closed_utc=life.stamp(), service_ack_path=str(self.root/'ack.json'), outcome='given_up')
        life.save(self.root/'ack.json', dict(claim_id='wrong', address=self.task['address'], operation='giveup',
                                           observed_utc=life.stamp(), response=dict(released=True)))
        result = evidence.audit_worker(self.root, self.task, state, dict(open_claims=[]))
        self.assertTrue(any('claim/address' in e for e in result['evidence_errors']))


class ParsingAndUsage(unittest.TestCase):
    def test_actual_empty_output_exit_15_classified_as_process_failure(self):
        result = m.diagnose_result('', '', 15)
        self.assertEqual(result['failure_kind'], 'process_failure')
        self.assertEqual(result['process_exit_code'], 15)

    def test_empty_success_distinct_from_process_failure(self):
        self.assertEqual(m.diagnose_result('', '', 0)['failure_kind'], 'missing_output')

    def test_ambiguity_never_selects_first_match(self):
        result = m.diagnose_result('{"status":"match"}\n{"status":"wip"}', '', 0)
        self.assertEqual(result['status'], 'tool_error')

    def test_usage_normalizes_only_explicit_semantics(self):
        raw = dict(input=10, output=2, reasoning=4, cacheRead=100)
        self.assertIsNone(usage.normalize(raw, 'unknown'))
        self.assertEqual(usage.normalize(raw, 'uncached')['input_tokens'], 110)
        with self.assertRaises(ValueError):
            usage.normalize(raw, 'total')

    def test_usage_missing_is_not_zero(self):
        with self.assertRaises(ValueError):
            usage.counters(dict(input=1, output=2, reasoning=3))

    def test_schema_flags_not_a_comma_separated_string(self):
        schema = life.load(Path(evidence.__file__).with_name('report.schema.json'))
        errors = evidence.schema_errors({'checked_flags': ['/MT,/O2']}, schema)
        self.assertTrue(any('pattern' in e for e in errors))

    def test_headless_usage_deduplicates_ids_and_rejects_mixed_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'events.jsonl'
            event = dict(sessionID='ses_fixture', part=dict(id='step1', type='step-finish',
                         tokens=dict(input=10, output=2, reasoning=3, cache=dict(read=100))))
            path.write_text(json.dumps(event)+'\n'+json.dumps(event)+'\n')
            self.assertEqual(usage.headless_events(path)['raw']['input'], 10)
            event['sessionID'] = 'ses_other'
            event['part']['id'] = 'step2'
            with path.open('a') as stream:
                stream.write(json.dumps(event)+'\n')
            with self.assertRaisesRegex(ValueError, 'mixed-session'):
                usage.headless_events(path)


if __name__ == '__main__':
    unittest.main()
