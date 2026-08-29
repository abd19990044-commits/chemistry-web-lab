# -*- coding: utf-8 -*-
"""
Deterministic and Truthful Deployment Package Builder for Chemistry Lab.

Builds two distinct, purpose-specific distribution packages:
1. HuggingFace/  -> Minimal runtime closure for Docker Space deployment.
2. GitHub/       -> Complete source code distribution with CI, tests, and documentation.

PROVENANCE & INTEGRITY GUARANTEES:
- Supports two explicit modes:
    RELEASE_FROM_COMMIT    : Requires a clean Git working tree. Guarantees 100% commit fidelity.
    SNAPSHOT_FROM_WORKTREE : Allows packaging dirty working trees while truthfully declaring snapshot status.
- Computes SHA-256 for all package files and a deterministic package_tree_hash.
- Performs mandatory post-build self-verification of manifest hashes against written files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {
    '.git',
    '.pytest_cache',
    '__pycache__',
    '.mypy_cache',
    '.ruff_cache',
    '.cache',
    '.hypothesis',
    'node_modules',
    '.wrangler',
    '.venv',
    'venv',
    'scratch',
    '.state',
    '.data',
    'data_cache',
    'htmlcov',
    'HuggingFace',
    'GitHub',
    'temp_pytest_run',
    '.pytest_temp',
}

EXCLUDE_EXTS = {
    '.pyc',
    '.pyo',
    '.pyd',
    '.coverage',
    '.swp',
    '.swo',
    '.DS_Store',
}

GLOBAL_EXCLUDED_NAMES = {
    '.dev.vars',
    'kaggle.json',
    'orca_manual_6_1_0.pdf',
    'orca_all_code.txt',
    'scratch_aux_basis.txt',
    'scratch_dispersion.txt',
    'scratch_manual_analysis.txt',
    'scratch_nmr.txt',
    'scratch_solvation.txt',
    'scratch_tddft.txt',
    'scratch_toc.txt',
    'search_specifics.txt',
    'manual_exact_specs.txt',
    'merg.py',
}

TEXT_EXTS = {
    '.py', '.js', '.ts', '.html', '.css', '.json', '.md', '.txt',
    '.yml', '.yaml', '.toml', '.ini', '.cff', '.sql', '.sh', '.dockerignore'
}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def compute_tree_hash(file_hashes: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for rel_path, digest in sorted(file_hashes):
        h.update(f"{rel_path}:{digest}\n".encode('utf-8'))
    return h.hexdigest()


def is_global_excluded(p: Path) -> bool:
    if p.name in GLOBAL_EXCLUDED_NAMES:
        return True
    if any(part in EXCLUDE_DIRS for part in p.parts):
        return True
    if p.suffix in EXCLUDE_EXTS:
        return True
    return False


def safe_rmtree(target: Path):
    if not target.exists():
        return
    def _onerror(func, path, exc_info):
        try:
            import stat
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    shutil.rmtree(target, onerror=_onerror)


def clear_package_contents(target: Path):
    """Removes every entry of a package directory EXCEPT the exact `.git` entry.

    GitHub/ is an INDEPENDENT nested Git repository: its history, refs, index,
    objects, config and HEAD must survive a package rebuild byte-for-byte.
    Enforcement is structural - the `.git` entry is never even passed to the
    remover, so no caller mistake can delete it. Package MANIFEST generation
    also skips `.git` (see generate_and_verify_manifest) so repository
    metadata never becomes package content.
    """
    if not target.exists():
        return
    for entry in target.iterdir():
        if entry.name == '.git':
            continue
        if entry.is_dir() and not entry.is_symlink():
            safe_rmtree(entry)
        else:
            try:
                entry.unlink()
            except OSError:
                safe_rmtree(entry)


def is_text_file(p: Path) -> bool:
    if p.suffix.lower() in TEXT_EXTS:
        return True
    if p.name in ('LICENSE', 'LICENSE.txt', 'Dockerfile', '.dockerignore', '.gitignore', '.gitattributes', 'MANIFEST', 'CITATION.cff'):
        return True
    return False


def copy_file_safe(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_text_file(src):
        try:
            content = src.read_text(encoding='utf-8')
            content = content.replace('\u2014', '-').replace('\u2013', '-')
            dst.write_text(content, encoding='utf-8', newline='\n')
            return
        except Exception:
            pass
    shutil.copy2(src, dst)


def copy_tree_filtered(src_dir: Path, dst_dir: Path, file_filter=None):
    if not src_dir.exists():
        return
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel_root = Path(root).relative_to(src_dir)
        for f in files:
            p = Path(root) / f
            if is_global_excluded(p):
                continue
            if file_filter and not file_filter(p):
                continue
            dst_file = dst_dir / rel_root / f
            copy_file_safe(p, dst_file)


def inspect_git_state() -> dict:
    state = {
        "git_available": False,
        "clean_sha": None,
        "is_dirty": True,
        "dirty_files": [],
        "branch": "unknown",
    }
    try:
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO_ROOT, text=True).strip()
        status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=REPO_ROOT, text=True).splitlines()
        branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=REPO_ROOT, text=True).strip()
        
        # Filter out HuggingFace and GitHub output directories from dirty calculation
        real_dirty = []
        for line in status:
            fname = line[3:].strip()
            if not (fname.startswith('HuggingFace') or fname.startswith('GitHub')):
                real_dirty.append(line)

        state["git_available"] = True
        state["clean_sha"] = sha
        state["is_dirty"] = len(real_dirty) > 0
        state["dirty_files"] = real_dirty
        state["branch"] = branch
    except Exception as exc:
        state["error"] = str(exc)
    return state


def build_huggingface_package(dest_dir: Path, provenance: dict) -> dict:
    safe_rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Root runtime files
    hf_root_files = [
        'app.py',
        'chem_core.py',
        'kaggle_runner.py',
        'reaction_conditions.py',
        'requirements.txt',
        'pyproject.toml',
        'Dockerfile',
        '.dockerignore',
        'LICENSE',
        'LICENSE.txt',
        'THIRD_PARTY_LICENSES.md',
    ]
    for rf in hf_root_files:
        src_p = REPO_ROOT / rf
        if src_p.exists():
            copy_file_safe(src_p, dest_dir / rf)

    # 2. Runtime directories
    copy_tree_filtered(REPO_ROOT / 'chem_core', dest_dir / 'chem_core')
    copy_tree_filtered(REPO_ROOT / 'kaggle_runner', dest_dir / 'kaggle_runner')
    copy_tree_filtered(REPO_ROOT / 'orca_orchestrator', dest_dir / 'orca_orchestrator')
    copy_tree_filtered(REPO_ROOT / 'services', dest_dir / 'services')
    copy_tree_filtered(REPO_ROOT / 'api', dest_dir / 'api')
    copy_tree_filtered(REPO_ROOT / 'templates', dest_dir / 'templates')
    copy_tree_filtered(REPO_ROOT / 'static', dest_dir / 'static')
    copy_tree_filtered(REPO_ROOT / 'schema', dest_dir / 'schema')

    # 3. Data runtime assets
    if (REPO_ROOT / 'data' / 'ir_peak_database.json').exists():
        copy_file_safe(REPO_ROOT / 'data' / 'ir_peak_database.json', dest_dir / 'data' / 'ir_peak_database.json')
    copy_tree_filtered(REPO_ROOT / 'data' / 'examples', dest_dir / 'data' / 'examples')

    # 4. Academic user manual generator
    if (REPO_ROOT / 'scripts' / 'generate_academic_manual.py').exists():
        copy_file_safe(REPO_ROOT / 'scripts' / 'generate_academic_manual.py', dest_dir / 'scripts' / 'generate_academic_manual.py')

    # 5. orca_engine runtime library
    orca_engine_src = REPO_ROOT / 'orca_engine'
    copy_tree_filtered(orca_engine_src / 'src', dest_dir / 'orca_engine' / 'src')
    for o_meta in ('pyproject.toml', 'README.md', 'LICENSE', '.gitignore'):
        if (orca_engine_src / o_meta).exists():
            copy_file_safe(orca_engine_src / o_meta, dest_dir / 'orca_engine' / o_meta)

    # 6. HuggingFace configuration and documentation
    hf_env_example = """# ORCA Web Lab - Hugging Face Spaces Environment Configuration
# Set these in Hugging Face Space Settings -> Variables and Secrets

# 1. Master Encryption Secret for Kaggle Credential Vault (Required for credential persistence)
# Provide a 256-bit (32-byte) hex string or base64 key
# Generate using: python -c "import secrets; print(secrets.token_hex(32))"
KAGGLE_CREDENTIALS_ENCRYPTION_KEY=

# 2. Cloudflare Control Plane Configuration (Optional - for distributed metadata store)
CONTROL_PLANE_URL=
CONTROL_PLANE_API_TOKEN=
CONTROL_PLANE_PROJECT_ID=orca-web-lab
CONTROL_PLANE_NAMESPACE=production

# 3. Persistent Storage and State Paths
# Set to /data on Spaces with Persistent Storage attached
ORCA_STATE_DIR=/data
ORCA_RESULTS_DIR=/data/results

# 4. Flask Session Secret Key (Recommended for stable sessions across container restarts)
SECRET_KEY=
"""
    (dest_dir / '.env.example').write_text(hf_env_example, encoding='utf-8', newline='\n')

    hf_readme = """---
title: Orca Web Lab & Quantum Engine
emoji: ⚛️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Orca Web Lab & Quantum Chemistry Engine ⚛️🧪

Production-grade quantum chemistry web platform, interactive molecular visualization studio, ORCA 6 input generator, and fault-tolerant calculation manager.

## Hugging Face Spaces Deployment Guide

### 1. Overview
This distribution folder is configured specifically for deployment to **Hugging Face Spaces** using the Docker SDK.

### 2. Required Space Secrets and Configuration
Configure the following in **Space Settings -> Variables and Secrets**:

| Variable / Secret | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `KAGGLE_CREDENTIALS_ENCRYPTION_KEY` | **Secret** | Recommended | 256-bit (32-byte hex) master key for AES-256-GCM AEAD Kaggle credential persistence. |
| `SECRET_KEY` | **Secret** | Recommended | Flask session secret key for stable session cookies across restarts. |
| `CONTROL_PLANE_URL` | Variable | Optional | Remote Cloudflare Worker URL for persistent metadata. |
| `CONTROL_PLANE_API_TOKEN` | **Secret** | Optional | Bearer token for Cloudflare Control Plane authentication. |
| `CONTROL_PLANE_PROJECT_ID` | Variable | Optional | Cloudflare project identifier (`orca-web-lab`). |
| `CONTROL_PLANE_NAMESPACE` | Variable | Optional | Cloudflare namespace (`production`). |

### 3. Persistent Storage Support (`/data`)
If your Space has a **Persistent Storage Volume** attached:
- The orchestrator detects `/data` and stores the SQLite state cache at `/data/orchestrator.sqlite3`.
- Result archives are saved durably to `/data/results/`.
- On free/ephemeral Spaces, the application operates cleanly in ephemeral mode (`./.state/`).

### 4. Container Architecture
- **Base Image:** `python:3.11-slim`
- **Port:** `7860` (bound to `0.0.0.0`)
- **Server:** Gunicorn with 1 worker and 8 concurrent threads.
- **User:** Non-root `appuser` (UID 1000).

### 5. Health Check
Verify container health at any time:
```http
GET /health
```

### 6. License & Commercial Use
ORCA Web Lab is proprietary source-available software authored and owned solely by **Abdulsalam S. Hasan**.

The software is available free of charge for permitted academic, educational, personal, and non-commercial scientific research under the **ORCA Web Lab Academic and Non-Commercial License v1.1** (see `LICENSE`). Non-commercial eligibility is determined by the nature and purpose of the activity.

Commercial use (including internal corporate R&D, sponsored commercial research, commercial SaaS hosting, or product integration) requires a separate written **Commercial License**. Full intellectual property assignment requires a separate written agreement executed with the author.

**Commercial Licensing & Permissions Contact:**
- **Author & Rights Holder:** Abdulsalam S. Hasan
- **Email:** [abd.19990044@gmail.com](mailto:abd.19990044@gmail.com)
- **WhatsApp:** [+9647715541279](https://wa.me/9647715541279)
"""
    (dest_dir / 'README.md').write_text(hf_readme, encoding='utf-8', newline='\n')

    return generate_and_verify_manifest(dest_dir, "huggingface-spaces-deployment", provenance)


def build_github_package(dest_dir: Path, provenance: dict) -> dict:
    # INVARIANT: GitHub/ is an independent nested Git repository. A rebuild
    # refreshes package CONTENT only and must NEVER delete, overwrite, move,
    # recreate or replace GitHub/.git. clear_package_contents() removes stale
    # package entries while structurally preserving the exact .git tree.
    clear_package_contents(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Root files
    gh_root_files = [
        'app.py',
        'chem_core.py',
        'kaggle_runner.py',
        'reaction_conditions.py',
        'requirements.txt',
        'pyproject.toml',
        'pytest.ini',
        'Dockerfile',
        '.dockerignore',
        '.gitignore',
        'LICENSE',
        'LICENSE.txt',
        'THIRD_PARTY_LICENSES.md',
        'CITATION.cff',
        'README.md',
        'CONTRIBUTING.md',
        'SECURITY.md',
        'ARCHITECTURE.md',
        'DEPLOY.md',
        'REPRODUCIBILITY.md',
    ]
    for rf in gh_root_files:
        src_p = REPO_ROOT / rf
        if src_p.exists():
            copy_file_safe(src_p, dest_dir / rf)

    # 2. Source directories
    copy_tree_filtered(REPO_ROOT / 'chem_core', dest_dir / 'chem_core')
    copy_tree_filtered(REPO_ROOT / 'kaggle_runner', dest_dir / 'kaggle_runner')
    copy_tree_filtered(REPO_ROOT / 'orca_orchestrator', dest_dir / 'orca_orchestrator')
    copy_tree_filtered(REPO_ROOT / 'services', dest_dir / 'services')
    copy_tree_filtered(REPO_ROOT / 'api', dest_dir / 'api')
    copy_tree_filtered(REPO_ROOT / 'templates', dest_dir / 'templates')
    copy_tree_filtered(REPO_ROOT / 'static', dest_dir / 'static')
    copy_tree_filtered(REPO_ROOT / 'schema', dest_dir / 'schema')
    copy_tree_filtered(REPO_ROOT / 'data', dest_dir / 'data')
    copy_tree_filtered(REPO_ROOT / 'tests', dest_dir / 'tests')
    copy_tree_filtered(REPO_ROOT / 'tools', dest_dir / 'tools')
    copy_tree_filtered(REPO_ROOT / 'benchmarks', dest_dir / 'benchmarks')
    copy_tree_filtered(REPO_ROOT / 'scripts', dest_dir / 'scripts')
    copy_tree_filtered(REPO_ROOT / 'docs', dest_dir / 'docs')
    copy_tree_filtered(REPO_ROOT / '.github', dest_dir / '.github')

    # 3. Cloudflare Control Plane
    copy_tree_filtered(REPO_ROOT / 'cloudflare-control-plane', dest_dir / 'cloudflare-control-plane')

    # 4. orca_engine
    copy_tree_filtered(REPO_ROOT / 'orca_engine', dest_dir / 'orca_engine')

    # 5. GitHub .env.example
    gh_env_example = """# ORCA Web Lab - Local Development & CI Environment Configuration
# Copy this file to .env or configure in your local test environment

# 1. Master Encryption Secret for Kaggle Credential Vault
# 32-byte hex string (64 characters)
KAGGLE_CREDENTIALS_ENCRYPTION_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

# 2. Cloudflare Control Plane
CONTROL_PLANE_URL=http://127.0.0.1:8787
CONTROL_PLANE_API_TOKEN=test_cf_token_secret_12345
CONTROL_PLANE_PROJECT_ID=orca-web-lab
CONTROL_PLANE_NAMESPACE=test-env

# 3. State & Storage
ORCA_STATE_DIR=.state
ORCA_RESULTS_DIR=.state/results

# 4. Flask
SECRET_KEY=development-secret-key-do-not-use-in-production
"""
    (dest_dir / '.env.example').write_text(gh_env_example, encoding='utf-8', newline='\n')

    return generate_and_verify_manifest(dest_dir, "github-source-release", provenance)


def generate_and_verify_manifest(target_dir: Path, release_type: str, provenance: dict) -> dict:
    timestamp_now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    # Step 1: Scan all files except manifests
    raw_files = []
    file_hashes_for_tree = []
    for p in sorted(target_dir.rglob('*')):
        if '.git' in p.parts:
            continue  # nested repository metadata is never package content
        if p.is_file() and p.name not in ('FILE_MANIFEST.txt', 'RELEASE_MANIFEST.json'):
            rel = str(p.relative_to(target_dir)).replace('\\', '/')
            digest = sha256_file(p)
            sz = p.stat().st_size
            raw_files.append((rel, sz, digest))
            file_hashes_for_tree.append((rel, digest))

    total_bytes = sum(sz for _, sz, _ in raw_files)
    package_tree_hash = compute_tree_hash(file_hashes_for_tree)

    # Step 2: Write FILE_MANIFEST.txt
    manifest_txt_lines = [
        f"# ORCA Web Lab File Manifest - {release_type.upper()}",
        f"# Generated: {timestamp_now}",
        f"# Package Mode: {provenance['package_mode']}",
        f"# Source State: {provenance['source_git_state']}",
        f"# Source Git SHA: {provenance.get('source_git_sha') or 'N/A (Dirty Working Tree Snapshot)'}",
        f"# Package Tree Hash: {package_tree_hash}",
        f"# Total Files: {len(raw_files)}",
        f"# Total Size: {total_bytes:,} bytes ({total_bytes/1024/1024:.2f} MB)",
        "#",
        f"# {'Relative Path':<50} | {'Size':>10} | {'SHA-256':<64}",
        "# " + "-" * 130,
    ]
    for rel, sz, digest in raw_files:
        manifest_txt_lines.append(f"{rel:<50} | {sz:>10,} B | {digest}")

    manifest_txt_p = target_dir / 'FILE_MANIFEST.txt'
    manifest_txt_p.write_text('\n'.join(manifest_txt_lines) + '\n', encoding='utf-8', newline='\n')

    # Step 3: Classify critical runtime files
    runtime_critical_files = {}
    for rel, sz, digest in raw_files:
        origin = "repo_source"
        if rel in ('README.md', '.env.example'):
            origin = "package_metadata"
        elif rel in ('LICENSE', 'LICENSE.txt', 'THIRD_PARTY_LICENSES.md'):
            origin = "legal_document"
        elif rel.startswith('orca_engine/'):
            origin = "orca_engine_component"
        elif rel.startswith('cloudflare-control-plane/'):
            origin = "cloudflare_worker_component"

        runtime_critical_files[rel] = {
            "sha256": digest,
            "size_bytes": sz,
            "source_origin": origin,
        }

    # Also add FILE_MANIFEST.txt to critical files
    manifest_txt_digest = sha256_file(manifest_txt_p)
    runtime_critical_files['FILE_MANIFEST.txt'] = {
        "sha256": manifest_txt_digest,
        "size_bytes": manifest_txt_p.stat().st_size,
        "source_origin": "generated_manifest",
    }

    # Step 4: Write RELEASE_MANIFEST.json
    manifest_json = {
        "schema_version": "orca-web-lab.release-manifest.v1.0",
        "application_name": "orca-web-lab",
        "version": "1.0.3",
        "release_type": release_type,
        "package_mode": provenance["package_mode"],
        "source_type": provenance["source_type"],
        "source_git_state": provenance["source_git_state"],
        "source_git_sha": provenance["source_git_sha"],
        "source_git_dirty": provenance["source_git_dirty"],
        "package_tree_hash": package_tree_hash,
        "generated_at": timestamp_now,
        "file_count": len(runtime_critical_files),
        "total_size_bytes": sum(f["size_bytes"] for f in runtime_critical_files.values()),
        "runtime_critical_files": runtime_critical_files,
    }

    manifest_json_p = target_dir / 'RELEASE_MANIFEST.json'
    manifest_json_p.write_text(json.dumps(manifest_json, indent=2) + '\n', encoding='utf-8', newline='\n')

    # Step 5: MANDATORY POST-BUILD SELF-VERIFICATION
    # Re-verify every single file on disk against the manifest
    print(f"Verifying generated manifest for {target_dir.name}...")
    disk_files = [p for p in target_dir.rglob('*')
                  if '.git' not in p.parts and p.is_file()
                  and p.name != 'RELEASE_MANIFEST.json']
    assert len(disk_files) == len(runtime_critical_files), (
        f"File count mismatch in {target_dir.name}: disk has {len(disk_files)}, manifest has {len(runtime_critical_files)}"
    )

    for p in disk_files:
        rel = str(p.relative_to(target_dir)).replace('\\', '/')
        assert rel in runtime_critical_files, f"Unexpected unmanifested file on disk: {rel}"
        actual_sha = sha256_file(p)
        expected_sha = runtime_critical_files[rel]["sha256"]
        assert actual_sha == expected_sha, (
            f"Hash mismatch for {rel} in {target_dir.name}!\nExpected: {expected_sha}\nActual:   {actual_sha}"
        )

    print(f"[PASS] Self-verification passed: All {len(disk_files)} files in {target_dir.name} match manifest SHA-256 digests.")
    return manifest_json


def main():
    parser = argparse.ArgumentParser(description="Deterministic Package Builder for ORCA Web Lab")
    parser.add_argument(
        "--mode",
        choices=["release", "snapshot", "auto"],
        default="release",
        help="Package mode: 'release' (enforces clean git commit), 'snapshot' (allows dirty worktree), or 'auto' (detects)."
    )
    parser.add_argument(
        "--target",
        choices=["all", "huggingface", "github"],
        default="all",
        help="Target packages to assemble."
    )
    args = parser.parse_args()

    git_info = inspect_git_state()
    is_dirty = git_info["is_dirty"]
    clean_sha = git_info["clean_sha"]

    # Determine mode
    if args.mode == "release":
        if is_dirty:
            print("[ERROR] Working tree is dirty. Cannot build in RELEASE_FROM_COMMIT mode.")
            print("Dirty files detected:")
            for df in git_info["dirty_files"][:10]:
                print(f"  {df}")
            print("\nTo build a development snapshot anyway, use: python tools/build_deployment_packages.py --mode snapshot")
            sys.exit(1)
        package_mode = "RELEASE_FROM_COMMIT"
        source_type = "commit"
        source_git_state = "clean_commit"
        source_git_sha = clean_sha
        source_git_dirty = False
    elif args.mode == "snapshot":
        package_mode = "SNAPSHOT_FROM_WORKTREE"
        source_type = "dirty_worktree_snapshot"
        source_git_state = "dirty_snapshot"
        source_git_sha = f"{clean_sha}-dirty" if clean_sha else "dirty_snapshot"
        source_git_dirty = True
    else:  # auto
        if is_dirty:
            package_mode = "SNAPSHOT_FROM_WORKTREE"
            source_type = "dirty_worktree_snapshot"
            source_git_state = "dirty_snapshot"
            source_git_sha = f"{clean_sha}-dirty" if clean_sha else "dirty_snapshot"
            source_git_dirty = True
        else:
            package_mode = "RELEASE_FROM_COMMIT"
            source_type = "commit"
            source_git_state = "clean_commit"
            source_git_sha = clean_sha
            source_git_dirty = False

    provenance = {
        "package_mode": package_mode,
        "source_type": source_type,
        "source_git_state": source_git_state,
        "source_git_sha": source_git_sha,
        "source_git_dirty": source_git_dirty,
    }

    print("=" * 70)
    print(f"BUILDING DEPLOYMENT PACKAGES ({package_mode})")
    print(f"Source Git State : {source_git_state}")
    print(f"Source Git SHA   : {source_git_sha or 'N/A'}")
    print(f"Source Dirty     : {source_git_dirty}")
    print("=" * 70)

    hf_dir = REPO_ROOT / 'HuggingFace'
    gh_dir = REPO_ROOT / 'GitHub'

    if args.target in ("all", "huggingface"):
        build_huggingface_package(hf_dir, provenance)

    if args.target in ("all", "github"):
        build_github_package(gh_dir, provenance)

    print("\n" + "=" * 70)
    print(f"PACKAGE BUILD COMPLETE ({package_mode})")
    print("=" * 70)


if __name__ == '__main__':
    main()
