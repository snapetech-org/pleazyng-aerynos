#!/usr/bin/env python3
"""Deliver published core releases using their original attested Linux bytes."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import control

REPOSITORY_ID = 1433487464
SIGNER = control.CANONICAL + '/.github/workflows/build.yml'
MINIMUM = (3, 0, 29)


def version(tag):
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', tag)
    return tuple(map(int, match.groups())) if match else None


def choose(releases, receipts, attempts=None, time=None, retry=False):
    attempts = attempts or {}
    time = time or control.now()
    eligible = [r for r in releases if not r.get('draft') and r.get('published_at')
                and version(r.get('tag_name', '')) is not None
                and version(r['tag_name']) >= MINIMUM and r['tag_name'] not in receipts
                and attempts.get(r['tag_name'], {}).get('phase') != 'superseded'
                and (retry or not attempts.get(r['tag_name'], {}).get('retry_after')
                     or time >= dt.datetime.fromisoformat(attempts[r['tag_name']]['retry_after']))]
    return min(eligible, key=lambda r: version(r['tag_name'])) if eligible else None


def linux_asset(release):
    assets = [a for a in release['assets'] if a['name'] == 'pleazyng-linux-x64.tar.gz']
    if len(assets) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', assets[0].get('digest') or ''):
        raise ValueError('Published core release requires a unique Linux asset with a GitHub SHA-256 digest')
    return assets[0]


def producer_from_verification(results):
    identities = set()
    for result in results:
        cert = result.get('verificationResult', {}).get('signature', {}).get('certificate', {})
        match = re.fullmatch(r'https://github.com/' + re.escape(control.CANONICAL)
                             + r'/actions/runs/([1-9][0-9]*)(?:/attempts/[1-9][0-9]*)?', cert.get('runInvocationURI', ''))
        source = cert.get('sourceRepositoryDigest', '')
        if (str(cert.get('sourceRepositoryIdentifier')) == str(REPOSITORY_ID)
                and re.fullmatch('[0-9a-f]{40}', source) and match):
            identities.add((source, int(match.group(1))))
    if len(identities) != 1:
        raise ValueError('Verified Linux artifact must identify exactly one canonical source and producer')
    return identities.pop()


def verify_file(path):
    result = control.run(['gh', 'attestation', 'verify', str(path), '--repo', control.CANONICAL,
                          '--signer-workflow', SIGNER, '--format', 'json'])
    return producer_from_verification(json.loads(result.stdout))


def completed_core(source, producer):
    metadata = control.api(f'repos/{control.CANONICAL}/actions/runs/{producer}')
    if (metadata.get('repository', {}).get('id') != REPOSITORY_ID
            or metadata.get('path') != '.github/workflows/build.yml'
            or metadata.get('head_sha') != source or metadata.get('status') != 'completed'
            or metadata.get('conclusion') != 'success'
            or metadata.get('event') not in ('workflow_dispatch', 'push', 'release')):
        raise ValueError('Attested release assembler has not completed successfully in the canonical workflow')


def resolve(release):
    asset = linux_asset(release)
    with tempfile.TemporaryDirectory(prefix='aerynos-release-') as temporary:
        checksums = Path(temporary) / 'SHA256SUMS.txt'
        control.run(['gh', 'release', 'download', release['tag_name'], '--repo', control.CANONICAL,
                     '--pattern', checksums.name, '--dir', temporary])
        core_source, core_run = verify_file(checksums)
        completed_core(core_source, core_run)
        expected = [line.split()[0] for line in checksums.read_text().splitlines()
                    if len(line.split()) == 2 and line.split()[1].lstrip('*') == asset['name']]
        if expected != [asset['digest'].removeprefix('sha256:')]:
            raise ValueError('Attested core checksums do not uniquely bind the published Linux asset')
        bundle = Path(temporary) / asset['name']
        control.run(['gh', 'release', 'download', release['tag_name'], '--repo', control.CANONICAL,
                     '--pattern', asset['name'], '--dir', temporary])
        with bundle.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        if asset['digest'] != 'sha256:' + checksum:
            raise ValueError('Published Linux bytes differ from the GitHub release digest')
        source, producer = verify_file(bundle)
    # Keep original Linux compilation and successful final assembly separate.
    return {'source_sha': source, 'core_producer_run_id': producer, 'core_tag': release['tag_name'],
            'core_release_id': release['id'], 'core_linux_sha256': checksum,
            'core_release_url': release['html_url'], 'core_run_id': core_run,
            'core_source_sha': core_source}


def emit(result):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key, value in result.items():
            output.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')
    print(json.dumps(result))


def mirror_main():
    latest = control.api(f'repos/{control.CANONICAL}/commits/main')['sha']
    if not re.fullmatch('[0-9a-f]{40}', latest):
        raise ValueError('Canonical main did not resolve to an immutable commit')
    previous = control.read('sync-state/canonical-main.json', {})
    if previous.get('source_sha') != latest:
        control.write('sync-state/canonical-main.json', {
            'source_sha': latest, 'source_url': f'https://github.com/{control.CANONICAL}/commit/{latest}',
            'observed_at': control.now().isoformat(), 'purpose': 'source tracking; not package qualification'})
        control.commit(['sync-state/canonical-main.json'], 'Track canonical main ' + latest[:12])
    return latest


def reconcile():
    control.refresh()
    latest = mirror_main()
    if control.read('sync-state/promotion-hold.json', {}).get('active'):
        emit({'build': False, 'reason': 'owner-hold'})
        return
    receipts = control.read('sync-state/releases.json', {})
    pages = json.loads(control.run(['gh', 'api', '--paginate', '--slurp',
                                    f'repos/{control.CANONICAL}/releases?per_page=100']).stdout)
    attempts = control.read('sync-state/release-attempts.json', {})
    release = choose([r for page in pages for r in page], receipts, attempts,
                     retry=os.environ.get('MANUAL_RETRY') == 'true')
    if release is None:
        if choose([r for page in pages for r in page], receipts, attempts, retry=True):
            emit({'build': False, 'reason': 'failure-backoff'})
            return
        health = control.read('sync-state/release-health.json', {})
        last = dt.datetime.fromisoformat(health['observed_at']) if health.get('observed_at') else None
        if last is None or control.now() - last >= dt.timedelta(days=7):
            control.write('sync-state/release-health.json', {'observed_at': control.now().isoformat(),
                                                            'pending_releases': 0})
            control.commit(['sync-state/release-health.json'], 'Record release delivery availability')
        emit({'build': False, 'reason': 'no-pending-release'})
        return
    good = control.read('sync-state/last-good.json', {})
    if version('v' + good.get('version', '0.0.0').split('+')[0]) > version(release['tag_name']):
        attempts[release['tag_name']] = {'phase': 'superseded', 'release_id': release['id'],
                                        'reason': 'A newer qualified release is already on the live feed',
                                        'observed_at': control.now().isoformat()}
        control.write('sync-state/release-attempts.json', attempts)
        control.commit(['sync-state/release-attempts.json'], 'Retain disposition for older core release ' + release['tag_name'])
        emit({'build': False, 'reason': 'newer-release-published'})
        return
    try:
        candidate = resolve(release)
        sha = candidate['source_sha']
        floor = good.get('source_sha') or control.read('sync-state/bootstrap.json')['canonical_floor_sha']
        if (not control.is_ancestor(sha, candidate['core_source_sha'])
                or not control.is_ancestor(candidate['core_source_sha'], latest)
                or not control.is_ancestor(floor, sha)):
            raise RuntimeError('Published release source is outside the reviewed canonical lineage')
    except Exception as error:
        attempts[release['tag_name']] = {'phase': 'failed', 'release_id': release['id'],
                                        'failed_at': control.now().isoformat(), 'reason': str(error)[:1000],
                                        'retry_after': (control.now() + dt.timedelta(minutes=30)).isoformat()}
        control.write('sync-state/release-attempts.json', attempts)
        control.commit(['sync-state/release-attempts.json'], 'Record unresolved core delivery ' + release['tag_name'])
        raise
    result = {**candidate, 'build': True, 'release': max(int(os.environ['GITHUB_RUN_NUMBER']) + 1,
                                                       int(good.get('release', 1)) + 1)}
    control.write('sync-state/status.json', {**result, 'phase': 'building', 'observed_at': control.now().isoformat()})
    control.commit(['sync-state/status.json'], 'Qualify published core release ' + release['tag_name'])
    emit(result)


def bind_qualified(directory):
    """Bind native results to the published core bytes before promotion."""
    directory = Path(directory)
    q, qualified_assets = control.validated(directory)
    if q['source_sha'] != os.environ['SOURCE_SHA']:
        raise ValueError('Native source differs from the release plan')
    reused = os.environ.get('QUALIFIED_RUN_ID', '')
    if reused:
        if not re.fullmatch('[1-9][0-9]*', reused):
            raise ValueError('Native QA run ID must be numeric')
        native = control.api(f'repos/{control.CANONICAL}/actions/runs/{reused}')
        if (native.get('repository', {}).get('id') != REPOSITORY_ID
                or native.get('path') != '.github/workflows/aerynos-qualify.yml'
                or native.get('event') != 'workflow_dispatch' or native.get('head_branch') != 'main'
                or native.get('status') != 'completed' or native.get('conclusion') != 'success'):
            raise ValueError('Reused native QA must be a successful trusted canonical qualification run')
    tag = os.environ['CORE_TAG']
    release = control.api(f'repos/{control.CANONICAL}/releases/tags/{tag}')
    if (release.get('tag_name') != tag or release.get('draft') or not release.get('published_at')
            or str(release['id']) != os.environ['CORE_RELEASE_ID']):
        raise ValueError('Core release is not the approved published release')
    completed_core(os.environ['CORE_SOURCE_SHA'], os.environ['CORE_RUN_ID'])
    asset = linux_asset(release)
    checksum = q['build']['assets']['pleazyng-linux-x64.tar.gz']
    provenance = q['build'].get('provenance', {})
    if (qualified_assets.get('pleazyng-linux-x64.tar.gz') != checksum
            or asset['digest'] != 'sha256:' + checksum or checksum != os.environ['CORE_LINUX_SHA256']
            or provenance.get('kind') != 'attested-core-linux-artifact'
            or provenance.get('verified_source_sha') != q['source_sha']
            or str(provenance.get('producer_run_id')) != os.environ['CORE_PRODUCER_RUN_ID']
            or q['version'].split('+')[0] != tag.removeprefix('v')):
        raise ValueError('Native qualification does not match the published core Linux producer and bytes')
    receipt = {'tag': tag, 'release_id': release['id'], 'url': release['html_url'],
               'linux_sha256': checksum, 'source_sha': q['source_sha'],
               'linux_producer_run_id': provenance['producer_run_id'],
               'assembler_run_id': int(os.environ['CORE_RUN_ID']),
               'assembler_source_sha': os.environ['CORE_SOURCE_SHA'],
               'qualification_run_id': int(reused or os.environ.get('GITHUB_RUN_ID', '0'))}
    path = directory / 'core-release.json'
    control.write(path, receipt)
    with (directory / 'SHA256SUMS.txt').open('a') as output:
        output.write(hashlib.sha256(path.read_bytes()).hexdigest() + '  core-release.json\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['reconcile', 'bind'])
    parser.add_argument('directory', nargs='?', type=Path)
    args = parser.parse_args()
    if args.command == 'reconcile':
        reconcile()
    else:
        bind_qualified(args.directory)
