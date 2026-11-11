"""Track authoritative canonical main without building or qualifying a package."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import control

CANONICAL_ID = 1433487464
CHANNEL_ID = 1452033914
TRACKING_PATH = control.SOURCE_TRACKING_PATH


class Conflict(RuntimeError):
    pass


def request(path, payload=None):
    args = ['gh', 'api', path]
    options = {}
    if payload is not None:
        args.extend(['--method', 'PUT', '--input', '-'])
        options['input'] = json.dumps(payload)
    try:
        return json.loads(control.run(args, **options).stdout)
    except subprocess.CalledProcessError as error:
        status = re.search(r'HTTP ([0-9]{3})', error.stderr or '')
        code = status.group(1) if status else 'unavailable'
        if payload is not None and code == '409':
            raise Conflict('Source pointer changed concurrently') from None
        raise RuntimeError(f'Source tracking request failed (HTTP {code})') from None


def sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) is not None


def head(api):
    ref = api(f'repos/{control.CANONICAL}/git/ref/heads/main')
    obj = ref.get('object', {})
    if ref.get('ref') != 'refs/heads/main' or obj.get('type') != 'commit' or not sha(obj.get('sha')):
        raise ValueError('Canonical main must resolve to an immutable commit')
    return obj['sha']


def sync(api=None):
    api = api or request
    for repository, identifier in ((control.CANONICAL, CANONICAL_ID), (control.CHANNEL, CHANNEL_ID)):
        identity = api(f'repos/{repository}')
        if (identity.get('id') != identifier or identity.get('full_name', '').lower() != repository
                or identity.get('default_branch') != 'main'):
            raise ValueError('Source tracking repository identity does not match')
    for _ in range(3):
        latest = head(api)
        path = f'repos/{control.CHANNEL}/contents/{TRACKING_PATH}'
        document = api(path + '?ref=main')
        if document.get('type') != 'file' or document.get('encoding') != 'base64' or not sha(document.get('sha')):
            raise ValueError('Source pointer must be a regular immutable JSON file')
        encoded = document.get('content', '')
        if not isinstance(encoded, str) or len(encoded) > 128 * 1024:
            raise ValueError('Source pointer exceeds its size limit')
        data = base64.b64decode(''.join(encoded.split()), validate=True)
        if (len(data) > 64 * 1024
                or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != document['sha']):
            raise ValueError('Source pointer blob digest differs')
        previous = json.loads(data)
        older = previous.get('source_sha')
        if not sha(older):
            raise ValueError('Existing source pointer is invalid')
        if older != latest:
            compare = api(f'repos/{control.CANONICAL}/compare/{older}...{latest}')
            if (compare.get('status') != 'ahead' or compare.get('base_commit', {}).get('sha') != older
                    or compare.get('merge_base_commit', {}).get('sha') != older or compare.get('behind_by') != 0):
                raise ValueError('Source tracking cannot roll back or leave canonical main')
        if head(api) != latest:
            continue
        if older == latest:
            return {'source_sha': latest, 'changed': False}
        value = {'source_sha': latest, 'source_url': f'https://github.com/{control.CANONICAL}/commit/{latest}',
                 'observed_at': control.now().isoformat(), 'purpose': 'source tracking; not package qualification'}
        body = {'message': 'Track canonical main ' + latest[:12], 'branch': 'main', 'sha': document['sha'],
                'content': base64.b64encode((json.dumps(value, indent=2) + '\n').encode()).decode()}
        try:
            api(path, body)
        except Conflict:
            continue
        return {'source_sha': latest, 'changed': True}
    raise RuntimeError('Source tracking changed during all bounded attempts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != control.CHANNEL
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        raise RuntimeError('Automatic source tracking requires the trusted consumer main checkout')
    result = sync()
    if args.receipt:
        args.receipt.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
