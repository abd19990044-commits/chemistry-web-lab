"""Install the integrity-pinned standalone Ketcher editor at image build time.

All chemistry runs in the user's browser (Indigo WASM). No remote editor API,
CDN, npm build, or request-time downloads are required. Vendor binaries stay
outside Git and are lazy-loaded only when opening the 2D editor.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.request
import zipfile

VERSION = '3.18.0'
ARCHIVE_URL = f'https://github.com/epam/ketcher/releases/download/v{VERSION}/ketcher-standalone-{VERSION}.zip'
ARCHIVE_SHA256 = '484e7f10a0e74808ae5f43f0e6448db6f4d884bdd2189aa271108474167a3e7e'
MAX_ARCHIVE = 40 * 1024 * 1024


def download(url, maximum):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError('Editor download exceeds the size limit.')
            return raw
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def install(destination=None, archive=None):
    destination = Path(destination or Path(__file__).parent / 'static/vendor/ketcher')
    raw = Path(archive).read_bytes() if archive else download(ARCHIVE_URL, MAX_ARCHIVE)
    if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('Integrity failure: Ketcher standalone release archive.')
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        members = [m for m in package.infolist() if not m.is_dir() and (
            m.filename in ('index.html', 'logo.svg', 'favicon.ico', 'favicon-16x16.png', 'favicon-32x32.png', 'apple-touch-icon.png', 'manifest.json')
            or m.filename.startswith('static/js/') and Path(m.filename).name.startswith(('main.', '114.', '127.', '609.'))
            or m.filename.startswith('static/css/main.'))]
        if not any(m.filename == 'index.html' for m in members) or sum(m.file_size for m in members) > 40 * 1024 * 1024:
            raise ValueError('Unexpected editor distribution.')
        for member in members:
            path = Path(member.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in member.filename:
                raise ValueError('Unsafe editor archive member.')
            content = package.read(member)
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            hashes[member.filename] = hashlib.sha256(content).hexdigest()
            if target.suffix in ('.js', '.css', '.html', '.svg', '.json'):
                target.with_name(target.name + '.gz').write_bytes(gzip.compress(content, compresslevel=9, mtime=0))
    # Required upstream notices are versioned in our source tree, alongside the
    # application inventory. They are copied into the installed distribution.
    notices = Path(__file__).parent / 'static/licenses/ketcher'
    for name in ('LICENSE', 'NOTICE'):
        (destination / name).write_bytes((notices / name).read_bytes())
    (destination / 'installed.json').write_text(json.dumps(dict(version=VERSION, archive_sha256=ARCHIVE_SHA256, files=hashes), sort_keys=True))
    return destination


if __name__ == '__main__':
    print(install())
