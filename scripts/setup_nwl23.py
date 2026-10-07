#!/usr/bin/env python3
"""Install a verified full NWL23 profile alongside the authored test fixture."""

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from scrabble_harness.engine import MagpieSettings

MAX_ASSET_BYTES = 64 * 1024 * 1024


def verify_asset(content: bytes, expected: dict) -> None:
    if len(content) != expected['bytes'] or len(content) > MAX_ASSET_BYTES:
        raise ValueError('NWL asset size does not match its pinned record')
    if hashlib.sha256(content).hexdigest() != expected['sha256']:
        raise ValueError('NWL asset SHA-256 does not match its pinned record')
    git_blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
    if git_blob != expected['git_blob_sha1']:
        raise ValueError('NWL asset Git object does not match its pinned record')


def word_statistics(path: Path, expected_count: int) -> dict:
    words = path.read_text(encoding='ascii').splitlines()
    if (len(words) != expected_count or len(set(words)) != expected_count
            or any(not re.fullmatch(r'[A-Z]{2,15}', word) for word in words)):
        raise ValueError('Decoded NWL word list is incomplete, duplicated, or malformed')
    return {'word_count': len(words),
            'length_counts': dict(sorted(Counter(len(word) for word in words).items())),
            'lexicon_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def leave_statistics(path: Path, expected_count: int) -> dict:
    content = path.read_bytes()
    if len(content) < 8:
        raise ValueError('Truncated leave table')
    nodes, = struct.unpack_from('<I', content)
    offset = 4 + 4 * nodes
    if offset + 4 > len(content):
        raise ValueError('Truncated leave graph')
    count, = struct.unpack_from('<I', content, offset)
    if count != expected_count or len(content) != offset + 4 + 4 * count:
        raise ValueError('Incomplete NWL leave table')
    values = [value for value, in struct.iter_unpack('<f', content[offset + 4:])]
    if not all(math.isfinite(value) for value in values) or not any(value != 0 for value in values):
        raise ValueError('Leave table contains nonfinite values or is entirely zero')
    return {'leave_count': count, 'nonzero_leave_count': sum(value != 0 for value in values),
            'minimum_leave_value': min(values), 'maximum_leave_value': max(values)}


def audit(library: Path, data: Path, lock: dict) -> dict:
    # Called only in a bounded child, never in the setup parent.
    from scrabble_harness.engine.magpie_ffi import MagpieSession
    with tempfile.TemporaryDirectory(prefix='scrabble-nwl-audit-') as temporary:
        settings = Path(temporary) / 'settings.txt'
        settings.touch()
        with MagpieSession(library, data, settings=settings) as native:
            native.run('set -lex NWL23 -leaves NWL23 -wmp false -savesettings false -threads 1 -showbu false')
            native.run('convert dawg2text NWL23 english')
    return (word_statistics(data / 'lexica/NWL23.txt', lock['word_count'])
            | leave_statistics(data / 'lexica/NWL23.klv2', lock['leave_count']))


def setup(base_manifest: Path, output: Path, asset_source: Path | None) -> dict:
    lock_path = PROJECT / 'configs/nwl23.lock.json'
    lock = json.loads(lock_path.read_text())
    base = json.loads(base_manifest.read_text())
    settings = MagpieSettings.from_manifest(base_manifest)
    if settings.engine_commit != lock['engine_commit']:
        raise ValueError('NWL profile requires the tested engine pin')
    if output.exists():
        installed = MagpieSettings.from_manifest(output / 'manifest.json')
        existing = json.loads((output / 'manifest.json').read_text())
        if installed.lexicon_id != lock['lexicon_id'] or existing.get('word_count') != lock['word_count']:
            raise ValueError('Existing output is a different profile; choose a separate output directory')
        for asset in lock['assets']:
            verify_asset((installed.data / asset['path']).read_bytes(), asset)
        return existing
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.nwl23-stage-', dir=output.parent))
    try:
        data = stage / 'data'
        for name, expected_hash in base['input_asset_sha256'].items():
            # Only the three native inputs supported by this adapter are copied.
            if name not in {'layouts/standard15.txt', 'letterdistributions/english.csv', 'strategy/winpct_english.csv'}:
                raise ValueError('Unsupported base-profile input asset')
            content = (settings.data / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise ValueError('Base native inputs changed while preparing the NWL profile')
            destination = data / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        for asset in lock['assets']:
            name = asset['path']
            if name not in {'lexica/NWL23.kwg', 'lexica/NWL23.klv2'}:
                raise ValueError('Unexpected NWL asset path')
            if asset_source is not None:
                content = (asset_source / Path(name).name).read_bytes()
            else:
                url = f"https://raw.githubusercontent.com/jvc56/MAGPIE-DATA/{lock['data_commit']}/data/{name}"
                with urllib.request.urlopen(url, timeout=45) as response:
                    content = response.read(MAX_ASSET_BYTES + 1)
            verify_asset(content, asset)
            destination = data / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--_audit',
                                 '--library', str(settings.library), '--data', str(data)],
                                capture_output=True, text=True, check=True, timeout=60)
        statistics = json.loads(result.stdout)
        manifest = base | statistics | {
            'lexicon_id': lock['lexicon_id'], 'lexicon_edition': lock['edition'],
            'data_commit': lock['data_commit'], 'lock_sha256': hashlib.sha256(lock_path.read_bytes()).hexdigest(),
            'data': str(output / 'data'), 'dictionary_scope': 'full',
            'kwg_sha256': lock['assets'][0]['sha256'], 'klv_sha256': lock['assets'][1]['sha256'],
            'leave_values_source': 'MAGPIE-DATA pinned NWL23.klv2',
            'source_assets': lock['assets'], 'word_map': False,
            'redistribute_dictionary': False, 'strength_benchmark': False,
        }
        # Validate with staging paths before atomically publishing the new profile.
        (stage / 'manifest.json').write_text(json.dumps(manifest | {'data': str(data)}, indent=2) + '\n')
        MagpieSettings.from_manifest(stage / 'manifest.json')
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        stage.rename(output)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-manifest', type=Path, default=PROJECT / '.native/manifest.json')
    parser.add_argument('--output', type=Path, default=PROJECT / '.native/nwl23')
    parser.add_argument('--asset-source', type=Path, help='Offline directory containing NWL23.kwg and NWL23.klv2')
    parser.add_argument('--_audit', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--library', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--data', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args._audit:
            lock = json.loads((PROJECT / 'configs/nwl23.lock.json').read_text())
            print(json.dumps(audit(args.library, args.data, lock)))
        else:
            profile = setup(args.base_manifest.resolve(), args.output.resolve(),
                            args.asset_source.resolve() if args.asset_source else None)
            print(f"NWL23 ready: {profile['word_count']:,} words, {profile['leave_count']:,} leave values")
            print(f"Manifest: {args.output.resolve() / 'manifest.json'}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'NWL setup failed: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr[-3000:], file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
