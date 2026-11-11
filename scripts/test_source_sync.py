import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import control
import source_sync

A, B, C = ('a' * 40, 'b' * 40, 'c' * 40)


class API:
    def __init__(self, previous=A, latest=B):
        self.previous, self.latest = previous, latest
        self.calls = []
        self.conflicts = 0
        self.before_write = None
        self.identities = {control.CANONICAL: source_sync.CANONICAL_ID,
                           control.CHANNEL: source_sync.CHANNEL_ID}

    def document(self):
        data = json.dumps({'source_sha': self.previous}).encode()
        return {'type': 'file', 'encoding': 'base64',
                'sha': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest(),
                'content': base64.b64encode(data).decode()}

    def __call__(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        for repository, identifier in self.identities.items():
            if path == f'repos/{repository}':
                return {'id': identifier, 'full_name': repository, 'default_branch': 'main'}
        if path.endswith('/git/ref/heads/main'):
            if self.before_write is not None and len([p for p, _ in self.calls if p.endswith('/git/ref/heads/main')]) == 2:
                self.latest = self.before_write
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.latest}}
        if '/compare/' in path:
            old, new = path.rsplit('/', 1)[1].split('...')
            order = [A, B, C]
            return {'status': 'ahead' if old in order and new in order and order.index(old) < order.index(new) else 'diverged',
                    'base_commit': {'sha': old}, 'merge_base_commit': {'sha': old}, 'behind_by': 0}
        if path == f'repos/{control.CHANNEL}/contents/{source_sync.TRACKING_PATH}?ref=main':
            return self.document()
        if path == f'repos/{control.CHANNEL}/contents/{source_sync.TRACKING_PATH}' and body is not None:
            if self.conflicts:
                self.conflicts -= 1
                raise source_sync.Conflict('concurrent write')
            if body['sha'] != self.document()['sha']:
                raise AssertionError('update omitted compare-and-swap')
            self.previous = json.loads(base64.b64decode(body['content']))['source_sha']
            return {'commit': {'sha': 'd' * 40}}
        raise AssertionError(path)


class SourceSyncTests(unittest.TestCase):
    def test_updates_only_source_pointer_and_cannot_claim_qualification(self):
        api = API()
        # Dispatch payload and commit input are deliberately not consumed.
        with patch.dict(os.environ, {'GITHUB_SHA': C, 'GITHUB_EVENT_PATH': '/does-not-select-source'}):
            result = source_sync.sync(api)
        self.assertEqual(result, {'source_sha': B, 'changed': True})
        writes = [(p, b) for p, b in api.calls if b is not None]
        self.assertEqual(len(writes), 1)
        path, body = writes[0]
        self.assertEqual(path, f'repos/{control.CHANNEL}/contents/{control.SOURCE_TRACKING_PATH}')
        value = json.loads(base64.b64decode(body['content']))
        self.assertEqual(set(value), {'source_sha', 'source_url', 'observed_at', 'purpose'})
        self.assertEqual(value['purpose'], 'source tracking; not package qualification')
        self.assertEqual(body['branch'], 'main')

    def test_identical_source_does_not_write(self):
        api = API(previous=B)
        self.assertFalse(source_sync.sync(api)['changed'])
        self.assertFalse(any(body is not None for _, body in api.calls))

    def test_queued_event_fetches_newest_main_before_writing(self):
        api = API()
        api.before_write = C
        self.assertEqual(source_sync.sync(api)['source_sha'], C)
        self.assertEqual(api.previous, C)
        self.assertEqual(len([b for _, b in api.calls if b is not None]), 1)

    def test_conflict_is_bounded_and_retries_from_current_blob(self):
        api = API()
        api.conflicts = 1
        self.assertTrue(source_sync.sync(api)['changed'])
        api = API()
        api.conflicts = 99
        with self.assertRaisesRegex(RuntimeError, 'bounded'):
            source_sync.sync(api)
        self.assertEqual(len([b for _, b in api.calls if b is not None]), 3)
        self.assertEqual(api.previous, A)

    def test_wrong_repository_or_divergent_pointer_fails_without_writes(self):
        for api in (API(previous='f' * 40), API(previous=C, latest=B)):
            with self.subTest(previous=api.previous), self.assertRaises(ValueError):
                source_sync.sync(api)
            self.assertFalse(any(body is not None for _, body in api.calls))
        api = API()
        api.identities[control.CHANNEL] = 99
        with self.assertRaisesRegex(ValueError, 'identity'):
            source_sync.sync(api)

    def test_tampered_pointer_digest_is_rejected(self):
        api = API()
        document = api.document()
        document['sha'] = 'f' * 40
        api.document = lambda: document
        with self.assertRaisesRegex(ValueError, 'digest'):
            source_sync.sync(api)
        self.assertFalse(any(body is not None for _, body in api.calls))

    def test_cli_rejects_an_untrusted_checkout_before_network(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': control.CHANNEL,
                                     'GITHUB_REF': 'refs/heads/untrusted'}), \
                patch('sys.argv', ['source_sync.py']), patch.object(source_sync, 'sync') as sync:
            with self.assertRaisesRegex(RuntimeError, 'trusted'):
                source_sync.main()
            sync.assert_not_called()

    def test_only_write_conflicts_are_retryable_and_credentials_are_not_reported(self):
        for code, expected in (('409', source_sync.Conflict), ('403', RuntimeError)):
            error = subprocess.CalledProcessError(1, ['gh'], stderr=f'private-response (HTTP {code})')
            with patch.object(control, 'run', side_effect=error), self.assertRaises(expected) as caught:
                source_sync.request('fixed-path', {'fixed': 'value'})
            self.assertNotIn('private-response', str(caught.exception))
        with patch.object(control, 'run', side_effect=subprocess.CalledProcessError(1, ['gh'], stderr='HTTP 409')):
            with self.assertRaises(RuntimeError) as caught:
                source_sync.request('fixed-path')
            self.assertNotIsInstance(caught.exception, source_sync.Conflict)

    def test_receiver_is_main_only_and_has_no_package_execution(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/source-sync.yml').read_text()
        self.assertIn('types: [canonical-main-updated]', workflow)
        self.assertIn("github.repository == 'snapetech-org/pleazyng-aerynos' && github.ref == 'refs/heads/main'", workflow)
        self.assertIn('ref: main\n          persist-credentials: false', workflow)
        commands = [line.strip() for line in workflow.splitlines() if line.strip().startswith('run:')]
        self.assertEqual(commands, ['run: python3 scripts/source_sync.py --receipt "$RUNNER_TEMP/source-sync.json"'])
        self.assertNotIn('client_payload', workflow)

    def test_delivery_refreshes_after_remote_pointer_update(self):
        import releases
        with patch.object(source_sync, 'sync', return_value={'source_sha': B}), \
                patch.object(control, 'refresh') as refresh:
            self.assertEqual(releases.mirror_main(), B)
            refresh.assert_called_once()


class PublicationRaceTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_source_only_race_is_rebased_but_feed_and_hold_races_are_preserved(self):
        cases = [(control.SOURCE_TRACKING_PATH, False, False), ('feed/stone.index', False, False),
                 ('sync-state/promotion-hold.json', False, False), ('sync-state/promotion-hold.json', True, False),
                 ('sync-state/promotion-hold.json', False, True), ('feed/stone.index', False, True)]
        for remote_path, roundtrip, merge_resolution in cases:
            with self.subTest(remote_path=remote_path, roundtrip=roundtrip, merge_resolution=merge_resolution), tempfile.TemporaryDirectory(prefix='aerynos-source-race-') as directory:
                root = Path(directory)
                bare, publisher, writer = root / 'remote.git', root / 'publisher', root / 'writer'
                self.git(root, 'init', '--bare', '--initial-branch=main', str(bare))
                self.git(root, 'clone', str(bare), str(publisher))
                for key, value in (('user.name', 'QA'), ('user.email', 'qa@example.invalid'), ('commit.gpgsign', 'false')):
                    self.git(publisher, 'config', key, value)
                for path, value in ((control.SOURCE_TRACKING_PATH, A), ('feed/stone.index', 'seed'),
                                    ('qualification.json', 'seed-qualified'), ('sync-state/promotion-hold.json', 'false')):
                    target = publisher / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(value)
                self.git(publisher, 'add', '.')
                self.git(publisher, 'commit', '-m', 'seed')
                self.git(publisher, 'push', 'origin', 'main')
                self.git(root, 'clone', str(bare), str(writer))
                for key, value in (('user.name', 'QA'), ('user.email', 'qa@example.invalid'), ('commit.gpgsign', 'false')):
                    self.git(writer, 'config', key, value)
                (publisher / 'feed/stone.index').write_text('next-qualified')
                (publisher / 'qualification.json').write_text('next-qualified-proof')
                original, pushed = control.run, []
                def run(args, **kwargs):
                    if 'push' in args:
                        pushed.append(args)
                        if len(pushed) == 1:
                            if merge_resolution:
                                self.git(writer, 'branch', 'side')
                                (writer / control.SOURCE_TRACKING_PATH).write_text(B)
                                self.git(writer, 'add', '--', control.SOURCE_TRACKING_PATH)
                                self.git(writer, 'commit', '-m', 'main tracks source')
                                self.git(writer, 'checkout', 'side')
                                (writer / control.SOURCE_TRACKING_PATH).write_text(C)
                                self.git(writer, 'add', '--', control.SOURCE_TRACKING_PATH)
                                self.git(writer, 'commit', '-m', 'side tracks source')
                                conflict = subprocess.run(['git', '-C', str(writer), 'merge', '--no-ff', 'main'],
                                                          capture_output=True, text=True)
                                self.assertEqual(conflict.returncode, 1)
                                # Both ordinary commits touch only source tracking;
                                # the merge resolution introduces the protected change.
                                (writer / control.SOURCE_TRACKING_PATH).write_text(B)
                                (writer / remote_path).write_text('remote-owner-change')
                                self.git(writer, 'add', '--', control.SOURCE_TRACKING_PATH, remote_path)
                                self.git(writer, 'commit', '-m', 'resolve source and change protected state')
                                self.git(writer, 'push', 'origin', 'HEAD:main')
                                return original(args, **kwargs)
                            (writer / remote_path).write_text(B if remote_path == control.SOURCE_TRACKING_PATH else 'remote-owner-change')
                            self.git(writer, 'add', '--', remote_path)
                            self.git(writer, 'commit', '-m', 'concurrent update')
                            self.git(writer, 'push', 'origin', 'main')
                            if roundtrip:
                                (writer / remote_path).write_text('false')
                                (writer / control.SOURCE_TRACKING_PATH).write_text(B)
                                self.git(writer, 'add', '--', remote_path, control.SOURCE_TRACKING_PATH)
                                self.git(writer, 'commit', '-m', 'restore hold and track source')
                                self.git(writer, 'push', 'origin', 'main')
                    return original(args, **kwargs)
                before = Path.cwd()
                try:
                    os.chdir(publisher)
                    with patch.object(control, 'run', side_effect=run):
                        if remote_path == control.SOURCE_TRACKING_PATH:
                            control.commit(['feed/stone.index', 'qualification.json'], 'qualified promotion')
                        else:
                            with self.assertRaisesRegex(RuntimeError, 'Concurrent channel'):
                                control.commit(['feed/stone.index', 'qualification.json'], 'qualified promotion')
                finally:
                    os.chdir(before)
                self.assertFalse(any(value == '-f' or value.startswith('--force') for args in pushed for value in args))
                if remote_path == control.SOURCE_TRACKING_PATH:
                    self.assertEqual(self.git(bare, 'show', 'main:feed/stone.index'), 'next-qualified')
                    self.assertEqual(self.git(bare, 'show', 'main:qualification.json'), 'next-qualified-proof')
                    self.assertEqual(self.git(bare, 'show', 'main:' + control.SOURCE_TRACKING_PATH), B)
                else:
                    self.assertEqual(self.git(bare, 'show', 'main:' + remote_path), 'false' if roundtrip else 'remote-owner-change')
                    self.assertEqual(self.git(publisher, 'show', 'HEAD:qualification.json'), 'next-qualified-proof')


if __name__ == '__main__':
    unittest.main()
