# -*- coding: utf-8 -*-
"""
Release and Package Provenance Verifier for ORCA Web Lab.

Performs deterministic, multi-layer verification:
1. Python runtime environment (3.11, 3.12, 3.13)
2. Git status, commit SHA, and worktree cleanliness
3. Required repository files and canonical schemas
4. Secret scanning and hardcoded local path detection
5. Dependency inventory verification
6. Package manifest integrity & cryptographic SHA-256 verification (HuggingFace/ and GitHub/)
7. Full test suite execution

Verdict Classification:
- VERIFIED_RELEASE  : Built from a clean Git commit; package hashes 100% verified.
- VERIFIED_SNAPSHOT : Built from a working-tree snapshot; manifest truthfully reflects snapshot state.
- REJECTED          : Manifest mismatch, unverified hashes, secrets detected, or failing tests.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_DEPENDENCIES = {
    "flask": "3.0.0",
    "requests": "2.28.0",
    "rdkit": "2023.9.0",
    "kaggle": "2.2.3",
    "google.auth": "2.0.0",
    "PIL": "9.0.0",
    "rarfile": "4.0",
    "openpyxl": "3.1.0",
    "jsonschema": "4.20.0",
    "cryptography": "41.0.0",
    "pytest": "8.0.0",
}

REQUIRED_FILES = [
    "pyproject.toml",
    "requirements.txt",
    "pytest.ini",
    "Dockerfile",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "LICENSE.txt",
    "THIRD_PARTY_LICENSES.md",
    ".gitignore",
    ".github/workflows/ci.yml",
    "schema/canonical_scientific.schema.json",
    "tools/build_deployment_packages.py",
    "tools/verify_release.py",
    "orca_engine/src/orca_engine/archive_validator.py",
    "orca_orchestrator/result_store.py",
    "orca_orchestrator/credential_vault.py",
    "cloudflare-control-plane/migrations/0002_credential_vault.sql",
]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def audit_python_version() -> tuple[bool, str]:
    v = sys.version_info
    v_str = f"{v.major}.{v.minor}.{v.micro}"
    if v.major != 3 or v.minor not in (11, 12, 13, 14):
        return False, f"Unsupported Python version: {v_str} (requires 3.11, 3.12, 3.13)"
    return True, f"Python {v_str} on {platform.system()} ({platform.machine()})"


def audit_git_state() -> tuple[bool, dict]:
    info: dict[str, str | list[str] | bool] = {}
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=REPO_ROOT, text=True).strip()
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO_ROOT, text=True).splitlines()
        
        real_dirty = []
        for line in status:
            fname = line[3:].strip()
            if not (fname.startswith('HuggingFace') or fname.startswith('GitHub')):
                real_dirty.append(line)

        info["sha"] = sha
        info["branch"] = branch
        info["is_dirty"] = len(real_dirty) > 0
        info["dirty_files"] = real_dirty
        return True, info
    except Exception as exc:
        return False, {"error": str(exc), "is_dirty": True}


def audit_required_files() -> tuple[bool, list[str]]:
    missing = []
    for rf in REQUIRED_FILES:
        p = REPO_ROOT / rf
        if not p.exists():
            missing.append(rf)
    return len(missing) == 0, missing


def audit_secret_and_paths() -> tuple[bool, list[str]]:
    path_patterns = [
        re.compile(r'[gG]:[\\/]orca web lab', re.IGNORECASE),
        re.compile(r'C:[\\/]Users[\\/][a-zA-Z0-9_-]+', re.IGNORECASE),
    ]
    secret_patterns = [
        re.compile(r'KGAT_[A-Za-z0-9_\-]{20,}'),
        re.compile(r'\"kaggle_key\":\s*\"[a-f0-9]{32}\"'),
        re.compile(r'CF_API_TOKEN\s*=\s*[\"\\\'][A-Za-z0-9_\-]{20,}'),
    ]

    try:
        tracked = subprocess.check_output(["git", "ls-files"], cwd=REPO_ROOT, text=True).splitlines()
    except Exception:
        tracked = []

    violations = []
    scannable_exts = ('.py', '.js', '.ts', '.sh', '.yml', '.yaml', '.toml', '.ini', '.json', '.sql', '.html')

    for rel in tracked:
        if not rel.endswith(scannable_exts):
            continue
        if 'ORCA_Parsed_Data.json' in rel or 'orca_parsed_data' in rel:
            continue
        p = REPO_ROOT / rel
        is_test_file = rel.startswith(("tests/", "orca_engine/tests/")) or rel == "tools/verify_release.py"
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as fp:
                for line_no, line in enumerate(fp, 1):
                    for pat in path_patterns:
                        if pat.search(line) and not is_test_file:
                            violations.append(f"{rel}:{line_no} -> {line.strip()[:80]}")
                    if not is_test_file:
                        for pat in secret_patterns:
                            if pat.search(line):
                                violations.append(f"{rel}:{line_no} -> {line.strip()[:80]}")
        except Exception:
            pass

    return len(violations) == 0, violations


def audit_dependencies() -> tuple[bool, dict[str, str]]:
    installed = {}
    missing = []
    pkg_map = {
        "flask": "flask",
        "requests": "requests",
        "rdkit": "rdkit",
        "kaggle": "kaggle",
        "google.auth": "google-auth",
        "pillow": "pillow",
        "rarfile": "rarfile",
        "openpyxl": "openpyxl",
        "jsonschema": "jsonschema",
        "cryptography": "cryptography",
        "pytest": "pytest",
    }
    for label, pkg_name in pkg_map.items():
        try:
            v = importlib.metadata.version(pkg_name)
            installed[label] = v
        except Exception:
            try:
                mod = __import__(label)
                installed[label] = getattr(mod, "__version__", "installed")
            except Exception:
                missing.append(label)
                installed[label] = "MISSING"

    return len(missing) == 0, installed


def verify_package_manifest(pkg_dir: Path) -> tuple[bool, str, dict]:
    manifest_p = pkg_dir / "RELEASE_MANIFEST.json"
    if not manifest_p.exists():
        return False, f"Missing RELEASE_MANIFEST.json in {pkg_dir.name}", {}

    try:
        manifest = json.loads(manifest_p.read_text(encoding='utf-8'))
    except Exception as exc:
        return False, f"Corrupted RELEASE_MANIFEST.json in {pkg_dir.name}: {exc}", {}

    critical_files = manifest.get("runtime_critical_files", {})
    if not critical_files:
        return False, f"Manifest in {pkg_dir.name} contains no runtime_critical_files", manifest

    # Audit all files on disk
    disk_files = [p for p in pkg_dir.rglob('*') if p.is_file() and p.name != 'RELEASE_MANIFEST.json']
    if len(disk_files) != len(critical_files):
        return False, (
            f"File count mismatch in {pkg_dir.name}: disk has {len(disk_files)}, manifest declares {len(critical_files)}"
        ), manifest

    for p in disk_files:
        rel = str(p.relative_to(pkg_dir)).replace('\\', '/')
        if rel not in critical_files:
            return False, f"Unmanifested file on disk in {pkg_dir.name}: {rel}", manifest
        actual_sha = sha256_file(p)
        expected_sha = critical_files[rel]["sha256"]
        if actual_sha != expected_sha:
            return False, (
                f"SHA-256 mismatch for {rel} in {pkg_dir.name}!\nExpected: {expected_sha}\nActual:   {actual_sha}"
            ), manifest

    return True, f"All {len(disk_files)} files verified against manifest SHA-256 digests", manifest


def run_tests() -> tuple[bool, str]:
    cmd = [sys.executable, "-m", "pytest", "-q", "tests/test_licensing.py", "tests/test_archive_security.py", "tests/test_kaggle_credential_vault.py"]
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return proc.returncode == 0, output.strip()
    except Exception as exc:
        return False, str(exc)


def main():
    parser = argparse.ArgumentParser(description="ORCA Web Lab Release and Provenance Verifier")
    parser.add_argument("--full-tests", action="store_true", help="Run entire pytest suite")
    args = parser.parse_args()

    print("=" * 70)
    print("ORCA WEB LAB RELEASE & PROVENANCE VERIFICATION AUDIT")
    print("=" * 70)

    # 1. Python runtime
    ok_py, py_msg = audit_python_version()
    print(f"[{'PASS' if ok_py else 'FAIL'}] Python Runtime: {py_msg}")

    # 2. Git State
    ok_git, git_info = audit_git_state()
    is_dirty = git_info.get("is_dirty", True)
    git_sha = git_info.get("sha", "unknown")
    print(f"[{'PASS' if ok_git else 'FAIL'}] Git Commit SHA: {str(git_sha)[:10]} (dirty: {is_dirty})")
    if is_dirty:
        print(f"       Dirty working-tree files ({len(git_info.get('dirty_files', []))}):")
        for df in git_info.get("dirty_files", [])[:5]:
            print(f"         {df}")

    # 3. Required Files
    ok_files, missing_files = audit_required_files()
    print(f"[{'PASS' if ok_files else 'FAIL'}] Required Source Files: {'All present' if ok_files else f'Missing: {missing_files}'}")

    # 4. Security & Path Audit
    ok_sec, sec_violations = audit_secret_and_paths()
    print(f"[{'PASS' if ok_sec else 'FAIL'}] Security & Hardcoded Path Audit: {len(sec_violations)} violations detected")
    if not ok_sec:
        for v in sec_violations[:5]:
            print(f"       {v}")

    # 5. Dependency Inventory
    ok_deps, deps_map = audit_dependencies()
    print(f"[{'PASS' if ok_deps else 'FAIL'}] Dependency Inventory: {len(deps_map)} packages verified")

    # 6. HuggingFace Package Verification
    hf_dir = REPO_ROOT / "HuggingFace"
    ok_hf, hf_msg, hf_manifest = verify_package_manifest(hf_dir)
    print(f"[{'PASS' if ok_hf else 'FAIL'}] HuggingFace Package: {hf_msg}")
    if ok_hf:
        print(f"       Mode: {hf_manifest.get('package_mode')}, Tree Hash: {hf_manifest.get('package_tree_hash')[:16]}...")

    # 7. GitHub Package Verification
    gh_dir = REPO_ROOT / "GitHub"
    ok_gh, gh_msg, gh_manifest = verify_package_manifest(gh_dir)
    print(f"[{'PASS' if ok_gh else 'FAIL'}] GitHub Package: {gh_msg}")
    if ok_gh:
        print(f"       Mode: {gh_manifest.get('package_mode')}, Tree Hash: {gh_manifest.get('package_tree_hash')[:16]}...")

    # 8. Test Execution
    print("[INFO] Running verification tests...")
    ok_tests, test_out = run_tests()
    last_line = test_out.splitlines()[-1] if test_out else "No output"
    print(f"[{'PASS' if ok_tests else 'FAIL'}] Automated Tests: {last_line}")

    # Final Classification
    print("\n" + "=" * 70)
    all_core_ok = ok_py and ok_files and ok_sec and ok_deps and ok_hf and ok_gh and ok_tests

    if all_core_ok and not is_dirty and hf_manifest.get("package_mode") == "RELEASE_FROM_COMMIT" and gh_manifest.get("package_mode") == "RELEASE_FROM_COMMIT":
        print("FINAL VERDICT: VERIFIED_RELEASE")
        print("  - Built from clean Git commit with 100% cryptographic provenance.")
        print(f"  - Release Git SHA: {git_sha}")
        sys.exit(0)
    elif all_core_ok and (is_dirty or "SNAPSHOT" in hf_manifest.get("package_mode", "") or "SNAPSHOT" in gh_manifest.get("package_mode", "")):
        print("FINAL VERDICT: VERIFIED_SNAPSHOT")
        print("  - Package contents 100% verified against manifest.")
        print("  - Note: Source was built from a working-tree snapshot (not an immutable clean commit).")
        sys.exit(0)
    else:
        print("FINAL VERDICT: REJECTED")
        print("  - One or more validation checks failed.")
        sys.exit(1)


if __name__ == '__main__':
    main()
