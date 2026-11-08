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
        self.root = Path(self.tmp.name)

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
        self.root = Path(self.tmp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for module, name, value in ((batch, 'ROOT', self.root), (batch, 'BATCHES', self.root/'work/batches'),
                                    (broker, 'workspace_root', lambda: self.root),
                                    (ledger, 'DEFAULT_DB', self.root/'work/matchflow/ledger.sqlite3')):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch('preflight.require'))
        item = dict(address='0x00123456', size=100, state='open', co_workers=0,
                    give_ups=0, open_problem_reports=0, file_problem_reports=0, lane='production',
                    reason='Reviewed current metadata', history_reviewed=True, history_sources=['fixture'],
                    prior_verdicts=[], verdict_history='reviewed')
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
            ledger.register(db, argparse.Namespace(claim='clm_fixture', address='0x00123456', lane='production',
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
        with self.assertRaises(ValueError):
            self.call('write', path='candidate.cpp', text='changed')

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
