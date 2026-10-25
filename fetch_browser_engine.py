"""Fetch the pinned browser-only Open Babel distribution during image build.

Never executed in a web request. Integrity failure stops the build.
Open Babel is GPL-2.0; upstream source and notices are linked alongside assets.
"""
from pathlib import Path
import gzip
import hashlib
import time
import urllib.request

REVISION = "4ccb195c2916e99905abf264bf6212f2b4f9a10d"
BASE = f"https://raw.githubusercontent.com/partridgejiang/cheminfo-to-web/{REVISION}/OpenBabel3/OpenBabel-js/bin/"
ASSETS = {'openbabel.js': 'e65f7946ec5004e2a38f2370ec2118a042a27ec5a27906f0a5eb1e0648446229', 'openbabel.wasm': 'b230a3fd1cabc12c84b8f33bd14de0cf2691b40d6afc2c84f4459e86b81b6992', 'openbabel.data': '382090ab1002bd6319d339d51c52df5fed8c63ce1183ed08a3d7f079da8ec572'}


def install(destination=None):
    destination = Path(destination or Path(__file__).parent / "static/vendor/openbabel")
    destination.mkdir(parents=True, exist_ok=True)
    for name, expected in ASSETS.items():
        target = destination / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        for attempt in range(3):
            try:
                with urllib.request.urlopen(BASE + name, timeout=60) as response:
                    data = response.read(12 * 1024 * 1024)
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError(f"Integrity failure: {name}")
                target.write_bytes(data)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
    # Precompress once at image build time; serving never spends request CPU
    # compressing the multi-megabyte engine for each user. Original bytes stay
    # pinned above and browsers transparently decode Content-Encoding: gzip.
    for name in ASSETS:
        (destination / (name + ".gz")).write_bytes(gzip.compress((destination / name).read_bytes(), compresslevel=9, mtime=0))
    return destination


if __name__ == "__main__":
    print(install())
