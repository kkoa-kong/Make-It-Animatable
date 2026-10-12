"""Prepare pinned Linux/CPython wheels on the host; never install into the host venv."""
import argparse
import hashlib
import html.parser
import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, parse_qs

from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename, canonicalize_name


class Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.urls.extend(value for key, value in attrs if key == 'href' and value)


def curl(url, destination):
    subprocess.run(['curl', '--http2', '--fail', '--location', '--silent', '--show-error',
                    '--connect-timeout', '20', '--max-time', '300',
                    '--output', str(destination), url], check=True)


def verify(path, digest=None):
    if digest:
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
        if h.hexdigest() != digest:
            raise RuntimeError(f'SHA256 mismatch: {path}')
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError(f'Corrupt wheel: {path}')


def prepare(requirement, wheelhouse, index, tags):
    req = Requirement(requirement)
    name = canonicalize_name(req.name)
    for path in wheelhouse.iterdir():
        if path.suffix == '.whl':
            wheel_name, version, _, wheel_tags = parse_wheel_filename(path.name)
            if wheel_name == name and version in req.specifier and wheel_tags & tags.keys():
                verify(path)
                print(f'REUSE {path.name}', flush=True)
                return
    special = {
        'torch-cluster': 'https://data.pyg.org/whl/torch-2.8.0+cu129.html',
        'pytorch3d': 'https://miropsota.github.io/torch_packages_builder/pytorch3d/',
        'xformers': 'https://download.pytorch.org/whl/cu129/xformers/',
    }
    page = special.get(name, index.rstrip('/') + '/' + name + '/')
    with tempfile.TemporaryDirectory(prefix='.mia-wheel-', dir=wheelhouse) as temporary:
        listing = Path(temporary) / 'index.html'
        curl(page, listing)
        parser = Links()
        parser.feed(listing.read_text())
        candidates = []
        for link in parser.urls:
            url = urljoin(page, link)
            filename = unquote(Path(urlsplit(url).path).name)
            if not filename.endswith('.whl'):
                continue
            wheel_name, version, _, wheel_tags = parse_wheel_filename(filename)
            matches = wheel_tags & tags.keys()
            if wheel_name == name and version in req.specifier and matches:
                candidates.append((min(tags[t] for t in matches), filename, url))
        if not candidates:
            if name != 'iopath':
                raise RuntimeError(f'No matching wheel for {req} at {page}')
            # iopath is a pure-Python source release. Build its wheel on the host.
            subprocess.run(['uv', 'tool', 'run', '--python', __import__('sys').executable,
                            '--from', 'pip', 'pip', 'wheel', '--no-deps',
                            '--index-url', index, '--wheel-dir', str(wheelhouse), str(req)], check=True)
            return
        _, filename, url = min(candidates)
        destination = wheelhouse / filename
        partial = Path(temporary) / filename
        curl(url.split('#')[0], partial)
        digest = parse_qs(urlsplit(url).fragment).get('sha256', [None])[0]
        verify(partial, digest)
        partial.replace(destination)
        print(f'READY {filename} ({destination.stat().st_size} bytes; source {urlsplit(url).hostname})', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('wheelhouse', type=Path)
    parser.add_argument('--index', default='https://mirrors.aliyun.com/pypi/simple/')
    args = parser.parse_args()
    args.wheelhouse.mkdir(parents=True, exist_ok=True)
    pins = Path(__file__).with_name('api-offline-requirements.txt').read_text().splitlines()
    pins = [line for line in pins if line and not line.startswith('#')]
    tags = {tag: position for position, tag in enumerate(sys_tags())}
    print(f'Preparing {len(pins)} pinned runtime wheels via {args.index}', flush=True)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(prepare, pin, args.wheelhouse, args.index, tags) for pin in pins]
        for future in futures:
            future.result()
    print('WHEELHOUSE COMPLETE', flush=True)


if __name__ == '__main__':
    main()
