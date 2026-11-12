import copy
import hashlib
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import releases
import control


class ReleaseDeliveryTests(unittest.TestCase):
    def release(self, tag='v3.0.29'):
        return {'id': 11, 'tag_name': tag, 'draft': False, 'published_at': '2026-11-10',
                'html_url': 'https://github.com/snapetech-org/pleazyng/releases/tag/' + tag,
                'assets': [{'name': 'pleazyng-linux-x64.tar.gz', 'digest': 'sha256:' + 'c' * 64}]}

    def verified(self):
        return [{'verificationResult': {'signature': {'certificate': {
            'sourceRepositoryIdentifier': str(releases.REPOSITORY_ID),
            'sourceRepositoryDigest': 'a' * 40,
            'runInvocationURI': f'https://github.com/{control.CANONICAL}/actions/runs/42/attempts/1'}}}}]

    def test_orders_pending_releases_and_ignores_drafts_tools_and_old_releases(self):
        items = [self.release('v3.0.30'), self.release(), self.release('v3.0.7'),
                 self.release('flutter-security-3.47.8'), {**self.release('v3.0.31'), 'draft': True}]
        self.assertEqual(releases.choose(items, {})['tag_name'], 'v3.0.29')
        self.assertEqual(releases.choose(items, {'v3.0.29': {}})['tag_name'], 'v3.0.30')
        self.assertIsNone(releases.choose(items[:1], {'v3.0.30': {}}))

    def test_verified_identity_comes_from_certificate_and_requires_unique_producer(self):
        good = self.verified()
        self.assertEqual(releases.producer_from_verification(good), ('a' * 40, 42))
        for key, value in [('sourceRepositoryIdentifier', '99'), ('sourceRepositoryDigest', 'main'),
                           ('runInvocationURI', 'https://github.com/attacker/repo/actions/runs/42')]:
            bad = copy.deepcopy(good)
            bad[0]['verificationResult']['signature']['certificate'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                releases.producer_from_verification(bad)
        other = copy.deepcopy(good)
        other[0]['verificationResult']['signature']['certificate']['sourceRepositoryDigest'] = 'b' * 40
        with self.assertRaises(ValueError):
            releases.producer_from_verification(good + other)

    def test_package_counter_survives_failed_uploads_and_different_workflow_counters(self):
        self.assertEqual(releases.next_release({'release': 4}, {'release': 12, 'phase': 'failed'}, 2), 13)
        self.assertEqual(releases.next_release({'release': 14}, {'release': 12}, 2), 15)
        self.assertEqual(releases.next_release({}, {}, 20), 21)

    def test_release_requires_unique_digest_bound_linux_asset(self):
        release = self.release()
        releases.linux_asset(release)
        for assets in [[], release['assets'] * 2, [{'name': 'pleazyng-linux-x64.tar.gz', 'digest': None}]]:
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                releases.linux_asset({**release, 'assets': assets})

    def test_hold_prevents_even_release_lookup_and_download(self):
        with patch.object(releases, 'mirror_main', return_value='b' * 40), patch.object(control, 'refresh'), patch.object(control, 'read', return_value={'active': True}), \
                patch.object(releases, 'emit') as emit, patch.object(control, 'run') as run:
            releases.reconcile()
            emit.assert_called_once_with({'build': False, 'reason': 'owner-hold'})
            run.assert_not_called()

    def test_resolve_preserves_compiler_and_assembler_runs_for_same_frozen_source(self):
        payload = b'existing immutable Linux artifact; no compiler runs'
        checksum = hashlib.sha256(payload).hexdigest()
        release = self.release()
        release['assets'][0]['digest'] = 'sha256:' + checksum
        calls = []
        def download(arguments):
            calls.append(arguments)
            name = arguments[arguments.index('--pattern') + 1]
            target = Path(arguments[arguments.index('--dir') + 1]) / name
            target.write_bytes((checksum + '  pleazyng-linux-x64.tar.gz\n').encode()
                               if name == 'SHA256SUMS.txt' else payload)
            return SimpleNamespace(stdout='')
        with patch.object(control, 'run', side_effect=download), \
                patch.object(releases, 'verify_file', side_effect=[('a' * 40, 99), ('a' * 40, 42)]), \
                patch.object(releases, 'completed_core') as completed, patch.object(releases, 'release_tag_commit', return_value='a' * 40):
            result = releases.resolve(release)
        completed.assert_called_once_with('a' * 40, 99)
        self.assertEqual(result['source_sha'], 'a' * 40)
        self.assertEqual(result['core_producer_run_id'], 42)
        self.assertEqual(result['core_run_id'], 99)
        self.assertEqual(result['core_linux_sha256'], checksum)
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(Path(call[call.index('--dir') + 1]).exists() for call in calls))

    def test_failed_release_backoff_does_not_block_a_new_release(self):
        attempts = {'v3.0.29': {'phase': 'failed', 'retry_after': '2099-01-01T00:00:00+00:00'}}
        items = [self.release(), self.release('v3.0.30')]
        self.assertEqual(releases.choose(items, {}, attempts)['tag_name'], 'v3.0.30')
        self.assertEqual(releases.choose(items, {}, attempts, retry=True)['tag_name'], 'v3.0.29')
        self.assertIsNone(releases.choose([self.release()], {}, attempts))
        attempts['v3.0.29']['phase'] = 'superseded'
        self.assertIsNone(releases.choose([self.release()], {}, attempts, retry=True))

    def test_repaired_assembler_keeps_original_linux_certificate_identity(self):
        payload = b'original frozen Linux bytes'
        checksum = hashlib.sha256(payload).hexdigest()
        release = self.release()
        release['assets'][0]['digest'] = 'sha256:' + checksum

        def download(arguments):
            name = arguments[arguments.index('--pattern') + 1]
            target = Path(arguments[arguments.index('--dir') + 1]) / name
            target.write_bytes((checksum + '  pleazyng-linux-x64.tar.gz\n').encode()
                               if name == 'SHA256SUMS.txt' else payload)

        recovery = {'packaging_source_sha': 'd' * 40, 'receipt_sha256': 'e' * 64, 'approval': {}}
        with patch.object(control, 'run', side_effect=download), \
                patch.object(releases, 'verify_file', side_effect=[('b' * 40, 99), ('a' * 40, 42)]), \
                patch.object(releases, 'completed_core'), patch.object(releases, 'release_tag_commit', return_value='a' * 40), \
                patch.object(releases.packaging_recovery, 'verify', return_value=recovery) as verify:
            result = releases.resolve(release)
        self.assertEqual(result['source_sha'], 'a' * 40)
        self.assertEqual(result['core_source_sha'], 'b' * 40)
        self.assertEqual(result['core_packaging_source_sha'], 'd' * 40)
        self.assertEqual(result['core_recovery_sha256'], 'e' * 64)
        self.assertEqual(verify.call_args.kwargs['producer_run'], 42)
        self.assertEqual(verify.call_args.kwargs['assembly_run'], 99)
        self.assertEqual(verify.call_args.kwargs['linux_sha256'], checksum)
        with patch.object(control, 'run', side_effect=download), \
                patch.object(releases, 'verify_file', side_effect=[('b' * 40, 99), ('a' * 40, 42)]), \
                patch.object(releases, 'completed_core'), patch.object(releases, 'release_tag_commit', return_value='a' * 40):
            with self.assertRaisesRegex(ValueError, 'public attested receipt'):
                releases.resolve({**release, 'self_asserted_unchanged_product': True})
    def test_resolution_failure_is_recorded_before_source_outputs_exist(self):
        reads = {'sync-state/promotion-hold.json': {}, 'sync-state/releases.json': {},
                 'sync-state/release-attempts.json': {}, 'sync-state/last-good.json': {}}
        with patch.object(releases, 'mirror_main', return_value='b' * 40), patch.object(control, 'refresh'), \
                patch.object(control, 'read', side_effect=lambda p, default=None: reads[p]), \
                patch.object(control, 'run', return_value=SimpleNamespace(stdout=json.dumps([[self.release()]]))), \
                patch.object(releases, 'resolve', side_effect=ValueError('core assembler failed')), \
                patch.object(control, 'write') as write, patch.object(control, 'commit') as commit:
            with self.assertRaisesRegex(ValueError, 'core assembler failed'):
                releases.reconcile()
        attempt = write.call_args.args[1]['v3.0.29']
        self.assertEqual(attempt['phase'], 'failed')
        self.assertIn('retry_after', attempt)
        commit.assert_called_once()

    def test_public_install_retry_never_rebuilds_an_already_published_app(self):
        receipt = {'source_sha': 'a' * 40}
        result = releases.pending_public({'v3.0.30': receipt})
        self.assertFalse(result['build'])
        self.assertTrue(result['verify_public'])
        receipt['public_verification'] = {'status': 'failed', 'retry_after': '2099-01-01T00:00:00+00:00'}
        self.assertFalse(releases.pending_public({'v3.0.30': receipt})['verify_public'])
        self.assertTrue(releases.pending_public({'v3.0.30': receipt}, retry=True)['verify_public'])
        receipt['public_verification'] = {'status': 'passed'}
        self.assertIsNone(releases.pending_public({'v3.0.30': receipt}))

    def test_public_receipt_cannot_claim_pass_without_exact_package_and_checks(self):
        receipt = {'source_sha': 'a' * 40, 'version': '3.0.30+189', 'release': 3,
                   'channel_tag': 'main-' + 'a' * 12 + '-r3'}
        env = {'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_RUN_ID': '42'}
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / 'public.json'
            report.write_text(json.dumps({'status': 'passed', 'source_sha': 'a' * 40,
                                         'version': '3.0.30+189', 'release': 3,
                                         'tag': receipt['channel_tag'], 'checks': []}))
            with patch.dict(releases.os.environ, env), patch.object(control, 'refresh'), \
                    patch.object(control, 'read', return_value={'v3.0.30': receipt}), \
                    patch.object(control, 'write') as write, patch.object(control, 'commit'):
                with self.assertRaises(ValueError):
                    releases.record_public('v3.0.30', 'a' * 40, report)
                write.assert_not_called()
                releases.record_public('v3.0.30', 'a' * 40)
                failed = write.call_args.args[1]['v3.0.30']['public_verification']
                self.assertEqual(failed['status'], 'failed')
                self.assertIn('retry_after', failed)

    def test_version_tag_resolves_lightweight_and_annotated_commits(self):
        direct = {'object': {'type': 'commit', 'sha': 'a' * 40}}
        with patch.object(control, 'api', return_value=direct):
            self.assertEqual(releases.release_tag_commit('v3.0.30'), 'a' * 40)
        annotated = {'object': {'type': 'tag', 'sha': 'b' * 40}}
        with patch.object(control, 'api', side_effect=[annotated, direct]):
            self.assertEqual(releases.release_tag_commit('v3.0.30'), 'a' * 40)
        for invalid in [annotated, {'object': {'type': 'blob', 'sha': 'a' * 40}},
                        {'object': {'type': 'commit', 'sha': 'main'}}]:
            with self.subTest(invalid=invalid), patch.object(control, 'api', return_value=invalid), self.assertRaises(ValueError):
                releases.release_tag_commit('v3.0.30')

    def test_frozen_tag_binding_rejects_moved_tag_or_unproved_recovery_sources(self):
        releases.require_frozen_source('a' * 40, 'a' * 40, 'a' * 40)
        for compiler, assembler in [('b' * 40, 'a' * 40), ('a' * 40, 'b' * 40), ('b' * 40, 'b' * 40)]:
            with self.subTest(compiler=compiler, assembler=assembler), self.assertRaisesRegex(ValueError, 'recovery proof'):
                releases.require_frozen_source('a' * 40, compiler, assembler)

    def test_core_assembler_must_be_successful_trusted_and_exact(self):
        metadata = {'repository': {'id': releases.REPOSITORY_ID}, 'path': '.github/workflows/build.yml',
                    'head_sha': 'b' * 40, 'status': 'completed', 'conclusion': 'success',
                    'event': 'workflow_dispatch'}
        with patch.object(control, 'api', return_value=metadata):
            releases.completed_core('b' * 40, 99)
        for change in [{'conclusion': 'failure'}, {'status': 'in_progress'}, {'head_sha': 'c' * 40},
                       {'event': 'pull_request'}, {'repository': {'id': 99}}, {'path': 'other.yml'}]:
            with self.subTest(change=change), patch.object(control, 'api', return_value={**metadata, **change}), self.assertRaises(ValueError):
                releases.completed_core('b' * 40, 99)

    def test_source_tracking_updates_metadata_without_compiling_or_promoting(self):
        import source_sync
        with patch.object(source_sync, 'sync', return_value={'source_sha': 'a' * 40}), \
                patch.object(control, 'refresh') as refresh, patch.object(control, 'write') as write, \
                patch.object(control, 'commit') as commit, patch.object(control, 'run') as run:
            self.assertEqual(releases.mirror_main(), 'a' * 40)
        refresh.assert_called_once()
        write.assert_not_called()
        commit.assert_not_called()
        run.assert_not_called()

    def test_cross_repository_native_reuse_is_rejected(self):
        q = {'source_sha': 'a' * 40}
        env = {'SOURCE_SHA': 'a' * 40, 'QUALIFIED_RUN_ID': '42'}
        with patch.dict(releases.os.environ, env), patch.object(control, 'validated', return_value=(q, {})), \
                patch.object(control, 'api', side_effect=[
                    {'repository': {'full_name': 'other/repo'}, 'path': '.github/workflows/sync.yml',
                     'status': 'completed', 'event': 'workflow_dispatch'},
                    {'jobs': [{'name': 'qualify / Native aerynOS install and release-app QA', 'conclusion': 'success'}]},
                    {'artifacts': [{'name': 'aerynos-qualified-' + 'a' * 40, 'expired': False}]},
                ]) as api:
            with self.assertRaisesRegex(ValueError, 'successful artifact from this channel'):
                releases.bind_qualified(Path('unused'))
        self.assertEqual(api.call_count, 3)

    def test_binding_checks_published_bytes_version_and_original_producer(self):
        q = {'source_sha': 'a' * 40, 'version': '3.0.29+188', 'build': {
            'assets': {'pleazyng-linux-x64.tar.gz': 'c' * 64},
            'provenance': {'kind': 'attested-core-linux-artifact', 'verified_source_sha': 'a' * 40,
                           'producer_run_id': 42}}}
        environment = {'SOURCE_SHA': 'a' * 40, 'CORE_TAG': 'v3.0.29', 'CORE_RELEASE_ID': '11',
                       'CORE_LINUX_SHA256': 'c' * 64, 'CORE_PRODUCER_RUN_ID': '42',
                       'CORE_RUN_ID': '99', 'CORE_SOURCE_SHA': 'a' * 40}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'SHA256SUMS.txt').write_text('')
            with patch.object(releases, 'release_tag_commit', return_value='a' * 40), patch.object(releases, 'completed_core'), patch.dict(releases.os.environ, environment), patch.object(control, 'validated', return_value=(q, {'pleazyng-linux-x64.tar.gz': 'c' * 64})), \
                    patch.object(control, 'api', return_value=self.release()):
                receipt = releases.bind_qualified(root)
                self.assertEqual(receipt['linux_producer_run_id'], 42)
                self.assertIn('core-release.json', (root / 'SHA256SUMS.txt').read_text())
                for changed in [{'CORE_SOURCE_SHA': 'b' * 40}, {'SOURCE_SHA': 'b' * 40}, {'CORE_PRODUCER_RUN_ID': '43'}, {'CORE_LINUX_SHA256': 'd' * 64},
                                {'CORE_RELEASE_ID': '12'}, {'CORE_TAG': 'v3.0.30'}]:
                    with self.subTest(changed=changed), patch.dict(releases.os.environ, changed), self.assertRaises(ValueError):
                        releases.bind_qualified(root)
                with patch.object(control, 'api', return_value={**self.release(), 'draft': True}), self.assertRaises(ValueError):
                    releases.bind_qualified(root)

    def test_native_binding_repeats_repair_proof_and_rejects_changed_identity(self):
        q = {'source_sha': 'a' * 40, 'version': '3.0.29+188', 'build': {
            'assets': {'pleazyng-linux-x64.tar.gz': 'c' * 64},
            'provenance': {'kind': 'attested-core-linux-artifact', 'verified_source_sha': 'a' * 40,
                           'producer_run_id': 42}}}
        env = {'SOURCE_SHA': 'a' * 40, 'CORE_TAG': 'v3.0.29', 'CORE_RELEASE_ID': '11',
               'CORE_LINUX_SHA256': 'c' * 64, 'CORE_PRODUCER_RUN_ID': '42', 'CORE_RUN_ID': '99',
               'CORE_SOURCE_SHA': 'b' * 40, 'CORE_PACKAGING_SOURCE_SHA': 'd' * 40, 'CORE_RECOVERY_SHA256': 'e' * 64}
        recovery = {'packaging_source_sha': 'd' * 40, 'receipt_sha256': 'e' * 64, 'approval': {'verified': True}}

        def download(arguments):
            (Path(arguments[arguments.index('--dir') + 1]) / 'SHA256SUMS.txt').write_text(
                'c' * 64 + '  pleazyng-linux-x64.tar.gz\n')

        def certificate(path):
            return ('b' * 40, 99) if path.name == 'SHA256SUMS.txt' else ('a' * 40, 42)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'SHA256SUMS.txt').write_text('')
            (root / 'pleazyng-linux-x64.tar.gz').write_bytes(b'qualified input')
            with patch.dict(releases.os.environ, env), patch.object(releases, 'release_tag_commit', return_value='a' * 40), \
                    patch.object(releases, 'completed_core'), patch.object(releases, 'verify_file', side_effect=certificate), \
                    patch.object(control, 'api', return_value=self.release()), patch.object(control, 'run', side_effect=download), \
                    patch.object(control, 'validated', return_value=(q, {'pleazyng-linux-x64.tar.gz': 'c' * 64})), \
                    patch.object(releases.packaging_recovery, 'verify', return_value=recovery) as verify:
                result = releases.bind_qualified(root)
                self.assertEqual(result['packaging_recovery'], recovery)
                with patch.dict(releases.os.environ, {'CORE_RECOVERY_SHA256': 'f' * 64}):
                    with self.assertRaisesRegex(ValueError, 'changed after native qualification'):
                        releases.bind_qualified(root)
                self.assertEqual(verify.call_count, 2)


if __name__ == '__main__':
    unittest.main()
