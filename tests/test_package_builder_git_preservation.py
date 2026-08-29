# -*- coding: utf-8 -*-
"""Builder safety: a GitHub/ package rebuild must NEVER touch GitHub/.git.

The nested repository is independent state (history, refs, index, objects,
config, HEAD). This suite proves, against the REAL builder functions:
- sentinel .git content survives byte-for-byte across a full rebuild;
- stale package entries outside .git are removed;
- the current production package content is rebuilt;
- repository metadata never leaks into package manifests;
- the HuggingFace minimal runtime closure never contains .git metadata.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SENTINEL_HEAD = "ref: refs/heads/main\n"
SENTINEL_CONFIG = "[core]\n\trepositoryformatversion = 0\n"
SENTINEL_OBJ = b"\xde\xad\xbe\xef" * 5
SENTINEL_REF = "0" * 40 + "\n"


def _make_nested_repo(dest):
    git = dest / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(SENTINEL_HEAD, encoding="utf-8")
    (git / "config").write_text(SENTINEL_CONFIG, encoding="utf-8")
    (git / "objects" / "sentinel").write_bytes(SENTINEL_OBJ)
    (git / "refs" / "heads" / "main").write_text(SENTINEL_REF, encoding="utf-8")


def _provenance():
    return {"package_mode": "TEST_HARNESS", "source_type": "test",
            "source_git_state": "test", "source_git_sha": "0" * 40,
            "source_git_dirty": True}


def test_github_rebuild_preserves_nested_git_and_removes_stale_content(tmp_path):
    from tools.build_deployment_packages import build_github_package
    dest = tmp_path / "GitHub"
    dest.mkdir()
    _make_nested_repo(dest)
    # stale package content that a rebuild must remove
    (dest / "stale_top_file.txt").write_text("obsolete", encoding="utf-8")
    stale_dir = dest / "stale_old_dir" / "deep"
    stale_dir.mkdir(parents=True)
    (stale_dir / "old.txt").write_text("obsolete", encoding="utf-8")
    (dest / "app.py").write_text("# stale previous build\n", encoding="utf-8")

    manifest = build_github_package(dest, _provenance())

    git = dest / ".git"
    assert git.is_dir(), "GitHub/.git must survive the rebuild"
    assert (git / "HEAD").read_text(encoding="utf-8") == SENTINEL_HEAD
    assert (git / "config").read_text(encoding="utf-8") == SENTINEL_CONFIG
    assert (git / "objects" / "sentinel").read_bytes() == SENTINEL_OBJ
    assert (git / "refs" / "heads" / "main").read_text(encoding="utf-8") == SENTINEL_REF
    # stale non-.git package content removed
    assert not (dest / "stale_top_file.txt").exists()
    assert not (dest / "stale_old_dir").exists()
    # current production package content rebuilt (incl. the P2-2 files)
    for rel in ("app.py", "api/main.py", "services/kaggle_service.py",
                "services/orca_service.py", "api/routes/kaggle.py",
                "orca_orchestrator/orca_artifacts.py", "requirements.txt",
                "CITATION.cff", "FILE_MANIFEST.txt", "RELEASE_MANIFEST.json"):
        assert (dest / rel).is_file(), "missing rebuilt package file: %s" % rel
    # the rebuilt app.py is the real source, not the stale marker
    assert "# stale previous build" not in (dest / "app.py").read_text(encoding="utf-8")
    assert manifest["release_type"] == "github-source-release"
    # repository metadata never leaked into the manifest (exact-path check:
    # .github/... and .gitignore are legitimate package files)
    import json as _json
    manifest_paths = [ln.split("|", 1)[0].strip() for ln in
                      (dest / "FILE_MANIFEST.txt").read_text(encoding="utf-8").splitlines()
                      if ln and not ln.startswith("#") and "|" in ln]
    leaked = [p for p in manifest_paths if p == ".git" or p.startswith(".git/")]
    assert not leaked, "repository metadata leaked into FILE_MANIFEST: %s" % leaked
    release = _json.loads((dest / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    leaked2 = [k for k in release["runtime_critical_files"]
               if k == ".git" or k.startswith(".git/")]
    assert not leaked2, "repository metadata leaked into RELEASE_MANIFEST: %s" % leaked2


def test_packages_never_contain_git_metadata(tmp_path):
    """The HuggingFace minimal runtime closure is not a repository: any stray
    .git is wiped, and the builder's excludes never copy repository metadata."""
    from tools.build_deployment_packages import build_huggingface_package
    dest = tmp_path / "HuggingFace"
    dest.mkdir()
    (dest / ".git").mkdir()
    build_huggingface_package(dest, _provenance())
    assert not (dest / ".git").exists(), "HF runtime closure must not carry .git"
    assert (dest / "api" / "main.py").is_file()
    assert (dest / "services" / "kaggle_service.py").is_file()
