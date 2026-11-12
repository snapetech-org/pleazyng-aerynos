#!/usr/bin/env python3
"""Verify anonymous public Moss installation after native-qualified publication."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
import time
import urllib.request

CHANNEL = 'snapetech-org/pleazyng-aerynos'
LIVE_FEED = f'https://github.com/{CHANNEL}/raw/refs/heads/main/feed/stone.index'
MANIFEST = f'https://raw.githubusercontent.com/{CHANNEL}/main/sync-state/last-good.json'


def validate_receipt(receipt, expected_source):
    if (not re.fullmatch('[0-9a-f]{40}', expected_source)
            or receipt.get('source_sha') != expected_source or receipt.get('status') != 'qualified'
            or type(receipt.get('release')) is not int or receipt['release'] < 2
            or not re.fullmatch(r'\d+\.\d+\.\d+\+\d+', receipt.get('version', ''))):
        raise ValueError('Public receipt does not match the qualified source/version')
    tag = f'main-{expected_source[:12]}-r{receipt["release"]}'
    if receipt.get('tag') != tag:
        raise ValueError('Public package identity differs from its qualification')
    for name in ('feed.index', 'stone.index'):
        if not re.fullmatch('[0-9a-f]{64}', receipt.get('assets', {}).get(name, '')):
            raise ValueError('Public index checksum is missing')
    return f'https://github.com/{CHANNEL}/releases/download/{tag}/stone.index'


def fetch(url):
    with urllib.request.urlopen(url, timeout=45) as response:
        return response.read()


def installed_version(output, receipt):
    rows = [line.split() for line in output.splitlines() if line.startswith('pleazyng ')]
    expected = receipt['version'].split('+')[0] + '-' + str(receipt['release'])
    if len(rows) != 1 or len(rows[0]) < 2 or rows[0][1] != expected:
        raise RuntimeError('Public Moss installation did not activate ' + expected)


def verify(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Raw GitHub metadata can briefly lag its publishing commit.
    for attempt in range(6):
        try:
            receipt = json.loads(fetch(MANIFEST + '?delivery=' + args.source_sha + '&attempt=' + str(attempt)))
            pinned = validate_receipt(receipt, args.source_sha)
            break
        except (ValueError, OSError):
            if attempt == 5:
                raise
            time.sleep(5)
    (output / 'qualified-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    for name, url, expected in [('live.index', LIVE_FEED, receipt['assets']['feed.index']),
                                ('pinned.index', pinned, receipt['assets']['stone.index'])]:
        content = fetch(url)
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError('Public index differs from qualified bytes: ' + name)
        (output / name).write_bytes(content)
    sys.path.insert(0, str(args.tooling.resolve() / 'scripts/aerynos'))
    from vm import Guest
    from qualify import AppQA, command
    guest = Guest(args.scratch / 'vm')
    app = None
    try:
        guest.prepare(args.iso)
        guest.start()
        command(guest, 'moss repo add pleazyng-public ' + shlex.quote(LIVE_FEED)
                + '; moss -y pkg add pleazyng', output / 'live-install.log')
        installed_version(command(guest, 'moss pkg list --installed', output / 'live-installed.txt').decode(), receipt)
        command(guest, 'moss repo remove pleazyng-public; moss -y pkg remove pleazyng; '
                'moss repo add pleazyng-pinned ' + shlex.quote(pinned)
                + '; moss -y pkg add pleazyng', output / 'pinned-reinstall.log')
        installed_version(command(guest, 'moss pkg list --installed', output / 'pinned-installed.txt').decode(), receipt)
        app = AppQA(guest, output)
        app.login_desktop()
        app.launch()
        app.wait('Connect to Jellyfin')
        guest.screenshot(output / 'public-installed-launch.png')
        runtime = command(guest, 'journalctl _COMM=pleazyng --no-pager', output / 'runtime.log').decode()
        expected = 'Pleazyng v' + receipt['version'] + ' (' + args.source_sha[:7] + ')'
        if expected not in runtime:
            raise RuntimeError('Installed app did not identify the qualified source/version')
        app.stop()
        (output / 'public-install.json').write_text(json.dumps({
            'status': 'passed', 'source_sha': args.source_sha, 'version': receipt['version'],
            'release': receipt['release'], 'tag': receipt['tag'],
            'checks': ['anonymous_live_index_digest', 'anonymous_pinned_index_digest',
                       'native_live_moss_install', 'native_pinned_moss_reinstall',
                       'ordinary_launch', 'runtime_source_version']}, indent=2) + '\n')
    finally:
        try:
            if guest.process and guest.process.poll() is None:
                guest.screenshot(output / 'final.png')
                command(guest, 'journalctl _COMM=pleazyng --no-pager', output / 'runtime.log')
        finally:
            guest.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--tooling', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--iso', type=Path, required=True)
    verify(parser.parse_args())
