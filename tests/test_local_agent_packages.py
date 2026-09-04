# -*- coding: utf-8 -*-
"""Tests for Local Agent ZIP Package Builder, Member Hash Matching, and Zero-Secret Leaks."""
import hashlib
import zipfile
from pathlib import Path
from tools.build_local_agent_packages import build_all_packages

REPO = Path(__file__).resolve().parent.parent


def test_build_all_packages_and_integrity():
    manifests = build_all_packages()
    assert len(manifests) == 4

    source_modules = {
        "local_agent/agent.py": REPO / "local_agent" / "agent.py",
        "local_agent/config.py": REPO / "local_agent" / "config.py",
        "local_agent/security.py": REPO / "local_agent" / "security.py",
        "local_agent/protocol.py": REPO / "local_agent" / "protocol.py",
    }

    source_hashes = {}
    for rel_path, full_path in source_modules.items():
        with open(full_path, "rb") as f:
            source_hashes[rel_path] = hashlib.sha256(f.read()).hexdigest()

    for pkg_id, info in manifests.items():
        zip_p = Path(info["file_path"])
        assert zip_p.is_file()
        assert info["size_bytes"] > 0
        assert len(info["sha256"]) == 64

        with zipfile.ZipFile(zip_p, "r") as zf:
            namelist = zf.namelist()
            assert "README_LOCAL_AGENT.txt" in namelist
            assert "VERSION" in namelist
            assert "checksums.txt" in namelist
            assert any(f.startswith("local_agent/") for f in namelist)

            # Assert member hash matches repo authoritative source exactly
            for rel_path, expected_sha in source_hashes.items():
                if rel_path in namelist:
                    member_data = zf.read(rel_path)
                    member_sha = hashlib.sha256(member_data).hexdigest()
                    assert member_sha == expected_sha, f"ZIP member {rel_path} in {zip_p.name} does not match repo source!"

            for name in namelist:
                content = zf.read(name)
                # Ensure no machine paths or user credentials in package
                assert b"D:\\orca6" not in content
                assert b"C:\\Users\\ahmed" not in content
                assert b"G:\\orca web lab" not in content
                assert b".gemini\\antigravity" not in content

                # Ensure no live runtime secrets or database files
                assert not name.endswith(".sqlite3")
                assert not name.endswith(".lock")
                assert "chemistry_lab_pairing.txt" not in name

                # Ensure zero ORCA binary executables
                assert not name.endswith(".exe") or "python" in name.lower() or name in namelist  # No orca.exe
                assert "orca.exe" not in name.lower()
