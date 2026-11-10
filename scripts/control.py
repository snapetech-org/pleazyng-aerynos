#!/usr/bin/env python3
"""Validate native packages and atomically promote immutable aerynOS releases."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

CANONICAL = 'snapetech-org/pleazyng'
CHANNEL = 'snapetech-org/pleazyng-aerynos'
REQUIRED = {'native_boulder_build', 'native_moss_install', 'native_moss_upgrade', 'installed_elf', 'bundled_im_cache', 'ordinary_release_launch',
            'background_return_playing', 'background_return_paused', 'route_resume', 'bookmark_resume', 'audio_clock',
            'jellyfin_sign_in', 'dv_default_on', 'fallback_default_on', 'visible_h264', 'pause', 'seek', 'fullscreen'}


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def api(path):
    return json.loads(run(['gh', 'api', path]).stdout)


def now():
    return dt.datetime.now(dt.timezone.utc)


def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def write(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, indent=2) + '\n')


def refresh():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Automatic mutation is restricted to an isolated Actions checkout')
    run(['git', 'fetch', 'origin', 'main'])
    run(['git', 'checkout', '--detach', 'origin/main'])


def commit(paths, message):
    run(['git', 'config', 'user.name', 'github-actions[bot]'])
    run(['git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'])
    run(['git', 'add', '--', *paths])
    if subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode == 0:
        return
    run(['git', 'commit', '-m', message])
    # Ordinary fast-forward push: another writer wins rather than being overwritten.
    run(['git', '-c', 'credential.helper=!gh auth git-credential', 'push', 'origin', 'HEAD:main'])


def is_ancestor(older, newer):
    if older == newer:
        return True
    return api(f'repos/{CANONICAL}/compare/{older}...{newer}')['status'] in ('ahead', 'identical')


def validated(directory):
    directory = Path(directory)
    q = read(directory / 'qualification.json')
    checks = q.get('checks')
    if (q.get('status') != 'qualified' or not isinstance(checks, list)
            or any(not isinstance(check, str) for check in checks)
            or not REQUIRED.issubset(checks)):
        raise RuntimeError('Required native qualification is incomplete')
    if not re.fullmatch('[0-9a-f]{40}', q['source_sha']) or type(q['release']) is not int or q['release'] < 2:
        raise ValueError('Invalid qualified source/release')
    expected_tag = f'main-{q["source_sha"][:12]}-r{q["release"]}'
    if q['tag'] != expected_tag:
        raise ValueError('Snapshot tag does not match source/release')
    assets = {}
    for line in (directory / 'SHA256SUMS.txt').read_text().splitlines():
        digest, name = line.split()
        if Path(name).name != name or name in assets or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid or duplicate asset name/hash')
        p = directory / name
        with p.open('rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() != digest:
                raise RuntimeError('Qualified asset checksum mismatch: ' + name)
        assets[name] = digest
    required_assets = {q['stone'], 'stone.index', 'feed.index', 'qualification.json', 'recipe.tar.gz', 'pleazyng-linux-x64.tar.gz', 'pleazyng-source.tar.gz'}
    if not required_assets.issubset(assets) or assets[q['stone']] != q['stone_sha256']:
        raise RuntimeError('Qualified package/source handoff is incomplete')
    with (directory / 'SHA256SUMS.txt').open('rb') as f:
        assets['SHA256SUMS.txt'] = hashlib.file_digest(f, 'sha256').hexdigest()
    return q, assets


def promotion_allowed(candidate, good, main, ancestry, bootstrap=None):
    if not good.get('source_sha'):
        if not bootstrap or not ancestry(bootstrap['canonical_floor_sha'], candidate['source_sha']):
            raise RuntimeError('First promotion predates the reviewed canonical bootstrap floor')
        for receipt in (good, candidate.get('baseline', {})):
            if any(receipt.get(key) != value for key, value in bootstrap['seed'].items()):
                raise RuntimeError('First promotion must upgrade the exact reviewed seed')
    if good.get('source_sha') == candidate['source_sha']:
        return False
    if good.get('source_sha') and not ancestry(good['source_sha'], candidate['source_sha']):
        raise RuntimeError('Refusing stale or divergent source promotion')
    if not ancestry(candidate['source_sha'], main):
        raise RuntimeError('Qualified source is no longer on canonical main')
    if candidate['release'] <= int(good.get('release', 1)):
        raise RuntimeError('Package release would not advance monotonically')
    return True


def set_hold(active, reason):
    # The supported hold workflow shares the publisher's concurrency group.
    refresh()
    previous = read('sync-state/promotion-hold.json', {})
    write('sync-state/promotion-hold.json', {**previous, 'active': active,
                                           'reason': reason, 'updated_at': now().isoformat(),
                                           'public_promotion': 'held' if active else 'requires complete published core and exact-byte native QA'})
    commit(['sync-state/promotion-hold.json'], 'Hold aerynOS publication' if active else 'Resume guarded aerynOS delivery')


def publish(directory):
    directory = Path(directory).resolve()
    refresh()
    if read('sync-state/promotion-hold.json', {}).get('active'):
        raise RuntimeError('Owner hold: public promotion is disabled')
    q, assets = validated(directory)
    core = read(directory / 'core-release.json')
    if not core or core['source_sha'] != q['source_sha'] or 'core-release.json' not in assets:
        raise RuntimeError('Published core release binding is required')
    good = read('sync-state/last-good.json', {})
    main = api(f'repos/{CANONICAL}/commits/main')['sha']
    if not promotion_allowed(q, good, main, is_ancestor, read('sync-state/bootstrap.json')):
        print('This source is already published')
        return
    tag = q['tag']
    result = subprocess.run(['gh', 'release', 'view', tag, '-R', CHANNEL, '--json', 'isDraft,assets,url'], capture_output=True, text=True)
    if result.returncode:
        notes = directory / 'release-notes.md'
        notes.write_text(f'Independent aerynOS package {q["version"]}, source {q["source_sha"]}, package release {q["release"]}. '
                         'Native build/install and ordinary release-app checks pass. Physical HDR/FEL picture accuracy remains unqualified. '
                         'This channel release is separate from the all-platform core application release. See qualification.json and source archive.\n')
        run(['gh', 'release', 'create', tag, '-R', CHANNEL, '--draft', '--prerelease', '--target', 'main',
             '--title', 'aerynOS main ' + q['source_sha'][:12], '--notes-file', str(notes)])
    release = api(f'repos/{CHANNEL}/releases/tags/{tag}')
    existing = {a['name']: a for a in release['assets']}
    for name, digest in assets.items():
        if name in existing:
            if existing[name].get('digest') != 'sha256:' + digest:
                raise RuntimeError('Refusing to overwrite immutable asset ' + name)
        elif release['draft']:
            run(['gh', 'release', 'upload', tag, str(directory / name), '-R', CHANNEL])
        else:
            raise RuntimeError('Published release is missing an immutable asset')
    release = api(f'repos/{CHANNEL}/releases/tags/{tag}')
    remote = {a['name']: a.get('digest') for a in release['assets']}
    if any(remote.get(name) != 'sha256:' + digest for name, digest in assets.items()):
        raise RuntimeError('GitHub asset digest verification failed')
    # Uploads can take minutes. Fetch the current hold and feed checkpoint
    # immediately before the public transition, not merely before uploads.
    refresh()
    if read('sync-state/promotion-hold.json', {}).get('active'):
        raise RuntimeError('Owner hold raised during upload: public promotion is disabled')
    good = read('sync-state/last-good.json', {})
    main = api(f'repos/{CANONICAL}/commits/main')['sha']
    if not promotion_allowed(q, good, main, is_ancestor, read('sync-state/bootstrap.json')):
        return
    if release['draft']:
        run(['gh', 'release', 'edit', tag, '-R', CHANNEL, '--draft=false'])
    # Only now touch the live pointer. No release asset is deleted or replaced.
    feed = Path('feed')
    feed.mkdir(exist_ok=True)
    shutil.copyfile(directory / 'feed.index', feed / 'stone.index')
    with tarfile.open(directory / 'recipe.tar.gz') as archive:
        if any((p.name != 'recipe' and not p.name.startswith('recipe/')) or p.issym() or p.islnk() or '..' in Path(p.name).parts for p in archive.getmembers()):
            raise ValueError('Unsafe recipe handoff')
        # This is an isolated Actions checkout; retire obsolete tracked recipe helpers.
        if Path('recipe').exists():
            shutil.rmtree('recipe')
        archive.extractall('.', filter='data')
    write('sync-state/last-good.json', {**q, 'core_release': core, 'published_at': now().isoformat(), 'release_url': release['html_url'],
                                      'feed_url': f'https://github.com/{CHANNEL}/raw/refs/heads/main/feed/stone.index',
                                      'release_feed_url': f'https://github.com/{CHANNEL}/releases/download/{tag}/stone.index', 'assets': assets})
    receipts = read('sync-state/releases.json', {})
    receipts[core['tag']] = {**core, 'version': q['version'], 'release': q['release'], 'channel_tag': tag, 'channel_url': release['html_url'],
                             'published_at': now().isoformat()}
    write('sync-state/releases.json', receipts)
    write('sync-state/status.json', {'source_sha': q['source_sha'], 'core_tag': core['tag'],
                                    'phase': 'published', 'published_at': now().isoformat()})
    shutil.copyfile(directory / 'qualification.json', 'qualification.json')
    readme = Path('README.md')
    readme.write_text(readme.read_text().replace(
        'https://github.com/snapetech-org/pleazyng-aerynos/releases/download/snapshot-3.0.22-181-r1/stone.index',
        f'https://github.com/{CHANNEL}/raw/refs/heads/main/feed/stone.index'))
    commit(['feed/stone.index', 'recipe', 'qualification.json', 'sync-state', 'README.md'], 'Promote qualified core release ' + core['tag'])
    print('Published and atomically promoted ' + tag)


def fail(sha):
    refresh()
    if read('sync-state/last-good.json', {}).get('source_sha') == sha:
        return
    status = read('sync-state/status.json', {})
    if status.get('source_sha') != sha:
        return
    write('sync-state/status.json', {**status, 'phase': 'failed', 'failed_at': now().isoformat(),
                                   'retry_after': (now() + dt.timedelta(minutes=30)).isoformat()})
    paths = ['sync-state/status.json']
    if status.get('core_tag'):
        attempts = read('sync-state/release-attempts.json', {})
        attempts[status['core_tag']] = read('sync-state/status.json')
        write('sync-state/release-attempts.json', attempts)
        paths.append('sync-state/release-attempts.json')
    commit(paths, 'Retain last good feed after candidate failure ' + sha[:12])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('publish'); p.add_argument('directory', type=Path)
    p = commands.add_parser('fail'); p.add_argument('sha')
    p = commands.add_parser('hold')
    p.add_argument('--active', choices=['true', 'false'], required=True)
    p.add_argument('--reason', required=True)
    args = parser.parse_args()
    if args.command == 'publish':
        publish(args.directory)
    elif args.command == 'hold':
        set_hold(args.active == 'true', args.reason)
    else:
        fail(args.sha)
