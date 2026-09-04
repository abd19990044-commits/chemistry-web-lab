# -*- coding: utf-8 -*-
"""Builds generic downloadable ZIP packages for Chemistry Lab Local Companion Agent."""
from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List

_BASE_DIR = Path(__file__).resolve().parent.parent
_DIST_DIR = _BASE_DIR / "dist" / "packages"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_all_packages() -> Dict[str, Dict[str, Any]]:
    """Builds Windows, Linux, macOS, and HPC agent ZIP packages."""
    _DIST_DIR.mkdir(parents=True, exist_ok=True)
    manifests: Dict[str, Dict[str, Any]] = {}

    configs = [
        {
            "id": "windows-x64",
            "name": "ChemistryLabAgent-Windows.zip",
            "display_name": "Windows (x64)",
            "os_name": "windows",
            "scripts": [
                _BASE_DIR / "bootstrap" / "windows" / "install_and_run.bat",
                _BASE_DIR / "bootstrap" / "windows" / "bootstrap_windows.ps1",
            ],
            "reqs": _BASE_DIR / "requirements-local-agent.txt",
        },
        {
            "id": "linux-x64",
            "name": "ChemistryLabAgent-Linux.zip",
            "display_name": "Linux (x64 / Workstation)",
            "os_name": "linux",
            "scripts": [
                _BASE_DIR / "bootstrap" / "linux" / "install_and_run.sh",
                _BASE_DIR / "bootstrap" / "linux" / "start_agent.sh",
            ],
            "reqs": _BASE_DIR / "requirements-local-agent.txt",
        },
        {
            "id": "macos",
            "name": "ChemistryLabAgent-macOS.zip",
            "display_name": "macOS",
            "os_name": "macos",
            "scripts": [
                _BASE_DIR / "bootstrap" / "macos" / "install_and_run.command",
                _BASE_DIR / "bootstrap" / "macos" / "install_and_run.sh",
            ],
            "reqs": _BASE_DIR / "requirements-local-agent.txt",
        },
        {
            "id": "hpc-linux",
            "name": "ChemistryLabAgent-HPC.zip",
            "display_name": "University HPC / Supercomputer",
            "os_name": "hpc",
            "scripts": [
                _BASE_DIR / "bootstrap" / "hpc" / "install_hpc_agent.sh",
                _BASE_DIR / "bootstrap" / "hpc" / "start_hpc_agent.sh",
            ],
            "reqs": _BASE_DIR / "requirements-local-agent-hpc.txt",
        },
    ]

    for cfg in configs:
        zip_path = _DIST_DIR / cfg["name"]
        member_hashes: Dict[str, str] = {}

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Add platform scripts
            for scr in cfg["scripts"]:
                if scr.is_file():
                    with open(scr, "rb") as sf:
                        sdata = sf.read()
                    zf.writestr(scr.name, sdata)
                    member_hashes[scr.name] = _sha256_bytes(sdata)

            # Add requirements
            if cfg["reqs"].is_file():
                with open(cfg["reqs"], "rb") as rf:
                    rdata = rf.read()
                zf.writestr(cfg["reqs"].name, rdata)
                member_hashes[cfg["reqs"].name] = _sha256_bytes(rdata)

            # Add local_agent package
            agent_pkg_dir = _BASE_DIR / "local_agent"
            for root, _, files in os.walk(agent_pkg_dir):
                for f in sorted(files):
                    if f.endswith(".py"):
                        full_f = Path(root) / f
                        rel_f = str(full_f.relative_to(_BASE_DIR)).replace("\\", "/")
                        with open(full_f, "rb") as pf:
                            pdata = pf.read()
                        zf.writestr(rel_f, pdata)
                        member_hashes[rel_f] = _sha256_bytes(pdata)

            # Add README
            readme_text = f"""Chemistry Lab Local Agent ({cfg['display_name']})
Version: 1.0.3

1. Extract all files from this ZIP.
2. Run the platform launcher ({cfg['scripts'][0].name}).
3. The program will generate a Connection API (CLA_...).
4. Open the Chemistry Lab website -> Local & HPC Execution -> Connect Computer.
5. Enter the Connection API to pair this computer with your account.
6. Configure your ORCA executable path and directories.
"""
            readme_bytes = readme_text.encode("utf-8")
            zf.writestr("README_LOCAL_AGENT.txt", readme_bytes)
            member_hashes["README_LOCAL_AGENT.txt"] = _sha256_bytes(readme_bytes)

            version_bytes = b"1.0.3\n"
            zf.writestr("VERSION", version_bytes)
            member_hashes["VERSION"] = _sha256_bytes(version_bytes)

            # Generate checksums.txt
            checksum_lines = [f"{h}  {name}" for name, h in sorted(member_hashes.items())]
            checksum_bytes = ("\n".join(checksum_lines) + "\n").encode("utf-8")
            zf.writestr("checksums.txt", checksum_bytes)

        sha = _sha256_file(zip_path)
        manifests[cfg["id"]] = {
            "id": cfg["id"],
            "display_name": cfg["display_name"],
            "os_name": cfg["os_name"],
            "filename": cfg["name"],
            "size_bytes": zip_path.stat().st_size,
            "sha256": sha,
            "version": "1.0.3",
            "file_path": str(zip_path),
            "instructions": [
                f"Extract {cfg['name']}",
                f"Run {cfg['scripts'][0].name}",
                "Copy the CONNECTION API displayed in console",
                "Paste into Chemistry Lab website -> Connect Computer",
            ],
        }

    # Write overall checksums.txt in dist/packages/
    with open(_DIST_DIR / "checksums.txt", "w", encoding="utf-8") as cf:
        for k, v in manifests.items():
            cf.write(f"{v['sha256']}  {v['filename']}\n")

    return manifests


def get_package_manifests() -> List[Dict[str, Any]]:
    m = build_all_packages()
    return list(m.values())


def get_package_file_path(package_id: str) -> Optional[str]:
    m = build_all_packages()
    if package_id in m:
        return m[package_id]["file_path"]
    return None


if __name__ == "__main__":
    res = build_all_packages()
    for k, v in res.items():
        print(f"Built {k}: {v['filename']} ({v['size_bytes']} bytes, SHA256: {v['sha256'][:16]}...)")
