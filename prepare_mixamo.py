"""Download and integrity-check the pinned, animation-only Mixamo library.

Run: .venv/bin/python prepare_mixamo.py --destination data/mixamo
Only animation_motion/**/*.fbx and the three original metadata JSONs are fetched.
Completed files are revalidated on reruns; interrupted transfers resume with Range.
No Blender, generated descriptions, or classification is involved.
"""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import time
from urllib.parse import quote, urlsplit

import requests

DATASET = 'Linzhan/Mixamo-Animations-Characters'
REVISION = '7f134bc734557b1d9516cb476d25ea1f577e45c1'
METADATA = ('animation_motion_prompts.json', 'animation_motion_frames.json', 'animation_bones.json')
_thread = threading.local()


def session():
    if not hasattr(_thread, 'session'):
        _thread.session = requests.Session()
        _thread.session.headers['User-Agent'] = 'Make-It-Animatable-Mixamo/1'
    return _thread.session


def safe_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError(f'Unsafe source path: {value!r}')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')) or ':' in value:
        raise ValueError(f'Unsafe source path: {value!r}')
    return value


def local_path(root, relative):
    relative = safe_path(relative)
    path = root / relative
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise ValueError(f'Refusing symlink in destination: {part}')
    return path


def get_inventory(revision, directory, report, *, endpoint='https://huggingface.co', recursive=None):
    base = f'{endpoint}/api/datasets/{DATASET}/tree/{revision}'
    url = base + ('/' + directory if directory else '')
    params = {'recursive': 'true' if (bool(directory) if recursive is None else recursive) else 'false', 'limit': 1000}
    entries = []
    visited = set()
    pages = 0
    while url:
        if url in visited:
            raise RuntimeError('Inventory pagination cycle')
        visited.add(url)
        for attempt in range(5):
            try:
                response = session().get(url, params=params, timeout=(20, 90))
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list):
                    raise ValueError('Remote inventory is not a list')
                break
            except (requests.RequestException, ValueError):
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 16))
        pages += 1
        entries.extend(page)
        url = response.links.get('next', {}).get('url')
        params = None
        # Mirrors may retain canonical HF Link headers. Keep pagination on
        # the selected mirror, but only for this pinned dataset tree.
        if url and endpoint != 'https://huggingface.co':
            parsed = urlsplit(url)
            base_path = urlsplit(base).path
            if (parsed.scheme == 'https' and parsed.netloc == 'huggingface.co'
                    and (parsed.path == base_path or parsed.path.startswith(base_path + '/'))):
                url = endpoint + parsed.path + ('?' + parsed.query if parsed.query else '')
        if url and (urlsplit(url).scheme != 'https' or urlsplit(url).netloc != urlsplit(endpoint).netloc
                    or not urlsplit(url).path.startswith(urlsplit(base).path)):
            raise ValueError(f'Unexpected inventory pagination URL: {url}')
    report['inventory_pages'][directory or 'root'] = pages
    return entries


def integrity(path, asset):
    if not path.is_file() or path.stat().st_size != asset['size']:
        raise ValueError('missing file or size mismatch')
    sha256 = hashlib.sha256()
    git = hashlib.sha1(f"blob {asset['size']}\0".encode())
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha256.update(chunk)
            git.update(chunk)
    digest = sha256.hexdigest()
    expected = asset.get('lfs', {}).get('oid')
    if expected:
        if digest != expected:
            raise ValueError(f'SHA256 mismatch: expected {expected}, got {digest}')
    elif git.hexdigest() != asset['oid']:
        raise ValueError(f"Git blob SHA1 mismatch: expected {asset['oid']}, got {git.hexdigest()}")
    return digest


def download(root, revision, asset, *, endpoint='https://huggingface.co', relative_path=None):
    relative = asset['path'] if relative_path is None else relative_path
    path = local_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    cases = []
    if path.exists():
        try:
            return {'path': relative, 'sha256': integrity(path, asset), 'size': asset['size'],
                    'reused': True, 'integrity_cases': cases}
        except ValueError as exc:
            cases.append({'path': relative, 'case': 'existing_file_mismatch', 'detail': str(exc)})
    partial = local_path(root, relative + '.part')
    source_path = safe_path(asset['path'])
    url = f'{endpoint}/datasets/{DATASET}/resolve/{revision}/{quote(source_path, safe="/")}'
    for attempt in range(5):
        try:
            if partial.exists() and partial.stat().st_size >= asset['size']:
                try:
                    digest = integrity(partial, asset)
                except ValueError:
                    partial.unlink()
                else:
                    partial.replace(path)
                    return {'path': relative, 'sha256': digest, 'size': asset['size'],
                            'reused': False, 'integrity_cases': cases}
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {'Accept-Encoding': 'identity'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            with session().get(url, headers=headers, stream=True, timeout=(20, 90)) as response:
                response.raise_for_status()
                if response.status_code == 206:
                    expected_range = f"bytes {offset}-"
                    content_range = response.headers.get('Content-Range', '')
                    if not content_range.startswith(expected_range) or not content_range.endswith(f"/{asset['size']}"):
                        raise ValueError(f'Unexpected Content-Range: {content_range}')
                elif response.status_code == 200:
                    offset = 0
                else:
                    raise ValueError(f'Unexpected transfer status: {response.status_code}')
                with partial.open('ab' if offset else 'wb') as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        stream.write(chunk)
            try:
                digest = integrity(partial, asset)
            except ValueError as exc:
                cases.append({'path': relative, 'case': 'transfer_integrity_mismatch', 'detail': str(exc)})
                partial.unlink(missing_ok=True)
                raise
            partial.replace(path)
            return {'path': relative, 'sha256': digest, 'size': asset['size'],
                    'reused': False, 'integrity_cases': cases}
        except (requests.RequestException, ValueError, OSError) as exc:
            if attempt == 4:
                return {'path': relative, 'error': str(exc), 'integrity_cases': cases}
            time.sleep(min(2 ** attempt, 16))


def load_metadata(root, filename, report):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                report['duplicate_json_keys'].append({'file': filename, 'key': key})
            result[key] = value
        return result
    with (root / filename).open(encoding='utf-8') as stream:
        value = json.load(stream, object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise ValueError(f'{filename} must be a JSON object')
    return value


def publish_json(root, name, value):
    path = local_path(root, name)
    temporary = local_path(root, name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def prepare(args, report):
    root = args.destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    roots = get_inventory(args.revision, '', report)
    animations = get_inventory(args.revision, 'animation_motion', report)
    assets = [item for item in roots if item.get('type') == 'file' and item.get('path') in METADATA]
    assets += [item for item in animations if item.get('type') == 'file'
               and item.get('path', '').startswith('animation_motion/')
               and item['path'].lower().endswith('.fbx')]
    paths = set()
    for asset in assets:
        path = safe_path(asset['path'])
        if path in paths:
            report['duplicate_remote_paths'].append(path)
        paths.add(path)
        if not isinstance(asset.get('size'), int) or asset['size'] <= 0:
            raise ValueError(f'Invalid remote size for {path}')
        lfs = asset.get('lfs')
        if lfs:
            if lfs.get('size') != asset['size'] or not re.fullmatch('[0-9a-f]{64}', lfs.get('oid', '')):
                raise ValueError(f'Invalid LFS hash/size for {path}')
        elif not re.fullmatch('[0-9a-f]{40}', asset.get('oid', '')):
            raise ValueError(f'No usable upstream hash for {path}')
    report['remote_inventory'] = [{'path': a['path'], 'size': a['size'], 'git_blob_sha1': a['oid'],
                                    'lfs_sha256': a.get('lfs', {}).get('oid')}
                                   for a in sorted(assets, key=lambda a: a['path'])]
    report['missing_metadata'] = sorted(set(METADATA) - paths)
    if report['missing_metadata'] or report['duplicate_remote_paths']:
        raise ValueError('Missing metadata or duplicate remote inventory paths')
    results = {}
    for asset in assets:
        if asset['path'] in METADATA:
            result = download(root, args.revision, asset)
            results[asset['path']] = result
            report['integrity_cases'].extend(result['integrity_cases'])
            if 'error' in result:
                report['failed_downloads'].append(result)
    if report['failed_downloads']:
        raise ValueError('Metadata downloads failed')
    prompts = load_metadata(root, METADATA[0], report)
    frames = load_metadata(root, METADATA[1], report)
    bones = load_metadata(root, METADATA[2], report)
    if not isinstance(bones.get('mixamo'), list) or not all(isinstance(b, str) for b in bones['mixamo']):
        report['invalid_metadata'].append({'file': METADATA[2], 'detail': 'mixamo must be a bone name list'})
    ids = defaultdict(list)
    names = defaultdict(list)
    records = []
    for filename, prompt in sorted(prompts.items()):
        try:
            safe_path(filename)
            if not filename.lower().endswith('.fbx'):
                raise ValueError('Motion filename must end with .fbx')
            relative = 'animation_motion/' + filename
            if not isinstance(prompt, dict):
                raise ValueError('Prompt entry must be an object')
            for field in ('motion_id', 'prompt', 'description'):
                if not isinstance(prompt.get(field), str) or (field != 'description' and not prompt[field]):
                    raise ValueError(f'Invalid {field}')
            ids[prompt['motion_id']].append(filename)
            names[prompt['prompt']].append(filename)
            if filename not in frames:
                report['missing_frames'].append(filename)
            elif isinstance(frames[filename], bool) or not isinstance(frames[filename], int) or frames[filename] <= 0:
                raise ValueError('Frame count must be a positive integer')
            if relative not in paths:
                report['missing_files'].append(relative)
            records.append({'motion_id': prompt['motion_id'], 'filename': filename,
                            'prompt': prompt['prompt'], 'description': prompt['description'],
                            'frames': frames.get(filename), 'relative_path': relative})
        except ValueError as exc:
            report['invalid_metadata'].append({'file': METADATA[0], 'key': filename, 'detail': str(exc)})
    report['duplicate_ids'] = [{'motion_id': key, 'filenames': values} for key, values in sorted(ids.items()) if len(values) > 1]
    report['duplicate_names'] = [{'prompt': key, 'filenames': values} for key, values in sorted(names.items()) if len(values) > 1]
    report['extra_frame_keys'] = sorted(set(frames) - set(prompts))
    referenced = {r['relative_path'] for r in records}
    animation_assets = [a for a in assets if a['path'].startswith('animation_motion/')]
    report['unreferenced_files'] = sorted(a['path'] for a in animation_assets if a['path'] not in referenced)
    fatal_metadata = ('duplicate_json_keys', 'duplicate_ids', 'invalid_metadata', 'missing_frames',
                      'missing_files', 'extra_frame_keys', 'unreferenced_files')
    if any(report[key] for key in fatal_metadata):
        raise ValueError('Metadata association validation failed; see detailed report')
    print(f"Downloading/verifying {len(animation_assets)} FBX files, {sum(a['size'] for a in assets)} total bytes", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(download, root, args.revision, a): a['path'] for a in animation_assets}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
            except Exception as exc:
                result = {'path': futures[future], 'error': str(exc), 'integrity_cases': []}
            results[result['path']] = result
            report['integrity_cases'].extend(result['integrity_cases'])
            if 'error' in result:
                report['failed_downloads'].append(result)
            if number % 100 == 0 or number == len(animation_assets):
                print(f"Verified {number}/{len(animation_assets)}; failures={len(report['failed_downloads'])}", flush=True)
    successful = {key: value for key, value in results.items() if 'error' not in value}
    contents = defaultdict(list)
    for path, result in successful.items():
        if path.startswith('animation_motion/'):
            contents[result['sha256']].append(path)
    report['duplicate_contents'] = [{'sha256': key, 'paths': sorted(value)} for key, value in sorted(contents.items()) if len(value) > 1]
    report['counts'] = {'motions': len(records), 'remote_fbx': len(animation_assets), 'metadata_files': len(METADATA),
                        'verified_files': len(successful), 'verified_fbx': sum(p.startswith('animation_motion/') for p in successful),
                        'verified_bytes': sum(v['size'] for v in successful.values()),
                        'remote_required_bytes': sum(a['size'] for a in assets),
                        'reused_files': sum(v['reused'] for v in successful.values()),
                        'duplicate_name_groups': len(report['duplicate_names']),
                        'duplicate_content_groups': len(report['duplicate_contents'])}
    report['verified_assets'] = [successful[key] for key in sorted(successful)]
    if report['failed_downloads']:
        raise ValueError('Required downloads failed integrity or transfer validation')
    for record in records:
        record['sha256'] = successful[record['relative_path']]['sha256']
    report['integrity_status'] = 'passed'
    report['complete'] = True
    publish_json(root, 'validation_report.json', report)
    publish_json(root, 'index.json', {'schema_version': 1, 'dataset': DATASET, 'revision': args.revision, 'motions': records})
    print(json.dumps({'destination': str(root), 'integrity_status': 'passed', 'counts': report['counts']}, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--destination', type=Path, default=Path(__file__).resolve().parent / 'data' / 'mixamo')
    parser.add_argument('--revision', default=REVISION, help='Immutable 40-character dataset commit')
    parser.add_argument('--workers', type=int, default=8, help='Bounded parallel transfers (1..32)')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.revision):
        parser.error('--revision must be an immutable lowercase 40-character commit hash')
    if not 1 <= args.workers <= 32:
        parser.error('--workers must be between 1 and 32')
    report = {'schema_version': 1, 'dataset': DATASET, 'revision': args.revision,
              'complete': False, 'integrity_status': 'failed', 'inventory_pages': {},
              'hash_verification': 'LFS SHA256 for large files; Git blob SHA1 for non-LFS files; local SHA256 for all',
              'download_scope': ['animation_motion/**/*.fbx', *METADATA]}
    for key in ('duplicate_json_keys', 'duplicate_remote_paths', 'duplicate_ids', 'duplicate_names',
                'duplicate_contents', 'missing_metadata', 'missing_frames', 'missing_files',
                'invalid_metadata', 'extra_frame_keys', 'unreferenced_files', 'integrity_cases', 'failed_downloads'):
        report[key] = []
    try:
        prepare(args, report)
    except Exception as exc:
        report['failure'] = str(exc)
        root = args.destination.resolve()
        root.mkdir(parents=True, exist_ok=True)
        publish_json(root, 'validation_report.json', report)
        local_path(root, 'index.json').unlink(missing_ok=True)
        print(f'Mixamo preparation failed: {exc}; see {root / "validation_report.json"}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
