import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import control
import packaging_recovery as recovery

S, R, W = ('a' * 40, 'b' * 40, 'c' * 40)
P, A = 42, 43
LINUX = 'd' * 64
APPROVAL = {'independently_verified': 'fixture'}
TV_RUN, TV_R, TV_W = 44, 'f' * 40, 'e' * 40
TV_APPROVAL = {'independently_verified': 'earlier TV repair'}
IOS_RUN, IOS_R, IOS_W = 45, '1' * 40, '2' * 40
IOS_APPROVAL = {'independently_verified': 'earlier iOS repair'}


class PackagingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.required = ['Code Analysis', 'Unit Tests', 'Native Formatting']
        self.receipt = {
            'schema_version': 2, 'recovery_policy': 'frozen-apple-packaging-v1',
            'product_source_sha': S, 'packaging_source_sha': R, 'workflow_source_sha': W,
            'source_run_id': P, 'assembly_run_id': A, 'actual_checkout_sha': R,
            'source_sha': S, 'head_sha': R, 'version': '3.0.31', 'packaging_approval': APPROVAL,
            'linux_archive_sha256': LINUX, 'include_apple_store_build': True,
            'platforms_complete': True, 'pending_jobs': [], 'excluded_optional_jobs': [],
            'rebuild_linux': False, 'rebuild_macos': False, 'rebuild_ios': True, 'rebuild_tvos': True,
            'rebuilt_jobs': sorted(recovery.APPLE), 'reused_jobs': sorted(recovery.REUSED),
            'android_source': {'source_sha': S, 'source_run_id': P},
            'webos_source': {'source_sha': S, 'source_run_id': P},
            'ci_mode': 'embedded', 'ci_source_sha': S, 'ci_run_id': P,
            'artifacts': [{'name': name, 'artifact_id': index, 'digest': 'sha256:' + 'e' * 64,
                           'source_sha': S, 'source_run_id': P}
                          for index, name in enumerate(sorted(recovery.ARTIFACTS), 1)],
        }
        self.original_jobs = [self.job(name) for name in
            recovery.REUSED | {'build-android', 'Build WebOS TV package (ARMv7)', 'Freeze qualified source'}]
        self.original_jobs += [self.job('Release CI / ' + name) for name in self.required]
        self.original_jobs += [self.job('create-release', 'skipped')]
        self.assembly_jobs = [self.job('create-release')]
        for name in recovery.APPLE:
            self.assembly_jobs.append({**self.job(name), 'steps': [self.job(
                'Build signed iOS App Store IPA' if name == 'build-ios' else 'Build signed tvOS App Store IPA')]})
        self.bad_artifact = False
        self.packages = {}
        self.tv_certificate = (TV_W, TV_RUN)
        self.ios_certificate = (IOS_W, IOS_RUN)
        self.tv_artifact_head = TV_W

    def job(self, name, conclusion='success'):
        return {'name': name, 'status': 'completed', 'conclusion': conclusion}

    def api(self, path):
        prefix = 'repos/' + control.CANONICAL
        if path == f'{prefix}/compare/{TV_W}...{W}':
            return {'status': 'ahead', 'base_commit': {'sha': TV_W},
                    'merge_base_commit': {'sha': TV_W}, 'behind_by': 0}
        if path == f'{prefix}/compare/{IOS_W}...{W}':
            return {'status': 'ahead', 'base_commit': {'sha': IOS_W},
                    'merge_base_commit': {'sha': IOS_W}, 'behind_by': 0}
        if path == f'{prefix}/actions/runs/{TV_RUN}':
            return {'id': TV_RUN, 'repository': {'id': recovery.REPOSITORY_ID},
                    'head_repository': {'id': recovery.REPOSITORY_ID}, 'head_sha': TV_W,
                    'head_branch': 'main', 'event': 'workflow_dispatch', 'path': '.github/workflows/build.yml',
                    'display_title': 'action:release:pool:private:tag:v3.0.31:repair:' + TV_R,
                    'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        if path == f'{prefix}/actions/runs/{TV_RUN}/attempts/1/jobs?per_page=100':
            return {'total_count': len(self.tv_jobs), 'jobs': copy.deepcopy(self.tv_jobs)}
        if path == f'{prefix}/actions/runs/{IOS_RUN}':
            return {'id': IOS_RUN, 'repository': {'id': recovery.REPOSITORY_ID},
                    'head_repository': {'id': recovery.REPOSITORY_ID}, 'head_sha': IOS_W,
                    'head_branch': 'main', 'event': 'workflow_dispatch', 'path': '.github/workflows/build.yml',
                    'display_title': 'action:release:pool:private:tag:v3.0.31:repair:' + IOS_R,
                    'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        if path == f'{prefix}/actions/runs/{IOS_RUN}/attempts/1/jobs?per_page=100':
            return {'total_count': len(self.ios_jobs), 'jobs': copy.deepcopy(self.ios_jobs)}
        for run_id, source in ((P, S), (A, W)):
            if path == f'{prefix}/actions/runs/{run_id}':
                return {'id': run_id, 'repository': {'id': recovery.REPOSITORY_ID},
                        'head_repository': {'id': recovery.REPOSITORY_ID}, 'head_sha': source,
                        'head_branch': 'main', 'event': 'workflow_dispatch', 'path': '.github/workflows/build.yml',
                        'status': 'completed', 'conclusion': 'failure' if run_id == P else 'success', 'run_attempt': 1}
            if path == f'{prefix}/actions/runs/{run_id}/attempts/1/jobs?per_page=100':
                items = self.original_jobs if run_id == P else self.assembly_jobs
                return {'total_count': len(items), 'jobs': copy.deepcopy(items)}
        if path == f'{prefix}/contents/docs/maintainers/capability-manifest.json?ref={S}':
            payload = json.dumps({'release': {'releaseGate': {'requiredJobs': self.required}}}).encode()
            blob = b'blob ' + str(len(payload)).encode() + b'\0' + payload
            return {'type': 'file', 'encoding': 'base64', 'sha': hashlib.sha1(blob).hexdigest(),
                    'content': base64.b64encode(payload).decode()}
        if '/actions/artifacts/' in path:
            identifier = int(path.rsplit('/', 1)[1])
            item = next(item for item in self.receipt['artifacts'] if item['artifact_id'] == identifier)
            run, head = ((TV_RUN, self.tv_artifact_head) if item['name'] == 'tvos-ipa' else
                         (IOS_RUN, IOS_W) if item['name'] == 'ios-ipa' else (P, S))
            return {'id': identifier, 'name': item['name'], 'digest': item['digest'],
                    'expired': self.bad_artifact, 'workflow_run': {'id': run, 'head_sha': head}}
        raise AssertionError('Unexpected API request: ' + path)

    def verify(self, *, payload=None, compiler=S, graph_error=None):
        payload = json.dumps(self.receipt).encode() if payload is None else payload
        checksum = hashlib.sha256(payload).hexdigest()
        release = {'tag_name': 'v3.0.31', 'assets': [{'name': recovery.RECEIPT, 'digest': 'sha256:' + checksum}]}
        checksums = checksum + '  ' + recovery.RECEIPT + '\n'
        for name, data in self.packages.items():
            digest = hashlib.sha256(data).hexdigest()
            release['assets'].append({'name': name, 'digest': 'sha256:' + digest})
            checksums += digest + '  ' + name + '\n'

        def download(arguments):
            name = arguments[arguments.index('--pattern') + 1]
            target = Path(arguments[arguments.index('--dir') + 1]) / name
            target.write_bytes(payload if name == recovery.RECEIPT else self.packages[name])

        with tempfile.TemporaryDirectory() as directory, patch.object(control, 'run', side_effect=download), \
                patch.object(control, 'api', side_effect=self.api), \
                patch.object(recovery, 'verify_frozen_packaging_repair',
                             side_effect=lambda product, packaging, workflow, api:
                             TV_APPROVAL if packaging == TV_R else IOS_APPROVAL), \
                patch.object(recovery, 'verify_receipt', return_value=APPROVAL, side_effect=graph_error) as graph:
            result = recovery.verify(release, directory, checksums,
                product_source=S, compiler_source=compiler, workflow_source=W,
                producer_run=P, assembly_run=A, linux_sha256=LINUX,
                verify_file=lambda path: self.ios_certificate if path.name in recovery.IOS_PACKAGES
                else self.tv_certificate)
        self.assertEqual(graph.call_args.kwargs['product_source_sha'], S)
        self.assertEqual(graph.call_args.kwargs['workflow_source_sha'], W)
        self.assertEqual(graph.call_args.kwargs['source_run_id'], P)
        self.assertEqual(graph.call_args.kwargs['assembly_run_id'], A)
        return result

    def test_attested_receipt_requires_independent_graph_ci_artifacts_and_signed_builds(self):
        result = self.verify()
        self.assertEqual(result['packaging_source_sha'], R)
        self.assertEqual(result['approval'], APPROVAL)
        self.assertEqual(len(result['receipt_sha256']), 64)

    def test_receipt_equivalence_claim_cannot_override_original_compiler_or_graph(self):
        with self.assertRaisesRegex(ValueError, 'compiler source'):
            self.verify(compiler=W)
        with self.assertRaisesRegex(ValueError, 'unapproved'):
            self.verify(graph_error=ValueError('unapproved repair'))

    def test_wrong_checkout_digest_approval_or_platform_flags_cannot_pass(self):
        original = copy.deepcopy(self.receipt)
        for change in ({'actual_checkout_sha': W}, {'linux_archive_sha256': 'f' * 64},
                       {'packaging_approval': {'self_asserted': True}}, {'rebuild_linux': True},
                       {'include_apple_store_build': False}, {'platforms_complete': False},
                       {'pending_jobs': ['build-ios']}, {'rebuilt_jobs': ['Build Linux (x64)']},
                       {'reused_jobs': []}, {'android_source': {'source_sha': W, 'source_run_id': P}}):
            with self.subTest(change=change):
                self.receipt = {**copy.deepcopy(original), **change}
                with self.assertRaises(ValueError):
                    self.verify()

    def test_original_ci_failure_or_missing_freeze_is_not_retroactively_qualified(self):
        baseline = copy.deepcopy(self.original_jobs)
        for name in ('Release CI / Code Analysis', 'Freeze qualified source', 'Build Linux (x64)'):
            with self.subTest(name=name):
                self.original_jobs = [dict(item, conclusion='failure') if item['name'] == name else item
                                      for item in baseline]
                with self.assertRaises(ValueError):
                    self.verify()

    def test_unsigned_skipped_apple_archive_and_missing_artifacts_fail(self):
        self.assembly_jobs[1]['steps'][0]['conclusion'] = 'skipped'
        with self.assertRaisesRegex(ValueError, 'signed'):
            self.verify()
        self.assembly_jobs[1]['steps'][0]['conclusion'] = 'success'
        self.receipt['artifacts'] = self.receipt['artifacts'][:-1]
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.verify()

    def test_expired_original_artifact_and_duplicate_attested_checksums_fail(self):
        self.bad_artifact = True
        with self.assertRaisesRegex(ValueError, 'artifact metadata'):
            self.verify()
        with self.assertRaises(ValueError):
            recovery.checksum_for(('f' * 64 + '  receipt.json\n') * 2, 'receipt.json')

    def enable_tv_reuse(self):
        self.packages = {name: ('original qualified ' + name).encode() for name in recovery.TV_PACKAGES}
        self.receipt.update(rebuild_tvos=False, rebuilt_jobs=['build-ios'],
                            reused_jobs=sorted(recovery.REUSED | {recovery.TV_JOB}))
        self.assembly_jobs = [item for item in self.assembly_jobs if item['name'] != recovery.TV_JOB]
        self.receipt['tvos_source'] = {
            'source_run_id': TV_RUN, 'source_sha': TV_R, 'workflow_source_sha': TV_W,
            'product_source_sha': S, 'original_source_run_id': P, 'job_id': 700, 'run_attempt': 1,
            'packaging_approval': TV_APPROVAL, 'artifact_id': 777, 'artifact_digest': 'sha256:' + 'f' * 64,
            'platforms_complete': True,
            'package_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in self.packages.items()},
        }
        self.receipt['artifacts'].append({'name': 'tvos-ipa', 'artifact_id': 777, 'digest': 'sha256:' + 'f' * 64,
                                          'source_run_id': TV_RUN, 'source_sha': TV_R})
        self.tv_jobs = [{**self.job('Validate release request'),
                         'steps': [self.job('Check out the approved packaging source')]},
                        {**self.job(recovery.TV_JOB), 'id': 700, 'steps': [self.job(name) for name in (
                            'Build tvOS app', 'Package unsigned tvOS developer IPA', 'Build signed tvOS App Store IPA',
                            'Attest unsigned tvOS developer package', 'Attest signed tvOS App Store package', 'Upload tvOS package')]}]

    def test_reused_tv_binds_separate_approved_source_job_artifact_and_original_certificates(self):
        self.enable_tv_reuse()
        result = self.verify()
        self.assertEqual(result['tvos_source']['source_sha'], TV_R)
        self.assertEqual(result['tvos_source']['workflow_source_sha'], TV_W)
        self.assertNotEqual(TV_R, TV_W)

    def test_tv_reuse_rejects_changed_product_ci_job_approval_and_package_digest(self):
        self.enable_tv_reuse()
        baseline = copy.deepcopy(self.receipt['tvos_source'])
        for change in ({'product_source_sha': R}, {'original_source_run_id': A}, {'job_id': 701},
                       {'packaging_approval': APPROVAL}, {'run_attempt': 2},
                       {'package_sha256': {name: '0' * 64 for name in recovery.TV_PACKAGES}}):
            with self.subTest(change=change):
                self.receipt['tvos_source'] = {**copy.deepcopy(baseline), **change}
                with self.assertRaises(ValueError):
                    self.verify()

    def test_tv_reuse_needs_old_workflow_certificate_and_zip_head_not_checkout_sha(self):
        self.enable_tv_reuse()
        self.tv_certificate = (W, A)
        with self.assertRaisesRegex(ValueError, 'certificate/digest'):
            self.verify()
        self.tv_certificate = (TV_W, TV_RUN)
        self.tv_artifact_head = TV_R
        with self.assertRaisesRegex(ValueError, 'artifact metadata'):
            self.verify()
        self.tv_artifact_head = TV_W
        self.tv_jobs[1]['steps'][-1]['conclusion'] = 'skipped'
        with self.assertRaisesRegex(ValueError, 'Upload tvOS package'):
            self.verify()

    def enable_dual_reuse(self):
        self.enable_tv_reuse()
        self.packages.update({name: ('original qualified ' + name).encode() for name in recovery.IOS_PACKAGES})
        self.receipt.update(rebuild_ios=False, rebuilt_jobs=[], reused_jobs=sorted(recovery.REUSED | recovery.APPLE))
        self.assembly_jobs = [item for item in self.assembly_jobs if item['name'] != recovery.IOS_JOB]
        self.receipt['ios_source'] = {
            'source_run_id': IOS_RUN, 'source_sha': IOS_R, 'workflow_source_sha': IOS_W,
            'product_source_sha': S, 'original_source_run_id': P, 'job_id': 701, 'run_attempt': 1,
            'packaging_approval': IOS_APPROVAL, 'artifact_id': 778, 'artifact_digest': 'sha256:' + 'f' * 64,
            'platforms_complete': True,
            'package_sha256': {name: hashlib.sha256(self.packages[name]).hexdigest()
                               for name in recovery.IOS_PACKAGES},
        }
        self.receipt['artifacts'].append({'name': 'ios-ipa', 'artifact_id': 778,
                                          'digest': 'sha256:' + 'f' * 64,
                                          'source_run_id': IOS_RUN, 'source_sha': IOS_R})
        self.ios_jobs = [
            {**self.job('Validate release request'),
             'steps': [self.job('Check out the approved packaging source')]},
            {**self.job(recovery.IOS_JOB), 'id': 701,
             'steps': [self.job(name) for name in (
                 'Build iOS (no codesign)', 'Create unsigned developer IPA',
                 'Attest unsigned developer IPA', 'Build signed iOS App Store IPA',
                 'Attest signed iOS App Store IPA', 'Upload IPA')]},
            {**self.job(recovery.TV_JOB, 'skipped'), 'steps': []},
            {**self.job('create-release', 'failure'),
             'steps': [self.job('Recover verified platform artifacts', 'failure'),
                       self.job('Create Release', 'skipped')]},
        ]

    def test_dual_signed_siblings_allow_assembly_only_with_exact_certificates(self):
        self.enable_dual_reuse()
        result = self.verify()
        self.assertEqual(result['ios_source']['source_run_id'], IOS_RUN)
        self.assertEqual(result['tvos_source']['source_run_id'], TV_RUN)
        self.ios_certificate = (W, A)
        with self.assertRaisesRegex(ValueError, 'iOS package certificate'):
            self.verify()
        self.ios_certificate = (IOS_W, IOS_RUN)
        self.ios_jobs[-1]['steps'][-1]['conclusion'] = 'success'
        with self.assertRaisesRegex(ValueError, 'attempted publication'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
