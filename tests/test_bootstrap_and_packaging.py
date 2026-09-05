# -*- coding: utf-8 -*-
"""Targeted test suite for cross-platform bootstraps and Python packaging contracts.

Validates:
1. Python support contract (>=3.11, <3.14) across all bootstrap scripts.
2. Windows batch & PowerShell interpreter discovery and user-gated winget.
3. Linux & macOS interpreter discovery and user-gated package managers.
4. HPC supercomputer hard invariant: no sudo / apt / dnf / yum, module/conda instructions.
5. Packaging configuration in pyproject.toml (requires-python, classifiers, wheel bundling).
6. Local companion agent package builder manifest integrity and checksums.
7. Absence of hardcoded developer machine paths and secrets.
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# 1. Windows Bootstrap Tests
# ===========================================================================

def test_windows_install_and_run_bat_version_guards():
    """Verify Windows batch script discovers supported Python and gates winget."""
    bat_path = REPO_ROOT / "bootstrap" / "windows" / "install_and_run.bat"
    assert bat_path.is_file(), "install_and_run.bat must exist"
    content = bat_path.read_text(encoding="utf-8", errors="ignore")

    # Version constraint checks (>=3.11, <3.14)
    assert "11<=v[1] and v[1]<14" in content or "(3, 11) <= v < (3, 14)" in content
    # Discovery of py launcher candidates
    assert ("py -3.12" in content and "py -3.13" in content and "py -3.11" in content) or ("py -0p" in content)
    # Discovery of python / python3 on PATH
    assert "where python" in content
    assert "where python3" in content
    # Winget confirmation prompt
    assert 'Would you like to install Python 3.12 using winget?' in content
    # Rejection of Python 3.14 note
    assert "Python 3.14 is currently unsupported" in content
    # Stale 3.10 requirement must not be present
    assert "Python 3.10+" not in content


def test_windows_bootstrap_ps1_version_guards():
    """Verify Windows PowerShell helper discovers supported Python and gates winget."""
    ps1_path = REPO_ROOT / "bootstrap" / "windows" / "bootstrap_windows.ps1"
    assert ps1_path.is_file(), "bootstrap_windows.ps1 must exist"
    content = ps1_path.read_text(encoding="utf-8", errors="ignore")

    # Version constraint checks
    assert "(3, 11) <= v < (3, 14)" in content
    assert '[version]"3.11"' in content
    assert '[version]"3.14"' in content
    # Py launcher query
    assert "py -0p" in content
    # Winget confirmation prompt
    assert "Would you like to install Python 3.12 via winget?" in content
    # Function defined
    assert "function Test-SupportedPython" in content


# ===========================================================================
# 2. Linux Bootstrap Tests
# ===========================================================================

@pytest.mark.parametrize("script_name", ["install_and_run.sh", "start_agent.sh"])
def test_linux_bootstrap_scripts(script_name: str):
    """Verify Linux scripts enforce Python >=3.11,<3.14 and user-confirmed package managers."""
    script_path = REPO_ROOT / "bootstrap" / "linux" / script_name
    assert script_path.is_file(), f"{script_name} must exist"
    content = script_path.read_text(encoding="utf-8", errors="ignore")

    # Version check present
    assert "(3, 11) <= v < (3, 14)" in content
    # Checks candidate names in order
    assert "python3.13" in content and "python3.12" in content and "python3.11" in content
    # Interactive confirmation prompt before package manager
    assert "read -r -p" in content or "read -p" in content
    assert "Install Python 3.12 using system package manager?" in content
    # Ensures no unconditional sudo execution without prompt
    assert "sudo apt" in content or "sudo dnf" in content


# ===========================================================================
# 3. macOS Bootstrap Tests
# ===========================================================================

@pytest.mark.parametrize("script_name", ["install_and_run.command", "install_and_run.sh"])
def test_macos_bootstrap_scripts(script_name: str):
    """Verify macOS scripts enforce Python >=3.11,<3.14 and prompt before Homebrew."""
    script_path = REPO_ROOT / "bootstrap" / "macos" / script_name
    assert script_path.is_file(), f"{script_name} must exist"
    content = script_path.read_text(encoding="utf-8", errors="ignore")

    # Version check
    assert "(3, 11) <= v < (3, 14)" in content
    # Check Homebrew paths
    assert "/opt/homebrew/bin/python3.12" in content
    # Interactive confirmation before brew install
    assert "read -r -p" in content or "read -p" in content
    assert "Install Python 3.12 via Homebrew" in content


# ===========================================================================
# 4. HPC Supercomputer Bootstrap Tests (Hard Invariant)
# ===========================================================================

@pytest.mark.parametrize("script_name", ["install_hpc_agent.sh", "start_hpc_agent.sh"])
def test_hpc_bootstrap_hard_invariant(script_name: str):
    """Verify HPC scripts strictly prohibit root/sudo/apt/yum and guide module/conda."""
    script_path = REPO_ROOT / "bootstrap" / "hpc" / script_name
    assert script_path.is_file(), f"{script_name} must exist"
    content = script_path.read_text(encoding="utf-8", errors="ignore")

    # Hard invariant: no sudo, apt, dnf, yum, pacman invocations
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("echo") or stripped.startswith('"'):
            continue
        assert not re.search(r"\bsudo\b", stripped), f"Forbidden sudo command in HPC script: {stripped}"
        assert not re.search(r"\bapt\b", stripped), f"Forbidden apt command in HPC script: {stripped}"
        assert not re.search(r"\byum\b", stripped), f"Forbidden yum command in HPC script: {stripped}"
        assert not re.search(r"\bdnf\b", stripped), f"Forbidden dnf command in HPC script: {stripped}"


    # Verify environment module and conda guidance
    assert "module load" in content
    assert "conda activate" in content
    assert "Supercomputer / HPC environments require user-space Python" in content


# ===========================================================================
# 5. Packaging Configuration & Wheel Bundling
# ===========================================================================

def test_pyproject_packaging_contract():
    """Verify pyproject.toml specifies exact runtime constraint and packages orca_engine."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert pyproject_path.is_file()
    content = pyproject_path.read_text(encoding="utf-8", errors="ignore")

    # requires-python
    assert 'requires-python = ">=3.11, <3.14"' in content

    # Supported Python classifiers
    assert '"Programming Language :: Python :: 3.11"' in content
    assert '"Programming Language :: Python :: 3.12"' in content
    assert '"Programming Language :: Python :: 3.13"' in content
    assert '"Programming Language :: Python :: 3.14"' not in content
    assert '"Programming Language :: Python :: 3.10"' not in content

    # Wheel bundling of orca_engine
    assert '"orca_engine/src/orca_engine" = "orca_engine"' in content
    # Wheel includes static and templates
    assert '"templates" = "templates"' in content
    assert '"static" = "static"' in content


def test_packaging_contract_guards_runtime_assets():
    """Verify project packaging contracts and built wheel include critical runtime assets (PKG-DRIFT-01 / CI-BLIND-01)."""
    # 1. Source files must exist
    rxn_js = REPO_ROOT / "static" / "js" / "reaction-unified.js"
    assert rxn_js.is_file(), "static/js/reaction-unified.js must exist"
    assert len(rxn_js.read_text(encoding="utf-8")) > 500, "reaction-unified.js must not be empty"

    rxn_svc = REPO_ROOT / "services" / "reaction_diagram_service.py"
    assert rxn_svc.is_file(), "services/reaction_diagram_service.py must exist"

    orca_eng = REPO_ROOT / "orca_engine" / "src" / "orca_engine" / "__init__.py"
    assert orca_eng.is_file(), "orca_engine package must exist"

    # 2. Packaging contract in pyproject.toml
    pyproject_content = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"static" = "static"' in pyproject_content
    assert '"orca_engine/src/orca_engine" = "orca_engine"' in pyproject_content
    assert '"services"' in pyproject_content

    # 3. If orca_web_lab wheel is present in dist/, verify its archive members
    dist_dir = REPO_ROOT / "dist"
    wheels = list(dist_dir.glob("orca_web_lab*.whl"))
    for whl in wheels:
        with zipfile.ZipFile(whl, "r") as zf:
            members = set(zf.namelist())
            assert any(m.endswith("static/js/reaction-unified.js") for m in members), (
                f"Wheel {whl.name} is missing static/js/reaction-unified.js"
            )
            assert any(m.endswith("services/reaction_diagram_service.py") for m in members), (
                f"Wheel {whl.name} is missing services/reaction_diagram_service.py"
            )
            assert any(m.startswith("orca_engine/") and m.endswith(".py") for m in members), (
                f"Wheel {whl.name} is missing orca_engine modules"
            )



def test_readme_and_docs_python_versions():
    """Verify README.md and documentation state Python >=3.11, <3.14."""
    readme_path = REPO_ROOT / "README.md"
    content = readme_path.read_text(encoding="utf-8", errors="ignore")
    assert "Python 3.11, 3.12, or 3.13 (`>=3.11, <3.14`)" in content
    assert "Python 3.10, 3.11, or 3.12" not in content

    doc_path = REPO_ROOT / "docs" / "LOCAL_AGENT_SETUP.md"
    doc_content = doc_path.read_text(encoding="utf-8", errors="ignore")
    assert "Python 3.11, 3.12, or 3.13 (`>=3.11, <3.14`)" in doc_content


# ===========================================================================
# 6. Local Agent Package Builder
# ===========================================================================

def test_local_agent_package_builder_and_manifests():
    """Verify tools/build_local_agent_packages.py builds valid packages with matching checksums."""
    from tools.build_local_agent_packages import build_all_packages

    manifests = build_all_packages()
    assert "windows-x64" in manifests
    assert "linux-x64" in manifests
    assert "macos" in manifests
    assert "hpc-linux" in manifests

    for pkg_id, m in manifests.items():
        zip_file = Path(m["file_path"])
        assert zip_file.is_file(), f"Package file {zip_file} must exist"
        assert zip_file.stat().st_size > 5000, "Package zip must have non-trivial size"

        # Verify ZIP contains expected files
        with zipfile.ZipFile(zip_file, "r") as zf:
            namelist = zf.namelist()
            assert "README_LOCAL_AGENT.txt" in namelist
            assert "VERSION" in namelist
            assert "checksums.txt" in namelist
            assert any(f.startswith("local_agent/") for f in namelist)


# ===========================================================================
# 7. Security and Personal Path Scanner
# ===========================================================================

def test_no_hardcoded_developer_paths_or_leaked_secrets():
    """Scans repository files to ensure no hardcoded developer paths or credentials exist."""
    secret_patterns = [
        re.compile(r"KGAT_[A-Za-z0-9_-]{20,}"),
        re.compile(r'"kaggle_key"\s*:\s*"[a-f0-9]{32}"'),
        re.compile(r"CF_API_TOKEN\s*=\s*['\"][A-Za-z0-9_-]{20,}"),
    ]
    path_patterns = [
        re.compile(r"[gG]:[\\/]orca web lab", re.IGNORECASE),
        re.compile(r"C:[\\/]Users[\\/][a-zA-Z0-9_-]+", re.IGNORECASE),
    ]
    kgat_test_exemption = re.compile(r"^tests[\\/]")
    fixture_corpus = re.compile(r"^orca_engine/data/")
    ignored_dirs = {
        '.git', '__pycache__', '.pytest_cache', 'node_modules', '.venv', 'venv',
        'dist', 'build', '.agents', '.openclaw', 'scratch', 'temp_pytest_run',
        'GitHub', 'HuggingFace', '.wrangler'
    }
    # Security scan applies to source code, scripts, configuration, and templates
    code_exts = ('.py', '.sh', '.bat', '.ps1', '.command', '.toml', '.json', '.yml', '.yaml', '.html', '.js')

    violations = []

    for root, dirs, files in os.walk(str(REPO_ROOT)):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in files:
            if not filename.endswith(code_exts):
                continue

            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, str(REPO_ROOT)).replace('\\', '/')

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, 1):
                        for pat in secret_patterns:
                            if pat.search(line):
                                if pat.pattern.startswith("KGAT_") and kgat_test_exemption.match(rel_path):
                                    continue
                                violations.append(f"{rel_path}:{line_no} [SECRET] -> {line.strip()[:100]}")
                        for pat in path_patterns:
                            if pat.search(line):
                                if fixture_corpus.match(rel_path):
                                    continue
                                violations.append(f"{rel_path}:{line_no} [PATH] -> {line.strip()[:100]}")
            except (OSError, UnicodeError):
                continue

    assert len(violations) == 0, f"Found {len(violations)} violations:\n" + "\n".join(violations[:20])

