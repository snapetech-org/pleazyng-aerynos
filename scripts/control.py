#!/usr/bin/env python3
"""Reconcile canonical main and atomically promote only qualified immutable snapshots."""
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


def plan(sha, good, status, run_number, time, retry=False):
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Canonical source must resolve to a full SHA')
    if good.get('source_sha') == sha:
        return {'build': False, 'reason': 'current', 'source_sha': sha}
    if not retry and status.get('source_sha') == sha and status.get('phase') == 'failed':
        retry = dt.datetime.fromisoformat(status['retry_after'])
        if time < retry:
            return {'build': False, 'reason': 'failure-backoff', 'source_sha': sha}
    return {'build': True, 'source_sha': sha, 'release': max(run_number + 1, int(good.get('release', 1)) + 1)}


def reconcile():
    refresh()
    run(['gh', 'workflow', 'enable', 'reconcile.yml', '-R', CHANNEL])
    latest = api(f'repos/{CANONICAL}/commits/main')['sha']
    sha = os.environ.get('REQUESTED_SOURCE_SHA') or latest
    if sha != latest and (not re.fullmatch('[0-9a-f]{40}', sha) or not is_ancestor(sha, latest)):
        raise ValueError('Requested source is not an immutable canonical main ancestor')
    good = read('sync-state/last-good.json', {})
    status = read('sync-state/status.json', {})
    if good.get('source_sha') and not is_ancestor(good['source_sha'], sha):
        raise RuntimeError('Refusing a source older than the current feed')
    if not good.get('source_sha'):
        bootstrap = read('sync-state/bootstrap.json')
        if not bootstrap or not is_ancestor(bootstrap['canonical_floor_sha'], sha):
            raise RuntimeError('Requested source predates the reviewed bootstrap floor')
    observed = read('sync-state/reconciliation.json', {})
    previous_time = dt.datetime.fromisoformat(observed['observed_at']) if observed.get('observed_at') else None
    if previous_time is None or now() - previous_time >= dt.timedelta(days=7):
        if good.get('tag'):
            release = api(f'repos/{CHANNEL}/releases/tags/{good["tag"]}')
            if release['draft']:
                raise RuntimeError('Last good release unexpectedly became a draft')
        write('sync-state/reconciliation.json', {'canonical_main_sha': latest, 'published_source_sha': good.get('source_sha'),
                                               'observed_at': now().isoformat(), 'last_good_release_available': bool(good.get('tag'))})
        commit(['sync-state/reconciliation.json'], 'Record weekly canonical and last-good feed health')
    result = plan(sha, good, status, int(os.environ['GITHUB_RUN_NUMBER']), now(), retry=os.environ.get('MANUAL_RETRY') == 'true')
    if result['build']:
        write('sync-state/status.json', {**result, 'phase': 'building', 'observed_at': now().isoformat(),
                                       'run_url': os.environ['GITHUB_SERVER_URL'] + '/' + CHANNEL + '/actions/runs/' + os.environ['GITHUB_RUN_ID']})
        commit(['sync-state/status.json'], 'Track canonical main candidate ' + sha[:12])
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        for key, value in result.items():
            f.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')
    print(json.dumps(result))


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


def publish(directory):
    directory = Path(directory).resolve()
    q, assets = validated(directory)
    refresh()
    good = read('sync-state/last-good.json', {})
    main = api(f'repos/{CANONICAL}/commits/main')['sha']
    if not promotion_allowed(q, good, main, is_ancestor, read('sync-state/bootstrap.json')):
        print('This source is already published')
        return
    tag = q['tag']
    result = subprocess.run(['gh', 'release', 'view', tag, '-R', CHANNEL, '--json', 'isDraft,assets,url'], capture_output=True, text=True)
    if result.returncode:
        notes = directory / 'release-notes.md'
        notes.write_text(f'Independent aerynOS main snapshot {q["version"]}, source {q["source_sha"]}, package release {q["release"]}. '
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
    write('sync-state/last-good.json', {**q, 'published_at': now().isoformat(), 'release_url': release['html_url'],
                                      'feed_url': f'https://github.com/{CHANNEL}/raw/refs/heads/main/feed/stone.index',
                                      'release_feed_url': f'https://github.com/{CHANNEL}/releases/download/{tag}/stone.index', 'assets': assets})
    write('sync-state/status.json', {'source_sha': q['source_sha'], 'phase': 'published', 'published_at': now().isoformat()})
    shutil.copyfile(directory / 'qualification.json', 'qualification.json')
    readme = Path('README.md')
    readme.write_text(readme.read_text().replace(
        'https://github.com/snapetech-org/pleazyng-aerynos/releases/download/snapshot-3.0.22-181-r1/stone.index',
        f'https://github.com/{CHANNEL}/raw/refs/heads/main/feed/stone.index'))
    commit(['feed/stone.index', 'recipe', 'qualification.json', 'sync-state', 'README.md'], 'Promote qualified canonical main ' + q['source_sha'][:12])
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
    commit(['sync-state/status.json'], 'Retain last good feed after candidate failure ' + sha[:12])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('reconcile')
    p = commands.add_parser('publish'); p.add_argument('directory', type=Path)
    p = commands.add_parser('fail'); p.add_argument('sha')
    args = parser.parse_args()
    if args.command == 'reconcile':
        reconcile()
    elif args.command == 'publish':
        publish(args.directory)
    else:
        fail(args.sha)
