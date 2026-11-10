#!/usr/bin/env python3
"""Stage the published Linux bytes without a cross-repository Actions token."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request


def stage(source, tooling, tag, checksum, source_sha, producer, output):
    if (not re.fullmatch(r'v\d+\.\d+\.\d+', tag)
            or not re.fullmatch('[0-9a-f]{64}', checksum)
            or not re.fullmatch('[0-9a-f]{40}', source_sha) or producer < 1):
        raise ValueError('Exact core version, digest, source and producer are required')
    url = f'https://github.com/snapetech-org/pleazyng/releases/download/{tag}/pleazyng-linux-x64.tar.gz'
    with tempfile.TemporaryDirectory(prefix='aerynos-published-linux-') as temporary:
        bundle = Path(temporary) / 'pleazyng-linux-x64.tar.gz'
        digest = hashlib.sha256()
        # Public release downloads need no credential for either repository.
        with urllib.request.urlopen(url, timeout=120) as response, bundle.open('wb') as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != checksum:
            raise ValueError('Downloaded Linux release differs from the resolved attested digest')
        # The canonical verifier still checks the original successful Linux job,
        # retained artifact identity and certificate's source/run claims.
        subprocess.run([sys.executable, str(tooling / 'scripts/aerynos/from_core.py'),
                        '--source', str(source), '--source-sha', source_sha,
                        '--producer-run-id', str(producer), '--bundle', str(bundle),
                        '--output', str(output)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--tooling', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--producer-run-id', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    stage(args.source, args.tooling, args.tag, args.sha256, args.source_sha, args.producer_run_id, args.output)
