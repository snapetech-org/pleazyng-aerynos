import hashlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import stage_release


class PublishedStagingTests(unittest.TestCase):
    def test_cli_passes_the_trusted_tooling_checkout_to_canonical_verifier(self):
        payload = b'attested core Linux bytes'
        seen = []
        tooling = Path('trusted-tooling')

        def verify(command, **kwargs):
            bundle = Path(command[command.index('--bundle') + 1])
            self.assertEqual(bundle.read_bytes(), payload)
            self.assertEqual(command[command.index('--tooling') + 1], str(tooling))
            self.assertEqual(command[command.index('--source-sha') + 1], 'a' * 40)
            self.assertEqual(command[command.index('--producer-run-id') + 1], '42')
            self.assertEqual(command[1], str(tooling / 'scripts/aerynos/from_core.py'))
            self.assertNotIn('--source', command)
            self.assertTrue(kwargs['check'])
            seen.append(bundle)

        with patch.object(stage_release.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as download, \
                patch.object(stage_release.subprocess, 'run', side_effect=verify):
            stage_release.main([
                '--tooling', str(tooling), '--tag', 'v3.0.31', '--sha256', hashlib.sha256(payload).hexdigest(),
                '--source-sha', 'a' * 40, '--producer-run-id', '42', '--output', 'candidate',
            ])
        download.assert_called_once_with(
            'https://github.com/snapetech-org/pleazyng/releases/download/v3.0.31/pleazyng-linux-x64.tar.gz', timeout=120)
        self.assertFalse(seen[0].exists())

    def test_workflow_uses_only_the_separate_trusted_main_tooling_checkout(self):
        workflow = Path(__file__).resolve().parents[1] / '.github/workflows/sync.yml'
        text = workflow.read_text(encoding='utf-8')
        self.assertIn('repository: snapetech-org/pleazyng\n'
                      '          ref: 7e73a69b61e5b090891ea4ae14d3f0c6b9b46fb1\n'
                      '          path: tooling', text)
        self.assertIn('python3 scripts/stage_release.py --tooling tooling \\', text)
        self.assertNotIn('path: source', text)
        self.assertNotIn('--source source', text)

    def test_changed_public_bytes_never_reach_staging(self):
        with patch.object(stage_release.urllib.request, 'urlopen', return_value=io.BytesIO(b'changed')), \
                patch.object(stage_release.subprocess, 'run') as verifier:
            with self.assertRaisesRegex(ValueError, 'attested digest'):
                stage_release.stage(Path('trusted-tooling'), 'v3.0.31', 'c' * 64, 'a' * 40, 42, Path('candidate'))
        verifier.assert_not_called()

    def test_invalid_identity_cannot_change_the_download_location(self):
        with patch.object(stage_release.urllib.request, 'urlopen') as download:
            for tag, digest, source_sha, producer in [('main', 'c' * 64, 'a' * 40, 42),
                                                       ('v3.0.31/../other', 'c' * 64, 'a' * 40, 42),
                                                       ('v3.0.31', 'bad', 'a' * 40, 42),
                                                       ('v3.0.31', 'c' * 64, 'main', 42),
                                                       ('v3.0.31', 'c' * 64, 'a' * 40, 0)]:
                with self.subTest(tag=tag, source_sha=source_sha, producer=producer), self.assertRaises(ValueError):
                    stage_release.stage(Path('trusted-tooling'), tag, digest, source_sha, producer, Path('candidate'))
        download.assert_not_called()
