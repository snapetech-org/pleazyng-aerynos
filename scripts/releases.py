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
import urllib.parse
import control
import packaging_recovery

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


def release_tag_commit(tag):
    if version(tag) is None:
        raise ValueError('An exact application version tag is required')
    obj = control.api(f'repos/{control.CANONICAL}/git/ref/tags/{urllib.parse.quote(tag, safe="")}')['object']
    seen = set()
    for _ in range(10):
        sha = obj.get('sha', '')
        if not re.fullmatch('[0-9a-f]{40}', sha) or sha in seen:
            raise ValueError('Invalid or cyclic version tag target')
        seen.add(sha)
        if obj.get('type') == 'commit':
            return sha
        if obj.get('type') != 'tag':
            raise ValueError('Version tag must resolve to a commit')
        obj = control.api(f'repos/{control.CANONICAL}/git/tags/{sha}')['object']
    raise ValueError('Version tag nesting limit exceeded')


def require_frozen_source(tag_source, compiler_source, assembler_source):
    if compiler_source != tag_source or assembler_source != tag_source:
        # Ordinary releases use exact equality. Packaging recovery goes through
        # its independent verifier; a receipt flag never waives this guard.
        raise ValueError('Version tag differs from compiler/assembler source; verified unchanged-product recovery proof is required')


def resolve(release):
    asset = linux_asset(release)
    with tempfile.TemporaryDirectory(prefix='aerynos-release-') as temporary:
        checksums = Path(temporary) / 'SHA256SUMS.txt'
        control.run(['gh', 'release', 'download', release['tag_name'], '--repo', control.CANONICAL,
                     '--pattern', checksums.name, '--dir', temporary])
        core_source, core_run = verify_file(checksums)
        completed_core(core_source, core_run)
        tag_source = release_tag_commit(release['tag_name'])
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
        # A packaging repair never changes the original Linux compiler source.
        require_frozen_source(tag_source, source, tag_source)
        recovery = None
        if core_source != tag_source:
            recovery = packaging_recovery.verify(release, temporary, checksums.read_text(),
                product_source=tag_source, compiler_source=source, workflow_source=core_source,
                producer_run=producer, assembly_run=core_run, linux_sha256=checksum, verify_file=verify_file)
    # Keep original Linux compilation and successful final assembly separate.
    return {'source_sha': source, 'core_producer_run_id': producer, 'core_tag': release['tag_name'],
            'core_release_id': release['id'], 'core_linux_sha256': checksum,
            'core_release_url': release['html_url'], 'core_run_id': core_run,
            'core_source_sha': core_source, 'version_tag_source_sha': tag_source,
            'core_packaging_source_sha': recovery['packaging_source_sha'] if recovery else '',
            'core_recovery_sha256': recovery['receipt_sha256'] if recovery else ''}


def emit(result):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key, value in result.items():
            output.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')
    print(json.dumps(result))


def mirror_main():
    import source_sync
    result = source_sync.sync()
    # The pointer was committed through the Contents API. Start subsequent
    # delivery decisions from that fresh main, never from the earlier checkout.
    control.refresh()
    return result['source_sha']


def next_release(good, status, run_number):
    # Scheduled and manual workflows have separate run counters. Reserve above
    # every recorded attempt so retries cannot collide with immutable uploads.
    return max(run_number + 1, int(good.get('release', 1)) + 1,
               int(status.get('release', 1)) + 1)


def pending_public(receipts, retry=False):
    pending = [(tag, receipt) for tag, receipt in receipts.items()
               if receipt.get('public_verification', {}).get('status') != 'passed']
    if not pending:
        return None
    tag, receipt = min(pending, key=lambda item: version(item[0]))
    after = receipt.get('public_verification', {}).get('retry_after')
    ready = retry or not after or control.now() >= dt.datetime.fromisoformat(after)
    return {'build': False, 'verify_public': bool(ready), 'core_tag': tag,
            'source_sha': receipt['source_sha'],
            'reason': 'public-install-pending' if ready else 'public-install-backoff'}


def reconcile():
    control.refresh()
    latest = mirror_main()
    if control.read('sync-state/promotion-hold.json', {}).get('active'):
        emit({'build': False, 'reason': 'owner-hold'})
        return
    receipts = control.read('sync-state/releases.json', {})
    pending = pending_public(receipts, retry=os.environ.get('MANUAL_RETRY') == 'true')
    if pending:
        emit(pending)
        return
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
    result = {**candidate, 'build': True, 'verify_public': True, 'release': next_release(good, control.read('sync-state/status.json', {}),
                                                        int(os.environ['GITHUB_RUN_NUMBER']))}
    control.write('sync-state/status.json', {**result, 'phase': 'building', 'observed_at': control.now().isoformat()})
    control.commit(['sync-state/status.json'], 'Qualify published core release ' + release['tag_name'])
    emit(result)


def bind_qualified(directory):
    """Bind native results to the published core bytes before promotion."""
    directory = Path(directory)
    q, qualified_assets = control.validated(directory)
    if q['source_sha'] != os.environ['SOURCE_SHA']:
        raise ValueError('Native source differs from the release plan')
    qualified_run_id = os.environ.get('QUALIFIED_RUN_ID')
    if qualified_run_id:
        if not re.fullmatch(r'[1-9][0-9]*', qualified_run_id):
            raise ValueError('Invalid native qualification run ID')
        run_id = int(qualified_run_id)
        run = control.api(f'repos/{control.CHANNEL}/actions/runs/{run_id}')
        jobs = control.api(f'repos/{control.CHANNEL}/actions/runs/{run_id}/jobs?per_page=100')['jobs']
        artifacts = control.api(f'repos/{control.CHANNEL}/actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
        name = 'aerynos-qualified-' + q['source_sha']
        if (run.get('repository', {}).get('full_name') != control.CHANNEL
                or run.get('path') != '.github/workflows/sync.yml'
                or run.get('status') != 'completed'
                or run.get('event') not in ('workflow_dispatch', 'workflow_call')
                or len([job for job in jobs if job['name'] == 'qualify / Native aerynOS install and release-app QA'
                        and job['conclusion'] == 'success']) != 1
                or len([artifact for artifact in artifacts if artifact['name'] == name
                        and not artifact['expired']]) != 1):
            raise ValueError('Reusable native QA must be a successful artifact from this channel workflow')
    tag = os.environ['CORE_TAG']
    release = control.api(f'repos/{control.CANONICAL}/releases/tags/{tag}')
    if (release.get('tag_name') != tag or release.get('draft') or not release.get('published_at')
            or str(release['id']) != os.environ['CORE_RELEASE_ID']):
        raise ValueError('Core release is not the approved published release')
    completed_core(os.environ['CORE_SOURCE_SHA'], os.environ['CORE_RUN_ID'])
    tag_source = release_tag_commit(tag)
    require_frozen_source(tag_source, q['source_sha'], tag_source)
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
    recovery = None
    if os.environ['CORE_SOURCE_SHA'] != tag_source:
        if (not re.fullmatch('[0-9a-f]{40}', os.environ.get('CORE_PACKAGING_SOURCE_SHA', ''))
                or not re.fullmatch('[0-9a-f]{64}', os.environ.get('CORE_RECOVERY_SHA256', ''))):
            raise ValueError('Native delivery has no independently resolved packaging recovery identity')
        with tempfile.TemporaryDirectory(prefix='aerynos-recovery-bind-') as temporary:
            control.run(['gh', 'release', 'download', tag, '--repo', control.CANONICAL,
                         '--pattern', 'SHA256SUMS.txt', '--dir', temporary])
            checksums = Path(temporary) / 'SHA256SUMS.txt'
            if verify_file(checksums) != (os.environ['CORE_SOURCE_SHA'], int(os.environ['CORE_RUN_ID'])):
                raise ValueError('Recovery assembly identity changed after native qualification')
            if (packaging_recovery.checksum_for(checksums.read_text(), 'pleazyng-linux-x64.tar.gz') != checksum
                    or verify_file(directory / 'pleazyng-linux-x64.tar.gz') !=
                    (tag_source, int(os.environ['CORE_PRODUCER_RUN_ID']))):
                raise ValueError('Qualified Linux bytes are not the original frozen compiler artifact')
            recovery = packaging_recovery.verify(release, temporary, checksums.read_text(),
                product_source=tag_source, compiler_source=q['source_sha'], workflow_source=os.environ['CORE_SOURCE_SHA'],
                producer_run=int(os.environ['CORE_PRODUCER_RUN_ID']), assembly_run=int(os.environ['CORE_RUN_ID']),
                linux_sha256=checksum, verify_file=verify_file)
        if (recovery['packaging_source_sha'] != os.environ.get('CORE_PACKAGING_SOURCE_SHA')
                or recovery['receipt_sha256'] != os.environ.get('CORE_RECOVERY_SHA256')):
            raise ValueError('Approved packaging recovery changed after native qualification')
    elif os.environ.get('CORE_PACKAGING_SOURCE_SHA') or os.environ.get('CORE_RECOVERY_SHA256'):
        raise ValueError('Unexpected packaging recovery identity for an ordinary frozen release')
    receipt = {'tag': tag, 'release_id': release['id'], 'url': release['html_url'],
               'linux_sha256': checksum, 'source_sha': q['source_sha'],
               'linux_producer_run_id': provenance['producer_run_id'],
               'assembler_run_id': int(os.environ['CORE_RUN_ID']),
               'assembler_source_sha': os.environ['CORE_SOURCE_SHA'], 'version_tag_source_sha': tag_source,
               'qualification_run_id': int(qualified_run_id or os.environ.get('GITHUB_RUN_ID', '0'))}
    if recovery:
        receipt['packaging_recovery'] = recovery
    path = directory / 'core-release.json'
    control.write(path, receipt)
    with (directory / 'SHA256SUMS.txt').open('a') as output:
        output.write(hashlib.sha256(path.read_bytes()).hexdigest() + '  core-release.json\n')
    return receipt


def record_public(tag, source, report_path=None):
    control.refresh()
    receipts = control.read('sync-state/releases.json', {})
    receipt = receipts.get(tag)
    if not receipt or receipt['source_sha'] != source:
        raise ValueError('Public verification does not match a published channel receipt')
    run_url = os.environ['GITHUB_SERVER_URL'] + '/' + control.CHANNEL + '/actions/runs/' + os.environ['GITHUB_RUN_ID']
    if report_path is None:
        verification = {'status': 'failed', 'run_url': run_url, 'failed_at': control.now().isoformat(),
                        'retry_after': (control.now() + dt.timedelta(minutes=30)).isoformat()}
    else:
        report = json.loads(report_path.read_text())
        required = {'anonymous_live_index_digest', 'anonymous_pinned_index_digest', 'native_live_moss_install',
                    'native_pinned_moss_reinstall', 'ordinary_launch', 'runtime_source_version'}
        if (report.get('status') != 'passed' or report.get('source_sha') != source
                or report.get('tag') != receipt['channel_tag'] or report.get('version') != receipt['version']
                or report.get('release') != receipt['release'] or not isinstance(report.get('checks'), list)
                or not required.issubset(report['checks'])):
            raise ValueError('Public installation checks are incomplete or refer to another package')
        verification = {**report, 'run_url': run_url, 'verified_at': control.now().isoformat(),
                        'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()}
    receipt['public_verification'] = verification
    control.write('sync-state/releases.json', receipts)
    control.commit(['sync-state/releases.json'], 'Record public aerynOS installation ' + tag)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['reconcile', 'bind', 'record-public'])
    parser.add_argument('directory', nargs='?', type=Path)
    parser.add_argument('--core-tag')
    parser.add_argument('--source-sha')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.command == 'reconcile':
        reconcile()
    elif args.command == 'bind':
        bind_qualified(args.directory)
    else:
        record_public(args.core_tag, args.source_sha, args.report)
