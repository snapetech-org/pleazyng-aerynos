import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import control


class OwnerHoldTests(unittest.TestCase):
    def test_reconcile_does_not_reenable_or_dispatch_during_owner_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            with patch.dict(control.os.environ, {'GITHUB_OUTPUT': str(output)}), \
                    patch.object(control, 'refresh'), patch.object(control, 'read', return_value={'active': True}), \
                    patch.object(control, 'run') as run, patch.object(control, 'api') as api:
                control.reconcile()
            self.assertIn('build=false', output.read_text())
            self.assertIn('reason=owner-hold', output.read_text())
            run.assert_not_called()
            api.assert_not_called()

    def test_promotion_stops_before_asset_or_api_work_during_owner_hold(self):
        with patch.object(control, 'refresh'), patch.object(control, 'read', return_value={'active': True}), \
                patch.object(control, 'validated') as validated, patch.object(control, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'Owner hold'):
                control.publish(Path('unconsumed-handoff'))
            validated.assert_not_called()
            api.assert_not_called()


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.time = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
        self.sha = 'a' * 40

    def test_current_source_does_not_rebuild(self):
        result = control.plan(self.sha, {'source_sha': self.sha, 'release': 7}, {}, 8, self.time)
        self.assertFalse(result['build'])

    def test_same_version_new_sha_gets_greater_package_release(self):
        good = {'source_sha': 'b' * 40, 'version': '3.0.22+181', 'release': 12}
        result = control.plan(self.sha, good, {}, 2, self.time)
        self.assertTrue(result['build'])
        self.assertEqual(result['release'], 13)

    def test_failed_source_backs_off_but_new_source_does_not(self):
        status = {'source_sha': self.sha, 'phase': 'failed', 'retry_after': (self.time + dt.timedelta(minutes=20)).isoformat()}
        self.assertFalse(control.plan(self.sha, {}, status, 1, self.time)['build'])
        self.assertTrue(control.plan('c' * 40, {}, status, 2, self.time)['build'])
        self.assertTrue(control.plan(self.sha, {}, status, 3, self.time + dt.timedelta(minutes=30))['build'])
        self.assertTrue(control.plan(self.sha, {}, status, 3, self.time, retry=True)['build'])
        self.assertFalse(control.plan(self.sha, {'source_sha': self.sha}, status, 3, self.time, retry=True)['build'])

    def test_rejects_ambiguous_or_injected_source_names(self):
        for value in ('main', 'a' * 12, '../main', 'a' * 40 + '\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.plan(value, {}, {}, 1, self.time)


class PromotionTests(unittest.TestCase):
    def relation(self, old, new):
        chain = ['a' * 40, 'b' * 40, 'c' * 40]
        return old in chain and new in chain and chain.index(old) <= chain.index(new)

    def test_same_app_version_advances_release_and_source(self):
        candidate = {'source_sha': 'b' * 40, 'release': 3, 'version': '3.0.22+181'}
        good = {'source_sha': 'a' * 40, 'release': 2, 'version': '3.0.22+181'}
        self.assertTrue(control.promotion_allowed(candidate, good, 'c' * 40, self.relation))

    def test_older_build_cannot_replace_newer_feed_even_with_larger_release(self):
        with self.assertRaises(RuntimeError):
            control.promotion_allowed({'source_sha': 'a' * 40, 'release': 99},
                                      {'source_sha': 'b' * 40, 'release': 3}, 'c' * 40, self.relation)

    def test_divergent_or_abandoned_source_cannot_publish(self):
        with self.assertRaises(RuntimeError):
            control.promotion_allowed({'source_sha': 'd' * 40, 'release': 4},
                                      {'source_sha': 'b' * 40, 'release': 3}, 'c' * 40, self.relation)

    def test_duplicate_is_a_noop_and_package_identity_cannot_repeat(self):
        self.assertFalse(control.promotion_allowed({'source_sha': 'b' * 40, 'release': 3},
                                                  {'source_sha': 'b' * 40, 'release': 3}, 'c' * 40, self.relation))
        with self.assertRaises(RuntimeError):
            control.promotion_allowed({'source_sha': 'c' * 40, 'release': 3},
                                      {'source_sha': 'b' * 40, 'release': 3}, 'c' * 40, self.relation)

    def test_first_promotion_requires_reviewed_floor_and_exact_seed_upgrade(self):
        seed = {'source_sha': None, 'legacy_source_sha': 'd' * 40,
                'tag': 'seed', 'release': 1, 'stone_sha256': 'e' * 64}
        bootstrap = {'canonical_floor_sha': 'b' * 40, 'seed': seed}
        q = {'source_sha': 'b' * 40, 'release': 2, 'baseline': dict(seed)}
        self.assertTrue(control.promotion_allowed(q, seed, 'c' * 40, self.relation, bootstrap))
        for change in ({'source_sha': 'a' * 40}, {'baseline': {}},
                       {'baseline': {**seed, 'stone_sha256': 'f' * 64}}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                control.promotion_allowed({**q, **change}, seed, 'c' * 40, self.relation, bootstrap)
        with self.assertRaises(RuntimeError):
            control.promotion_allowed(q, seed, 'c' * 40, self.relation)


class HandoffTests(unittest.TestCase):
    def handoff(self, root):
        stone = 'pleazyng-3.0.22-2-1-x86_64.stone'
        (root / stone).write_bytes(b'bounded integrity fixture')
        for name in ('stone.index', 'feed.index', 'recipe.tar.gz', 'pleazyng-linux-x64.tar.gz', 'pleazyng-source.tar.gz'):
            (root / name).write_bytes(name.encode())
        q = {'status': 'qualified', 'source_sha': 'a' * 40, 'version': '3.0.22+181', 'release': 2,
             'tag': 'main-' + 'a' * 12 + '-r2', 'checks': sorted(control.REQUIRED),
             'stone': stone, 'stone_sha256': hashlib.sha256((root / stone).read_bytes()).hexdigest()}
        (root / 'qualification.json').write_text(json.dumps(q))
        files = sorted(root.iterdir())
        (root / 'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n' for p in files))
        return q

    def test_complete_qualified_bytes_validate_and_checksum_file_is_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.handoff(root)
            _, assets = control.validated(root)
            self.assertIn('SHA256SUMS.txt', assets)
            self.assertIn('feed.index', assets)

    def test_partial_qualification_never_reaches_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            q = self.handoff(root)
            q['checks'].remove('bundled_im_cache')
            (root / 'qualification.json').write_text(json.dumps(q))
            with self.assertRaises(RuntimeError):
                control.validated(root)

    def test_false_gate_mapping_is_not_accepted_as_passed_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            q = self.handoff(root)
            q['checks'] = {check: False for check in control.REQUIRED}
            (root / 'qualification.json').write_text(json.dumps(q))
            with self.assertRaises(RuntimeError):
                control.validated(root)

    def test_modified_native_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            q = self.handoff(root)
            (root / q['stone']).write_bytes(b'different bytes')
            with self.assertRaises(RuntimeError):
                control.validated(root)

    def test_manifest_cannot_escape_artifact_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.handoff(root)
            with (root / 'SHA256SUMS.txt').open('a') as f:
                f.write('a' * 64 + '  ../outside\n')
            with self.assertRaises(ValueError):
                control.validated(root)


if __name__ == '__main__':
    unittest.main()
