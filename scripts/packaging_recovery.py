"""Verify public, attested frozen-source Apple packaging recovery evidence."""
import base64
import hashlib
import json
from pathlib import Path
import re

import control
from verify_frozen_packaging_repair import verify_receipt, verify_frozen_packaging_repair

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
TV_JOB = 'Build tvOS developer package (arm64)'
TV_PACKAGES = ('pleazyng-tvos-unsigned.ipa', 'pleazyng-tvos-appstore.ipa')
IOS_JOB = 'build-ios'
IOS_PACKAGES = ('pleazyng-ios-unsigned.ipa', 'pleazyng-ios-appstore.ipa')


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


def verify_original(receipt, source, producer, *, reused_tvos=False, reused_ios=False):
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
    siblings = ({TV_JOB} if reused_tvos else set()) | ({IOS_JOB} if reused_ios else set())
    for name in APPLE - set(receipt['rebuilt_jobs']) - siblings:
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
        sibling = (receipt.get('tvos_source') if isinstance(item, dict) and item.get('name') == 'tvos-ipa' else
                   receipt.get('ios_source') if isinstance(item, dict) and item.get('name') == 'ios-ipa' else None)
        run = sibling['source_run_id'] if sibling else producer
        checkout = sibling['source_sha'] if sibling else source
        workflow = sibling['workflow_source_sha'] if sibling else source
        require(isinstance(item, dict) and isinstance(item.get('name'), str) and item['name']
                and item['name'] not in names and item.get('source_run_id') == run
                and item.get('source_sha') == checkout and type(item.get('artifact_id')) is int
                and item['artifact_id'] > 0 and re.fullmatch('sha256:[0-9a-f]{64}', str(item.get('digest', ''))),
                'Recovery artifact identity differs from the original frozen producer')
        names.add(item['name'])
        actual = control.api(f'repos/{control.CANONICAL}/actions/artifacts/{item["artifact_id"]}')
        require(actual.get('id') == item['artifact_id'] and actual.get('name') == item['name']
                and actual.get('digest') == item['digest'] and actual.get('expired') is False
                and actual.get('workflow_run', {}).get('id') == run
                and actual.get('workflow_run', {}).get('head_sha') == workflow,
                'Original artifact metadata differs from the attested recovery record')
        if sibling:
            require(item['artifact_id'] == sibling['artifact_id'] and item['digest'] == sibling['artifact_digest'],
                    'Reused Apple artifact differs from its separately verified producer')
    expected = ARTIFACTS | ({'ios-ipa'} if 'build-ios' not in receipt['rebuilt_jobs'] else set())
    expected |= {'tvos-ipa'} if 'Build tvOS developer package (arm64)' not in receipt['rebuilt_jobs'] else set()
    require(expected <= names and names <= expected | {'windows-msix'},
            'Recovery reused artifact coverage is incomplete or unexpected')


def verify_tvos(receipt, release, directory, checksums, product, original_run, workflow, assembly_run, verify_file):
    """Reuse only a fully qualified TV output from another approved same-product repair."""
    source = receipt['tvos_source']
    require(isinstance(source, dict) and source.get('product_source_sha') == product
            and source.get('original_source_run_id') == original_run and source.get('platforms_complete') is True
            and type(source.get('source_run_id')) is int and source['source_run_id'] > 0
            and source['source_run_id'] not in (original_run, assembly_run)
            and type(source.get('job_id')) is int and source['job_id'] > 0
            and type(source.get('run_attempt')) is int and source['run_attempt'] > 0
            and type(source.get('artifact_id')) is int and source['artifact_id'] > 0
            and re.fullmatch('sha256:[0-9a-f]{64}', str(source.get('artifact_digest', ''))),
            'Reused tvOS source does not bind the same frozen product and original CI')
    proof = verify_frozen_packaging_repair(product, source.get('source_sha'),
        source.get('workflow_source_sha'), control.api)
    require(source.get('packaging_approval') == proof, 'Reused tvOS packaging approval differs from independent Git proof')
    old_workflow = source['workflow_source_sha']
    ancestry = control.api(f'repos/{control.CANONICAL}/compare/{old_workflow}...{workflow}')
    require(ancestry.get('status') == 'ahead' and ancestry.get('base_commit', {}).get('sha') == old_workflow
            and ancestry.get('merge_base_commit', {}).get('sha') == old_workflow and ancestry.get('behind_by') == 0,
            'Reused tvOS workflow is not an ancestor of the final assembler')
    run = trusted_run(source['source_run_id'], old_workflow)
    require(run['run_attempt'] == source['run_attempt'] and re.fullmatch(
        'action:release:pool:[a-z-]+:tag:' + re.escape(release['tag_name']) + ':repair:' + source['source_sha'],
        str(run.get('display_title', ''))), 'Reused tvOS run does not identify the approved repair/attempt')
    items = jobs(source['source_run_id'], source['run_attempt'])
    validation = successful(items, 'Validate release request')
    successful(validation.get('steps', []), 'Check out the approved packaging source')
    job = successful(items, TV_JOB)
    require(job.get('id') == source['job_id'], 'Reused tvOS job identity changed')
    for name in ('Build tvOS app', 'Package unsigned tvOS developer IPA', 'Build signed tvOS App Store IPA',
                 'Attest unsigned tvOS developer package', 'Attest signed tvOS App Store package', 'Upload tvOS package'):
        successful(job.get('steps', []), name)
    packages = source.get('package_sha256')
    require(isinstance(packages, dict) and set(packages) == set(TV_PACKAGES) and callable(verify_file),
            'Reused tvOS packages need their original independently verified certificates')
    for name in TV_PACKAGES:
        digest = checksum_for(checksums, name)
        assets = [asset for asset in release['assets'] if asset['name'] == name]
        require(packages[name] == digest and len(assets) == 1 and assets[0].get('digest') == 'sha256:' + digest,
                'Published tvOS package differs from its original qualified producer')
        control.run(['gh', 'release', 'download', release['tag_name'], '--repo', control.CANONICAL,
                     '--pattern', name, '--dir', str(directory)])
        path = Path(directory) / name
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                and verify_file(path) == (old_workflow, source['source_run_id']),
                'Reused tvOS package certificate/digest does not bind its original workflow and producer')
    return source


def verify_ios(receipt, release, directory, checksums, product, original_run, workflow, assembly_run, verify_file):
    """Bind reused signed iOS bytes to the separately approved, unpublished run."""
    source = receipt['ios_source']
    require(isinstance(source, dict) and source.get('product_source_sha') == product
            and source.get('original_source_run_id') == original_run and source.get('platforms_complete') is True
            and type(source.get('source_run_id')) is int and source['source_run_id'] > 0
            and source['source_run_id'] not in (original_run, assembly_run, receipt['tvos_source']['source_run_id'])
            and type(source.get('job_id')) is int and source['job_id'] > 0
            and type(source.get('run_attempt')) is int and source['run_attempt'] > 0
            and type(source.get('artifact_id')) is int and source['artifact_id'] > 0
            and re.fullmatch('sha256:[0-9a-f]{64}', str(source.get('artifact_digest', ''))),
            'Reused iOS source does not bind the frozen product and original CI')
    proof = verify_frozen_packaging_repair(product, source.get('source_sha'),
        source.get('workflow_source_sha'), control.api)
    require(source.get('packaging_approval') == proof, 'Reused iOS approval differs from independent Git proof')
    old_workflow = source['workflow_source_sha']
    ancestry = control.api(f'repos/{control.CANONICAL}/compare/{old_workflow}...{workflow}')
    require(ancestry.get('status') == 'ahead' and ancestry.get('base_commit', {}).get('sha') == old_workflow
            and ancestry.get('merge_base_commit', {}).get('sha') == old_workflow and ancestry.get('behind_by') == 0,
            'Reused iOS workflow is not an ancestor of final assembly')
    run = trusted_run(source['source_run_id'], old_workflow)
    require(run['run_attempt'] == source['run_attempt'] and run.get('conclusion') == 'failure'
            and re.fullmatch('action:release:pool:[a-z-]+:tag:' + re.escape(release['tag_name'])
                             + ':repair:' + source['source_sha'], str(run.get('display_title', ''))),
            'Reused iOS run does not identify the approved unpublished repair')
    items = jobs(source['source_run_id'], source['run_attempt'])
    validation = successful(items, 'Validate release request')
    successful(validation.get('steps', []), 'Check out the approved packaging source')
    job = successful(items, IOS_JOB)
    require(job.get('id') == source['job_id'], 'Reused iOS job identity changed')
    for name in ('Build iOS (no codesign)', 'Create unsigned developer IPA',
                 'Attest unsigned developer IPA', 'Build signed iOS App Store IPA',
                 'Attest signed iOS App Store IPA', 'Upload IPA'):
        successful(job.get('steps', []), name)
    tv = [item for item in items if item.get('name') == TV_JOB]
    require(len(tv) == 1 and tv[0].get('conclusion') == 'skipped' and tv[0].get('steps') == [],
            'Reused iOS sibling unexpectedly built tvOS')
    create = [item for item in items if item.get('name') == 'create-release']
    require(len(create) == 1 and create[0].get('conclusion') == 'failure'
            and len([step for step in create[0].get('steps', [])
                     if step.get('name') == 'Recover verified platform artifacts'
                     and step.get('conclusion') == 'failure']) == 1
            and len([step for step in create[0].get('steps', [])
                     if step.get('name') == 'Create Release'
                     and step.get('conclusion') == 'skipped']) == 1,
            'Reused iOS sibling attempted publication')
    packages = source.get('package_sha256')
    require(isinstance(packages, dict) and set(packages) == set(IOS_PACKAGES) and callable(verify_file),
            'Reused iOS packages need their original certificates')
    for name in IOS_PACKAGES:
        digest = checksum_for(checksums, name)
        assets = [asset for asset in release['assets'] if asset['name'] == name]
        require(packages[name] == digest and len(assets) == 1 and assets[0].get('digest') == 'sha256:' + digest,
                'Published iOS package differs from its original qualified producer')
        control.run(['gh', 'release', 'download', release['tag_name'], '--repo', control.CANONICAL,
                     '--pattern', name, '--dir', str(directory)])
        path = Path(directory) / name
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                and verify_file(path) == (old_workflow, source['source_run_id']),
                'Reused iOS package certificate/digest differs from its original workflow')
    return source


def verify(release, directory, checksums, *, product_source, compiler_source, workflow_source,
           producer_run, assembly_run, linux_sha256, verify_file=None):
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
    require(isinstance(rebuilt, list) and len(rebuilt) == len(set(rebuilt))
            and set(rebuilt) <= APPLE, 'Only approved failed Apple platform rebuilding is supported')
    flags = {name for name, key in (('build-ios', 'rebuild_ios'),
             ('Build tvOS developer package (arm64)', 'rebuild_tvos')) if receipt.get(key) is True}
    reused_tv = None
    if 'tvos_source' in receipt:
        require(flags == {'build-ios'} or (flags == set() and 'ios_source' in receipt),
                'Reused tvOS requires iOS rebuilding or a separate signed iOS sibling')
        reused_tv = verify_tvos(receipt, release, directory, checksums, product_source, producer_run,
                                 workflow_source, assembly_run, verify_file)
    reused_ios = None
    if 'ios_source' in receipt:
        require(flags == set() and reused_tv is not None,
                'Reused iOS requires an assembly-only repair with a signed TV sibling')
        reused_ios = verify_ios(receipt, release, directory, checksums, product_source, producer_run,
                                workflow_source, assembly_run, verify_file)
    require(bool(rebuilt) or (reused_tv is not None and reused_ios is not None),
            'Assembly-only recovery requires both signed Apple siblings')
    require(set(rebuilt) == flags and set(receipt.get('reused_jobs', [])) == REUSED | (APPLE - flags),
            'Recovery reused/rebuilt platform contract is incomplete')
    for alias in ('android_source', 'webos_source'):
        require(receipt.get(alias, {}).get('source_run_id') == producer_run
                and receipt[alias].get('source_sha') == product_source, 'Non-Apple platform source changed during repair')
    verify_original(receipt, product_source, producer_run, reused_tvos=bool(reused_tv),
                    reused_ios=bool(reused_ios))
    assembly = trusted_run(assembly_run, workflow_source, completed_success=True)
    assembly_jobs = jobs(assembly_run, assembly['run_attempt'])
    for name in rebuilt:
        job = successful(assembly_jobs, name)
        label = 'Build signed iOS App Store IPA' if name == 'build-ios' else 'Build signed tvOS App Store IPA'
        successful(job.get('steps', []), label)
    successful(assembly_jobs, 'create-release')
    verify_artifacts(receipt, product_source, producer_run)
    result = {'packaging_source_sha': packaging, 'receipt_sha256': expected, 'approval': proof}
    if reused_tv:
        result['tvos_source'] = reused_tv
    if reused_ios:
        result['ios_source'] = reused_ios
    return result
