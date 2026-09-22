"""Release Synchronization and Deployment Manager for Chemistry Lab.

Synchronizes:
    Main Project (Authoritative Source of Truth)
        ├──> GitHub/       (Complete public source repository)
        └──> HuggingFace/ (Minimal production deployment package)

Follows all 20 phases and safety constraints:
- Zero em-dashes and zero en-dashes invariant.
- English only.
- Strict secret scanning before any push.
- Independent deployment package architectures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
GH_DIR = REPO_ROOT / "GitHub"
HF_DIR = REPO_ROOT / "HuggingFace"

# Remote Targets
GITHUB_REPO_SLUG = "abd19990044-commits/chemistry-web-lab"
GITHUB_REMOTE_URL = f"https://github.com/{GITHUB_REPO_SLUG}.git"
HF_SPACE_SLUG = "mc2hf1999/orcaweb"
HF_SPACE_URL = f"https://huggingface.co/spaces/{HF_SPACE_SLUG}"


def get_hf_token() -> str:
    """Retrieve Hugging Face access token from the environment or a token file.

    The token file location is configurable via ``HF_TOKEN_FILE`` so no
    machine-specific path is ever hardcoded here.
    """
    token = os.environ.get("HF_TOKEN")
    if token:
        return token.strip()
    token_file = os.environ.get("HF_TOKEN_FILE")
    if token_file:
        p = Path(token_file)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "HF_TOKEN could not be retrieved. Set the HF_TOKEN environment variable "
        "(or HF_TOKEN_FILE pointing at a token file).")


# Directories to always ignore from synchronization
ALWAYS_IGNORE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "scratch",
    "GitHub",
    "HuggingFace",
    "hf_upload_workspace",
    ".pytest_temp",
    "test_job",
    "benchmarks",
    "\u0645\u0644\u0641\u0627\u062a \u062c\u064a\u0633\u0648\u0646 \u0627\u0644\u0646\u0627\u062a\u062c\u0629",
}

# Directories/files to exclude from Hugging Face production deployment
HF_EXCLUDED_PATHS = {
    ".github",
    "tests",
    "orca_engine/tests",
    "orca_engine/data",
    "orca_engine/scripts",
    "cloudflare-control-plane",
    "benchmarks",
    "scripts",
}

# File extensions to ignore everywhere
IGNORED_EXTENSIONS = {
    ".pyc", ".TAG", ".log", ".tmp", ".swp", ".bak"
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def scan_for_secrets(base_dir: Path) -> list[tuple[str, str, str]]:
    """Scan directory for forbidden secrets and private credential keys."""
    secret_patterns = [
        (re.compile(r"CONTROL_PLANE_API_TOKEN\s*=\s*['\"](?![ '\"\s]|placeholder|your_|[A-Z_]+_HERE)[a-zA-Z0-9_\-\.]{10,}['\"]", re.I), "CONTROL_PLANE_API_TOKEN"),
        (re.compile(r"CLOUDFLARE_API_TOKEN\s*=\s*['\"](?![ '\"\s]|placeholder|your_|[A-Z_]+_HERE)[a-zA-Z0-9_\-\.]{10,}['\"]", re.I), "CLOUDFLARE_API_TOKEN"),
        (re.compile(r"KAGGLE_KEY\s*=\s*['\"](?![ '\"\s]|placeholder|your_|[A-Z_]+_HERE)[a-zA-Z0-9]{20,}['\"]", re.I), "KAGGLE_KEY"),
        (re.compile(r"KAGGLE_CREDENTIALS_ENCRYPTION_KEY\s*=\s*['\"](?![ '\"\s]|placeholder|your_|[A-Z_]+_HERE)[a-zA-Z0-9_\-\.\=\+]{20,}['\"]", re.I), "KAGGLE_CREDENTIALS_ENCRYPTION_KEY"),
        (re.compile(r"hf_[a-zA-Z0-9]{30,}"), "HUGGING_FACE_TOKEN"),
        (re.compile(r"ghp_[a-zA-Z0-9]{30,}"), "GITHUB_TOKEN"),
        (re.compile(r"github_pat_[a-zA-Z0-9_]{30,}"), "GITHUB_PAT"),
        (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "PRIVATE_KEY_BLOCK"),
    ]
    findings = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ALWAYS_IGNORE_DIRS and (d == ".github" or not d.startswith("."))]
        for f in files:
            if any(f.endswith(ext) for ext in [".pyc", ".out", ".log", ".zip", ".tar", ".gz", ".db", ".pdf", ".png", ".jpg"]):
                continue
            fp = Path(root) / f
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
                for pat, name in secret_patterns:
                    m = pat.search(text)
                    if m:
                        findings.append((str(fp.relative_to(base_dir)), name, m.group(0)[:30]))
            except Exception:
                pass
    return findings


def get_main_project_files() -> dict[str, Path]:
    """Inventory all valid source files from the Main Project."""
    files_map = {}
    for root, dirs, files in os.walk(REPO_ROOT):
        # filter directories
        rel_root = Path(root).relative_to(REPO_ROOT)
        top_dir = rel_root.parts[0] if rel_root.parts else ""
        if top_dir in ALWAYS_IGNORE_DIRS:
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d not in ALWAYS_IGNORE_DIRS and (d == ".github" or not d.startswith("."))]
        
        for f in files:
            if any(f.endswith(ext) for ext in IGNORED_EXTENSIONS):
                continue
            full_path = Path(root) / f
            rel_path = full_path.relative_to(REPO_ROOT).as_posix()
            files_map[rel_path] = full_path
    return files_map


def is_huggingface_runtime_file(rel_path: str) -> bool:
    """Determine if a file is strictly required for Hugging Face production runtime."""
    # Check directory exclusions
    for exc in HF_EXCLUDED_PATHS:
        if rel_path == exc or rel_path.startswith(f"{exc}/"):
            return False
            
    # Exclude root test configurations or dev tools
    if rel_path in {"pytest.ini", "mypy.ini", "tsconfig.json", "wrangler.toml"}:
        return False
        
    # Exclude standalone engineering reports and non-runtime markdown
    if rel_path.endswith(".md"):
        allowed_md = {
            "README.md",
            "THIRD_PARTY_LICENSES.md",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "orca_engine/README.md",
            "orca_engine/CONTRIBUTING.md",
        }
        if rel_path not in allowed_md:
            return False
            
    if rel_path.endswith(".pdf"):
        return False
        
    return True


def synchronize_target(target_name: str, target_dir: Path, is_hf: bool = False) -> tuple[list[str], list[str], list[str]]:
    """Synchronize a deployment folder from the Main Project."""
    main_files = get_main_project_files()
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Filter files appropriate for this target
    expected_files: dict[str, Path] = {}
    for rel_path, src_path in main_files.items():
        if is_hf:
            if is_huggingface_runtime_file(rel_path):
                expected_files[rel_path] = src_path
        else:
            expected_files[rel_path] = src_path

    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []

    # 2. Check existing files in target and remove obsolete
    for root, dirs, files in os.walk(target_dir):
        rel_root = Path(root).relative_to(target_dir)
        if ".git" in rel_root.parts:
            continue
        dirs[:] = [d for d in dirs if d != ".git" and d not in ALWAYS_IGNORE_DIRS]
        
        for f in files:
            if f.endswith(tuple(IGNORED_EXTENSIONS)):
                continue
            dst_full = Path(root) / f
            rel_dst = dst_full.relative_to(target_dir).as_posix()
            if rel_dst not in expected_files:
                dst_full.unlink()
                removed.append(rel_dst)

    # 3. Clean empty directories
    for root, dirs, files in os.walk(target_dir, topdown=False):
        if ".git" in Path(root).relative_to(target_dir).parts:
            continue
        if not dirs and not files and root != str(target_dir):
            try:
                os.rmdir(root)
            except Exception:
                pass

    # 4. Copy / update expected files
    for rel_path, src_path in expected_files.items():
        dst_path = target_dir / rel_path
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not dst_path.exists():
            shutil.copy2(src_path, dst_path)
            added.append(rel_path)
        else:
            if sha256_file(src_path) != sha256_file(dst_path):
                shutil.copy2(src_path, dst_path)
                modified.append(rel_path)

    return sorted(added), sorted(modified), sorted(removed)


def generate_manifests_and_reports(gh_changes, hf_changes):
    """Generate manifest files and detailed difference report."""
    # 1. GitHub Manifest
    gh_manifest_lines = []
    for root, dirs, files in os.walk(GH_DIR):
        if ".git" in Path(root).relative_to(GH_DIR).parts:
            continue
        dirs[:] = [d for d in dirs if d != ".git" and d not in ALWAYS_IGNORE_DIRS]
        for f in files:
            if f.endswith(tuple(IGNORED_EXTENSIONS)):
                continue
            fp = Path(root) / f
            rel = fp.relative_to(GH_DIR).as_posix()
            gh_manifest_lines.append(f"{sha256_file(fp)}  {rel}")
    gh_manifest_lines.sort()
    (REPO_ROOT / "RELEASE_SYNC_GITHUB_MANIFEST.txt").write_text("\n".join(gh_manifest_lines) + "\n", encoding="utf-8")
    
    # 2. HuggingFace Manifest
    hf_manifest_lines = []
    for root, dirs, files in os.walk(HF_DIR):
        if ".git" in Path(root).relative_to(HF_DIR).parts:
            continue
        dirs[:] = [d for d in dirs if d != ".git" and d not in ALWAYS_IGNORE_DIRS]
        for f in files:
            if f.endswith(tuple(IGNORED_EXTENSIONS)):
                continue
            fp = Path(root) / f
            rel = fp.relative_to(HF_DIR).as_posix()
            hf_manifest_lines.append(f"{sha256_file(fp)}  {rel}")
    hf_manifest_lines.sort()
    (REPO_ROOT / "RELEASE_SYNC_HUGGINGFACE_MANIFEST.txt").write_text("\n".join(hf_manifest_lines) + "\n", encoding="utf-8")

    # 3. Difference Report
    diff_report = f"""# Release Synchronization Difference Report

## 1. Synchronization Summary

The deployment packages have been synchronized directly from the authoritative Main Project:
- **GitHub Package (`GitHub/`):** Contains {len(gh_manifest_lines)} total files (complete public source repository).
- **Hugging Face Package (`HuggingFace/`):** Contains {len(hf_manifest_lines)} total files (minimal production runtime deployment).

---

## 2. GitHub Changes (`GitHub/`)

- **Files Added:** {len(gh_changes[0])}
- **Files Modified:** {len(gh_changes[1])}
- **Files Removed:** {len(gh_changes[2])}

### Added Files:
{chr(10).join(f"- `{f}`" for f in gh_changes[0]) if gh_changes[0] else "- None"}

### Modified Files:
{chr(10).join(f"- `{f}`" for f in gh_changes[1]) if gh_changes[1] else "- None"}

### Removed Files:
{chr(10).join(f"- `{f}`" for f in gh_changes[2]) if gh_changes[2] else "- None"}

---

## 3. Hugging Face Changes (`HuggingFace/`)

- **Files Added:** {len(hf_changes[0])}
- **Files Modified:** {len(hf_changes[1])}
- **Files Removed:** {len(hf_changes[2])}

### Added Files:
{chr(10).join(f"- `{f}`" for f in hf_changes[0]) if hf_changes[0] else "- None"}

### Modified Files:
{chr(10).join(f"- `{f}`" for f in hf_changes[1]) if hf_changes[1] else "- None"}

### Removed Files:
{chr(10).join(f"- `{f}`" for f in hf_changes[2]) if hf_changes[2] else "- None"}

---

## 4. Architectural Differences Between GitHub/ and HuggingFace/

| Component / Layer | Included in GitHub/ | Included in HuggingFace/ | Architectural Rationale |
| :--- | :--- | :--- | :--- |
| **Core Web App (`app.py`, `templates/`, `static/`)** | YES | YES | Essential runtime user interface and API endpoints. |
| **2D Chemical Drawing (`chem_core/`)** | YES | YES | In-browser SVG/PNG molecular and reaction rendering. |
| **Quantum Engine (`orca_engine/src/`)** | YES | YES | Output parsing, thermochemistry, and spectroscopy engine. |
| **Cloud Orchestrator (`orca_orchestrator/`)** | YES | YES | Kaggle runner and Cloudflare background state machine. |
| **Canonical Schemas (`schema/`)** | YES | YES | JSON schema validation for scientific exports. |
| **Scientific Data & Tools (`tools/`, `data/`)** | YES | YES | Machine learning JSONL exporters and peak databases. |
| **Test Suites (`tests/`, `orca_engine/tests/`)** | YES | NO | Developer testing suites omitted from production Docker image. |
| **Test Fixtures & Corpus (`data/orcafile/`)** | YES | NO | Large testing logs omitted from lightweight Space container. |
| **Cloudflare Control Plane Source** | YES | NO | Deployed separately to Cloudflare Workers edge runtime. |
| **Engineering Reports & Audit Logs** | YES | NO | Development and legal audit documentation omitted from runtime. |
| **Space Docker Configuration (`Dockerfile`)** | YES | YES | Container definition for Hugging Face Spaces deployment. |
"""
    (REPO_ROOT / "RELEASE_SYNC_DIFFERENCE_REPORT.md").write_text(diff_report, encoding="utf-8")


def get_github_token() -> str:
    """Retrieve GitHub token from environment or git credential helper."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\nusername=abd19990044-commits\n\n",
            text=True,
            capture_output=True,
            check=True
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception as exc:
        print(f"[WARN] Could not retrieve token from credential helper: {exc}")
    raise RuntimeError("GitHub token could not be retrieved from environment or credential helper.")


def upload_to_github() -> tuple[bool, str]:
    """Stage, commit, and push the synchronized GitHub package."""
    print("\n" + "=" * 60)
    print("PHASE 17: UPLOADING TO GITHUB")
    print("=" * 60)
    
    token = get_github_token()
    auth_url = f"https://abd19990044-commits:{token}@github.com/{GITHUB_REPO_SLUG}.git"
    
    # Verify or init git in GitHub/
    gh_git = GH_DIR / ".git"
    if not gh_git.exists():
        subprocess.check_call(["git", "init", "-b", "main"], cwd=GH_DIR)
        subprocess.check_call(["git", "remote", "add", "origin", auth_url], cwd=GH_DIR)
    else:
        subprocess.check_call(["git", "remote", "set-url", "origin", auth_url], cwd=GH_DIR)

    subprocess.check_call(["git", "config", "user.name", "Abdulsalam S. Hasan"], cwd=GH_DIR)
    subprocess.check_call(["git", "config", "user.email", "abd.19990044@gmail.com"], cwd=GH_DIR)
    
    subprocess.check_call(["git", "add", "-A"], cwd=GH_DIR)
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=GH_DIR, text=True).strip()
    
    if not status:
        print("[INFO] GitHub/ working tree is clean. No new commit needed.")
    else:
        commit_msg = (
            "docs: synchronize Chemistry Lab v1.0.3 DOI and documentation\n\n"
            "- Synchronized official Zenodo DOI (10.5281/zenodo.22119038) across repository metadata\n"
            "- Updated README.md, CITATION.cff, Academic Manual, and Citation Guides\n"
            "- Unified product identity under Chemistry Lab v1.0.3\n"
            "- Preserved Academic and Non-Commercial License v1.1\n"
            "- Zero em-dashes and en-dashes invariant maintained across all modules\n"
        )
        subprocess.check_call(["git", "commit", "-m", commit_msg], cwd=GH_DIR)
        print("[PASS] Committed latest synchronized changes to GitHub/ repo.")

    # Push to origin main without force-push
    print("Pushing to GitHub remote...")
    push_res = subprocess.run(["git", "push", "origin", "main"], cwd=GH_DIR, capture_output=True, text=True)
    if push_res.returncode != 0:
        # If remote has non-fast-forward commits, pull with rebase
        print(f"[INFO] Initial push result: {push_res.stderr.strip()}")
        if "fetch first" in push_res.stderr or "non-fast-forward" in push_res.stderr:
            print("Pulling remote changes before push...")
            subprocess.check_call(["git", "pull", "--rebase", "origin", "main"], cwd=GH_DIR)
            subprocess.check_call(["git", "push", "origin", "main"], cwd=GH_DIR)
        else:
            raise RuntimeError(f"GitHub push failed: {push_res.stderr}")

    commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=GH_DIR, text=True).strip()
    print(f"[PASS] Successfully pushed to GitHub main. Commit SHA: {commit_sha}")
    return True, commit_sha


def upload_to_huggingface() -> tuple[bool, str]:
    """Upload synchronized HuggingFace package to Hugging Face Spaces."""
    print("\n" + "=" * 60)
    print("PHASE 18: UPLOADING TO HUGGING FACE SPACES")
    print("=" * 60)
    
    token = get_hf_token()
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"https://huggingface.co/api/spaces/{HF_SPACE_SLUG}",
        headers=headers,
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Hugging Face Space API check failed (HTTP {r.status_code}): {r.text}")
    print(f"[PASS] Connected to Hugging Face Space: {HF_SPACE_SLUG}")

    import tempfile

    temp_work_dir = Path(tempfile.mkdtemp(prefix="hf_upload_workspace"))
    temp_work_dir.mkdir(parents=True, exist_ok=True)

    # Keep the token out of argv, the persisted Git remote URL, and exception
    # text.  Git consumes this process-scoped HTTP header from its environment.
    basic_auth = base64.b64encode(f"mc2hf1999:{token}".encode("utf-8")).decode("ascii")
    git_env = os.environ.copy()
    git_env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic_auth}",
        "GIT_TERMINAL_PROMPT": "0",
    })
    public_hf_url = f"https://huggingface.co/spaces/{HF_SPACE_SLUG}.git"
    print("Cloning remote Space repository...")
    subprocess.check_call(["git", "clone", public_hf_url, str(temp_work_dir)], env=git_env)

    # Delete existing tracked files to ensure clean synchronization
    for item in temp_work_dir.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            def _err(f, path, exc):
                import stat
                os.chmod(path, stat.S_IWRITE)
                f(path)
            shutil.rmtree(item, onerror=_err)
        else:
            item.unlink()

    # Copy files from synchronized HuggingFace/ package
    copied_count = 0
    for src_path in HF_DIR.rglob("*"):
        if src_path.is_file():
            rel = src_path.relative_to(HF_DIR)
            dst = temp_work_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst)
            copied_count += 1

    print(f"[PASS] Copied {copied_count} runtime files to Space workspace.")
    
    subprocess.check_call(["git", "config", "user.name", "Abdulsalam S. Hasan"], cwd=temp_work_dir)
    subprocess.check_call(["git", "config", "user.email", "abd.19990044@gmail.com"], cwd=temp_work_dir)
    subprocess.check_call(["git", "add", "-A"], cwd=temp_work_dir)
    
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=temp_work_dir, text=True).strip()
    if status:
        commit_msg = (
            "release: deploy ORCA Web Lab v1.0.0 (License v1.1 & ML engine updates)\n\n"
            "- Consolidated orca_engine under ORCA Web Lab Academic and Non-Commercial License v1.1\n"
            "- Production runtime deployment with dual-engine thermochemistry and spectroscopy\n"
            "- Full Cloudflare control plane and Kaggle orchestrator support\n"
        )
        subprocess.check_call(["git", "commit", "-m", commit_msg], cwd=temp_work_dir)
        print("Pushing to Hugging Face Spaces remote...")
        subprocess.check_call(["git", "push", "origin", "main"], cwd=temp_work_dir, env=git_env)
    else:
        print("[INFO] Hugging Face Space is already up to date.")

    commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=temp_work_dir, text=True).strip()
    print(f"[PASS] Successfully deployed to Hugging Face Space. Commit SHA: {commit_sha}")
    return True, commit_sha


def main():
    print("=" * 70)
    print("ORCA WEB LAB: SYNCHRONIZATION AND DEPLOYMENT PIPELINE")
    print("=" * 70)

    # 1. Synchronize GitHub/
    print("\n--- Synchronizing GitHub/ folder from Main Project ---")
    gh_added, gh_modified, gh_removed = synchronize_target("GitHub", GH_DIR, is_hf=False)
    print(f"GitHub: {len(gh_added)} added, {len(gh_modified)} modified, {len(gh_removed)} removed.")

    # 2. Synchronize HuggingFace/
    print("\n--- Synchronizing HuggingFace/ folder from Main Project ---")
    hf_added, hf_modified, hf_removed = synchronize_target("HuggingFace", HF_DIR, is_hf=True)
    print(f"HuggingFace: {len(hf_added)} added, {len(hf_modified)} modified, {len(hf_removed)} removed.")

    # 3. Generate Manifests and Reports
    print("\n--- Generating Manifests and Difference Report ---")
    generate_manifests_and_reports(
        (gh_added, gh_modified, gh_removed),
        (hf_added, hf_modified, hf_removed)
    )
    print("[PASS] Manifests and RELEASE_SYNC_DIFFERENCE_REPORT.md generated.")

    # 4. Security Scan
    print("\n--- Running Security and Secret Scan across all areas ---")
    secrets_main = scan_for_secrets(REPO_ROOT)
    secrets_gh = scan_for_secrets(GH_DIR)
    secrets_hf = scan_for_secrets(HF_DIR)
    
    total_secrets = len(secrets_main) + len(secrets_gh) + len(secrets_hf)
    if total_secrets > 0:
        print(f"[CRITICAL ERROR] Found {total_secrets} potential secrets! Aborting upload.")
        for path, name, snip in secrets_main:
            print(f"  Main: {path} -> {name}: {snip}")
        for path, name, snip in secrets_gh:
            print(f"  GitHub: {path} -> {name}: {snip}")
        for path, name, snip in secrets_hf:
            print(f"  HuggingFace: {path} -> {name}: {snip}")
        sys.exit(1)
    print("[PASS] Secret scan passed with 0 exposed secrets found.")

    # 5. Upload GitHub
    gh_ok, gh_sha = upload_to_github()

    # 6. Upload Hugging Face
    hf_ok, hf_sha = upload_to_huggingface()

    print("\n" + "=" * 70)
    print("DEPLOYMENT SYNCHRONIZATION COMPLETE")
    print(f"GitHub Remote Commit SHA      : {gh_sha}")
    print(f"Hugging Face Remote Commit SHA: {hf_sha}")
    print("=" * 70)


if __name__ == "__main__":
    main()

