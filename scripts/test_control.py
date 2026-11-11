import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import control


class OwnerHoldTests(unittest.TestCase):
    def test_promotion_stops_before_asset_or_api_work_during_owner_hold(self):
        with patch.object(control, 'refresh'), patch.object(control, 'read', return_value={'active': True}), \
                patch.object(control, 'validated') as validated, patch.object(control, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'Owner hold'):
                control.publish(Path('unconsumed-handoff'))
            validated.assert_not_called()
            api.assert_not_called()

    def test_hold_raised_during_upload_prevents_release_visibility_and_feed_commit(self):
        state = {'held': False, 'uploaded': []}
        q = {'source_sha': 'a' * 40, 'tag': 'main-aaaaaaaaaaaa-r2', 'version': '3.0.29+188', 'release': 2}
        assets = {'core-release.json': 'b' * 64, 'package.stone': 'c' * 64}
        def read(path, default=None):
            if str(path) == 'sync-state/promotion-hold.json':
                return {'active': state['held']}
            if Path(path).name == 'core-release.json':
                return {'source_sha': q['source_sha']}
            return {}
        def api(path):
            if path.endswith('/commits/main'):
                return {'sha': q['source_sha']}
            return {'draft': True, 'assets': [{'name': name, 'digest': 'sha256:' + assets[name]}
                                             for name in state['uploaded']]}
        def upload(arguments):
            if arguments[:3] == ['gh', 'release', 'upload']:
                state['uploaded'].append(Path(arguments[4]).name)
                state['held'] = True
        with tempfile.TemporaryDirectory() as directory, patch.object(control, 'refresh') as refresh, \
                patch.object(control, 'validated', return_value=(q, assets)), patch.object(control, 'read', side_effect=read), \
                patch.object(control, 'api', side_effect=api), patch.object(control, 'promotion_allowed', return_value=True), \
                patch.object(control.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch.object(control, 'run', side_effect=upload) as run, patch.object(control, 'commit') as commit:
            with self.assertRaisesRegex(RuntimeError, 'hold raised during upload'):
                control.publish(directory)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(len(state['uploaded']), 2)
        self.assertFalse(any(call.args[0][:3] == ['gh', 'release', 'edit'] for call in run.call_args_list))
        commit.assert_not_called()

    def test_supported_hold_preserves_gate_configuration(self):
        with patch.object(control, 'refresh'), patch.object(control, 'read', return_value={'native_qa': 'required'}), \
                patch.object(control, 'write') as write, patch.object(control, 'commit'):
            control.set_hold(True, 'maintainer maintenance')
        value = write.call_args.args[1]
        self.assertTrue(value['active'])
        self.assertEqual(value['native_qa'], 'required')



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

    def test_server_playback_alone_cannot_qualify_the_direct_open_media_route(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            q = self.handoff(root)
            q['checks'].remove('open_media_direct_h264')
            (root / 'qualification.json').write_text(json.dumps(q))
            with self.assertRaisesRegex(RuntimeError, 'Required native qualification is incomplete'):
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
