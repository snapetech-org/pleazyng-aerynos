"""Verify public, attested frozen-source Apple packaging recovery evidence."""
import base64
import hashlib
import json
from pathlib import Path
import re

import control
from verify_frozen_packaging_repair import verify_receipt

RECEIPT = 'platform-reuse-provenance.json'
REPOSITORY_ID = 1433487464
APPLE = {'build-ios', 'Build tvOS developer package (arm64)'}
REUSED = {'Validate release request', 'Build Linux (x64)', 'Build Linux (arm64)',
          'build-macos', 'Build Windows (x64)', 'Build Windows (arm64)', 'package-windows',
          'Build Samsung Tizen TV package (ARM) / validate',
          'Build Samsung Tizen TV package (ARM) / package'}
ARTIFACTS = {'pleazyng-tizen-arm-experimental', 'macos-dmg', 'windows-x64-portable',
             'windows-arm64-build', 'windows-installer', 'windows-chocolatey',
             'linux-x64', 'linux-arm64', 'linux-x64-appimage', 'android-apk', 'webos-ipk'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checksum_for(text, name):
    values = [line.split()[0] for line in text.splitlines()
              if len(line.split()) == 2 and line.split()[1].lstrip('*') == name]
    require(len(values) == 1 and re.fullmatch('[0-9a-f]{64}', values[0]),
            'Attested checksums must uniquely bind ' + name)
    return values[0]


def jobs(run_id, attempt):
    result = control.api(f'repos/{control.CANONICAL}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    items = result.get('jobs')
    require(isinstance(items, list) and result.get('total_count') == len(items),
            'Complete public recovery job inventory is required')
    return items


def successful(items, name):
    matches = [item for item in items if item.get('name') == name]
    require(len(matches) == 1 and matches[0].get('status') == 'completed'
            and matches[0].get('conclusion') == 'success', 'Required recovery job did not pass uniquely: ' + name)
    return matches[0]


def trusted_run(run_id, source, *, completed_success=False):
    run = control.api(f'repos/{control.CANONICAL}/actions/runs/{run_id}')
    require(run.get('id') == run_id and run.get('repository', {}).get('id') == REPOSITORY_ID
            and run.get('head_repository', {}).get('id') == REPOSITORY_ID
            and run.get('head_sha') == source and run.get('head_branch') == 'main'
            and run.get('path') == '.github/workflows/build.yml'
            and run.get('event') == 'workflow_dispatch' and run.get('status') == 'completed'
            and type(run.get('run_attempt')) is int and run['run_attempt'] > 0
            and (not completed_success or run.get('conclusion') == 'success'),
            'Recovery run is not the completed trusted canonical source/workflow')
    return run


def manifest(source):
    metadata = control.api(f'repos/{control.CANONICAL}/contents/docs/maintainers/capability-manifest.json?ref={source}')
    require(metadata.get('type') == 'file' and metadata.get('encoding') == 'base64',
            'Frozen release CI manifest is unavailable')
    payload = base64.b64decode(''.join(metadata['content'].split()), validate=True)
    require(len(payload) <= 1024 * 1024, 'Frozen CI manifest is too large')
    blob = b'blob ' + str(len(payload)).encode() + b'\0' + payload
    require(hashlib.sha1(blob).hexdigest() == metadata.get('sha'), 'Frozen CI manifest digest mismatch')
    return json.loads(payload)['release']['releaseGate']['requiredJobs']


def verify_original(receipt, source, producer):
    run = trusted_run(producer, source)
    items = jobs(producer, run['run_attempt'])
    required = manifest(source)
    require(isinstance(required, list) and required and all(isinstance(name, str) and name for name in required)
            and len(required) == len(set(required)), 'Frozen original CI contract is incomplete')
    for name in required:
        successful(items, 'Release CI / ' + name)
    for item in items:
        if item.get('name', '').startswith('Release CI / '):
            require(item.get('status') == 'completed' and item.get('conclusion') in ('success', 'skipped'),
                    'An original embedded CI job failed or remains unfinished')
    successful(items, 'Freeze qualified source')
    for name in REUSED | {'build-android', 'Build WebOS TV package (ARMv7)'}:
        successful(items, name)
    for name in APPLE - set(receipt['rebuilt_jobs']):
        successful(items, name)
    attempted = [item for item in items if item.get('name') == 'create-release']
    require(len(attempted) == 1 and attempted[0].get('conclusion') == 'skipped',
            'Original failed build already attempted publication')
    require(receipt.get('ci_mode') == 'embedded' and receipt.get('ci_source_sha') == source
            and receipt.get('ci_run_id') == producer, 'Receipt does not bind the original embedded CI')


def verify_artifacts(receipt, source, producer):
    records = receipt.get('artifacts')
    require(isinstance(records, list) and records, 'Recovery artifact inventory is missing')
    names = set()
    for item in records:
        require(isinstance(item, dict) and isinstance(item.get('name'), str) and item['name']
                and item['name'] not in names and item.get('source_run_id') == producer
                and item.get('source_sha') == source and type(item.get('artifact_id')) is int
                and item['artifact_id'] > 0 and re.fullmatch('sha256:[0-9a-f]{64}', str(item.get('digest', ''))),
                'Recovery artifact identity differs from the original frozen producer')
        names.add(item['name'])
        actual = control.api(f'repos/{control.CANONICAL}/actions/artifacts/{item["artifact_id"]}')
        require(actual.get('id') == item['artifact_id'] and actual.get('name') == item['name']
                and actual.get('digest') == item['digest'] and actual.get('expired') is False
                and actual.get('workflow_run', {}).get('id') == producer
                and actual.get('workflow_run', {}).get('head_sha') == source,
                'Original artifact metadata differs from the attested recovery record')
    expected = ARTIFACTS | ({'ios-ipa'} if 'build-ios' not in receipt['rebuilt_jobs'] else set())
    expected |= {'tvos-ipa'} if 'Build tvOS developer package (arm64)' not in receipt['rebuilt_jobs'] else set()
    require(expected <= names and names <= expected | {'windows-msix'},
            'Recovery reused artifact coverage is incomplete or unexpected')


def verify(release, directory, checksums, *, product_source, compiler_source, workflow_source,
           producer_run, assembly_run, linux_sha256):
    """Checksums and Linux certificate identities must already be verified."""
    require(compiler_source == product_source, 'Linux compiler source must equal the frozen version tag')
    assets = [item for item in release['assets'] if item['name'] == RECEIPT]
    require(len(assets) == 1, 'Frozen packaging recovery needs its public attested receipt')
    expected = checksum_for(checksums, RECEIPT)
    require(assets[0].get('digest') == 'sha256:' + expected, 'Recovery receipt asset digest differs from attested checksums')
    control.run(['gh', 'release', 'download', release['tag_name'], '--repo', control.CANONICAL,
                 '--pattern', RECEIPT, '--dir', str(directory)])
    path = Path(directory) / RECEIPT
    require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'Public recovery receipt digest mismatch')
    receipt = json.loads(path.read_text())
    require(isinstance(receipt, dict), 'Public recovery receipt must be an object')
    packaging = receipt.get('packaging_source_sha')
    proof = verify_receipt(receipt, product_source_sha=product_source,
                           packaging_source_sha=packaging, workflow_source_sha=workflow_source,
                           source_run_id=producer_run, assembly_run_id=assembly_run, api=control.api)
    require(receipt.get('actual_checkout_sha') == packaging and receipt.get('packaging_approval') == proof
            and receipt.get('linux_archive_sha256') == linux_sha256
            and receipt.get('source_sha') == product_source and receipt.get('head_sha') == packaging
            and receipt.get('version') == release['tag_name'].removeprefix('v')
            and receipt.get('include_apple_store_build') is True
            and receipt.get('platforms_complete') is True and receipt.get('pending_jobs') == []
            and receipt.get('excluded_optional_jobs') == []
            and receipt.get('rebuild_linux') is False and receipt.get('rebuild_macos') is False,
            'Recovery receipt does not prove unchanged frozen Linux inputs and approved checkout')
    rebuilt = receipt.get('rebuilt_jobs')
    require(isinstance(rebuilt, list) and rebuilt and len(rebuilt) == len(set(rebuilt))
            and set(rebuilt) <= APPLE, 'Only approved failed Apple platform rebuilding is supported')
    flags = {name for name, key in (('build-ios', 'rebuild_ios'),
             ('Build tvOS developer package (arm64)', 'rebuild_tvos')) if receipt.get(key) is True}
    require(set(rebuilt) == flags and set(receipt.get('reused_jobs', [])) == REUSED | (APPLE - flags),
            'Recovery reused/rebuilt platform contract is incomplete')
    for alias in ('android_source', 'webos_source'):
        require(receipt.get(alias, {}).get('source_run_id') == producer_run
                and receipt[alias].get('source_sha') == product_source, 'Non-Apple platform source changed during repair')
    verify_original(receipt, product_source, producer_run)
    assembly = trusted_run(assembly_run, workflow_source, completed_success=True)
    assembly_jobs = jobs(assembly_run, assembly['run_attempt'])
    for name in rebuilt:
        job = successful(assembly_jobs, name)
        label = 'Build signed iOS App Store IPA' if name == 'build-ios' else 'Build signed tvOS App Store IPA'
        successful(job.get('steps', []), label)
    successful(assembly_jobs, 'create-release')
    verify_artifacts(receipt, product_source, producer_run)
    return {'packaging_source_sha': packaging, 'receipt_sha256': expected, 'approval': proof}
