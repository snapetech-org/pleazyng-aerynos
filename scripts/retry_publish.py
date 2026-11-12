#!/usr/bin/env python3
"""Promote a previously qualified same-repository native artifact."""
import argparse
import os
from pathlib import Path
import re

import control
import releases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--core-tag', required=True)
    parser.add_argument('--qualified-run-id', required=True)
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Qualified publication requires an isolated Actions checkout')
    if releases.version(args.core_tag) is None or not re.fullmatch(r'[1-9][0-9]*', args.qualified_run_id):
        raise ValueError('Invalid core tag or qualification run ID')
    release = control.api(f'repos/{control.CANONICAL}/releases/tags/{args.core_tag}')
    candidate = releases.resolve(release)
    qualification = control.read(args.directory / 'qualification.json')
    if (qualification['source_sha'] != candidate['source_sha']
            or qualification['version'].split('+')[0] != args.core_tag.removeprefix('v')):
        raise ValueError('Native qualification is for another core release')
    os.environ.update({
        'SOURCE_SHA': candidate['source_sha'],
        'CORE_TAG': candidate['core_tag'],
        'CORE_RELEASE_ID': str(candidate['core_release_id']),
        'CORE_RUN_ID': str(candidate['core_run_id']),
        'CORE_SOURCE_SHA': candidate['core_source_sha'],
        'CORE_LINUX_SHA256': candidate['core_linux_sha256'],
        'CORE_PRODUCER_RUN_ID': str(candidate['core_producer_run_id']),
        'CORE_PACKAGING_SOURCE_SHA': candidate['core_packaging_source_sha'],
        'CORE_RECOVERY_SHA256': candidate['core_recovery_sha256'],
        'QUALIFIED_RUN_ID': args.qualified_run_id,
    })
    releases.bind_qualified(args.directory)
    control.publish(args.directory)


if __name__ == '__main__':
    main()
