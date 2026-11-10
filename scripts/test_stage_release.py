import hashlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import stage_release


class PublishedStagingTests(unittest.TestCase):
    def test_public_bytes_are_digest_checked_before_canonical_producer_verification(self):
        payload = b'attested core Linux bytes'
        seen = []

        def verify(command, **kwargs):
            bundle = Path(command[command.index('--bundle') + 1])
            self.assertEqual(bundle.read_bytes(), payload)
            self.assertEqual(command[command.index('--source-sha') + 1], 'a' * 40)
            self.assertEqual(command[command.index('--producer-run-id') + 1], '42')
            self.assertIn('tooling/scripts/aerynos/from_core.py', command)
            self.assertTrue(kwargs['check'])
            seen.append(bundle)

        with patch.object(stage_release.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as download, \
                patch.object(stage_release.subprocess, 'run', side_effect=verify):
            stage_release.stage(Path('source'), Path('tooling'), 'v3.0.31', hashlib.sha256(payload).hexdigest(),
                                'a' * 40, 42, Path('candidate'))
        download.assert_called_once_with(
            'https://github.com/snapetech-org/pleazyng/releases/download/v3.0.31/pleazyng-linux-x64.tar.gz', timeout=120)
        self.assertFalse(seen[0].exists())

    def test_changed_public_bytes_never_reach_staging(self):
        with patch.object(stage_release.urllib.request, 'urlopen', return_value=io.BytesIO(b'changed')), \
                patch.object(stage_release.subprocess, 'run') as verifier:
            with self.assertRaisesRegex(ValueError, 'attested digest'):
                stage_release.stage(Path('source'), Path('tooling'), 'v3.0.31', 'c' * 64, 'a' * 40, 42, Path('candidate'))
        verifier.assert_not_called()

    def test_invalid_identity_cannot_change_the_download_location(self):
        with patch.object(stage_release.urllib.request, 'urlopen') as download:
            for tag, digest, source, producer in [('main', 'c' * 64, 'a' * 40, 42),
                                                   ('v3.0.31/../other', 'c' * 64, 'a' * 40, 42),
                                                   ('v3.0.31', 'bad', 'a' * 40, 42),
                                                   ('v3.0.31', 'c' * 64, 'main', 42),
                                                   ('v3.0.31', 'c' * 64, 'a' * 40, 0)]:
                with self.subTest(tag=tag, source=source, producer=producer), self.assertRaises(ValueError):
                    stage_release.stage(Path('source'), Path('tooling'), tag, digest, source, producer, Path('candidate'))
        download.assert_not_called()
