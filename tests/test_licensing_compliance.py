"""Licensing compliance and governance test suite for ORCA Web Lab.

Verifies:
1. orca_engine is consolidated under ORCA Web Lab Academic and Non-Commercial License v1.1.
2. Proprietary orca_engine source and metadata have no incorrect MIT declarations.
3. Legitimate third-party license obligations (MIT, BSD, Apache) remain intact in THIRD_PARTY_LICENSES.md.
4. Zero em-dashes or en-dashes invariant in licensing documents.
"""

from __future__ import annotations

import os
from pathlib import Path
import tomllib
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestOrcaEngineLicensingConsolidation:
    """Verify orca_engine license consolidation into the main academic license."""

    def test_orca_engine_license_file_is_canonical_v1_1(self):
        engine_license_path = REPO_ROOT / "orca_engine" / "LICENSE"
        assert engine_license_path.exists(), "orca_engine/LICENSE must exist"
        
        content = engine_license_path.read_text(encoding="utf-8")
        assert "MIT License" not in content, "orca_engine/LICENSE must not contain MIT License terms"
        assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in content, (
            "orca_engine/LICENSE must contain the canonical ORCA Web Lab Academic license header"
        )
        assert "Abdulsalam S. Hasan" in content, "orca_engine/LICENSE must identify the copyright holder"

    def test_orca_engine_pyproject_license_declaration(self):
        pyproject_path = REPO_ROOT / "orca_engine" / "pyproject.toml"
        assert pyproject_path.exists(), "orca_engine/pyproject.toml must exist"
        
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            
        proj = data.get("project", {})
        license_entry = proj.get("license", {})
        license_text = license_entry.get("text", "")
        
        assert "MIT" not in str(license_entry), "orca_engine pyproject license must not declare MIT"
        assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in license_text, (
            "orca_engine pyproject must declare the custom academic license"
        )
        
        classifiers = proj.get("classifiers", [])
        for c in classifiers:
            assert "MIT" not in c, f"Found unexpected MIT classifier: {c}"
            assert "OSI Approved" not in c, f"Custom license must not claim OSI approval: {c}"

    def test_orca_engine_readme_license_section(self):
        readme_path = REPO_ROOT / "orca_engine" / "README.md"
        assert readme_path.exists(), "orca_engine/README.md must exist"
        
        content = readme_path.read_text(encoding="utf-8")
        assert "[![License: MIT]" not in content, "Badge must not claim MIT"
        assert "MIT. See [LICENSE]" not in content, "Readme must not claim standalone MIT"
        assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in content, (
            "Readme must reference ORCA Web Lab Academic and Non-Commercial License v1.1"
        )

    def test_orca_engine_contributing_license(self):
        contrib_path = REPO_ROOT / "orca_engine" / "CONTRIBUTING.md"
        if contrib_path.exists():
            content = contrib_path.read_text(encoding="utf-8")
            assert "under the MIT License" not in content, "CONTRIBUTING.md must not accept under MIT"
            assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in content

    def test_orca_engine_citation_license(self):
        citation_path = REPO_ROOT / "orca_engine" / "CITATION.cff"
        if citation_path.exists():
            content = citation_path.read_text(encoding="utf-8")
            assert "license: MIT" not in content, "CITATION.cff must not declare license: MIT"
            assert "ORCA-Web-Lab" in content or "Non-Commercial" in content

    def test_cloudflare_control_plane_package_json(self):
        pkg_path = REPO_ROOT / "cloudflare-control-plane" / "package.json"
        if pkg_path.exists():
            import json
            with open(pkg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            assert data.get("license") != "MIT", "Cloudflare control plane must not claim MIT license"
            assert data.get("license") == "SEE LICENSE IN LICENSE"


class TestThirdPartyLicensePreservation:
    """Verify that genuine third-party licenses are strictly preserved."""

    def test_third_party_licenses_md_intact(self):
        tpl_path = REPO_ROOT / "THIRD_PARTY_LICENSES.md"
        assert tpl_path.exists(), "THIRD_PARTY_LICENSES.md must exist"
        
        content = tpl_path.read_text(encoding="utf-8")
        
        # Verify legitimate third-party MIT dependencies are still present
        assert "Gunicorn" in content
        assert "OpenPyXL" in content
        assert "JSONSchema" in content
        assert "Pytest" in content
        assert "MIT" in content, "THIRD_PARTY_LICENSES.md must still attribute genuine third-party MIT tools"
        
        # Verify BSD and Apache dependencies
        assert "Flask" in content and "BSD-3-Clause" in content
        assert "RDKit" in content and "BSD-3-Clause" in content
        assert "Requests" in content and "Apache-2.0" in content
        assert "3Dmol.js" in content and "BSD-3-Clause" in content
        
        # Verify ORCA program boundary
        assert "ORCA Quantum Chemistry Software Boundary" in content
        assert "Prof. Frank Neese" in content

    def test_root_license_file_authoritative(self):
        root_license_path = REPO_ROOT / "LICENSE"
        assert root_license_path.exists()
        content = root_license_path.read_text(encoding="utf-8")
        assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in content
        assert "Abdulsalam S. Hasan" in content
        assert "NOT an Open Source Initiative (OSI) approved open-source license" in content


class TestZeroEmDashesInLicensingFiles:
    """Strict invariant test: zero em-dashes (\u2014) and zero en-dashes (\u2013)."""

    def test_zero_em_dashes_in_all_licensing_files(self):
        files_to_check = [
            REPO_ROOT / "LICENSE",
            REPO_ROOT / "THIRD_PARTY_LICENSES.md",
            REPO_ROOT / "orca_engine" / "LICENSE",
            REPO_ROOT / "orca_engine" / "README.md",
            REPO_ROOT / "orca_engine" / "CONTRIBUTING.md",
            REPO_ROOT / "orca_engine" / "CITATION.cff",
            REPO_ROOT / "orca_engine" / "pyproject.toml",
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "cloudflare-control-plane" / "package.json",
        ]
        
        for fpath in files_to_check:
            if fpath.exists():
                text = fpath.read_text(encoding="utf-8", errors="ignore")
                em_count = text.count("\u2014")
                en_count = text.count("\u2013")
                assert em_count == 0, f"File {fpath} contains {em_count} em-dashes (\u2014)"
                assert en_count == 0, f"File {fpath} contains {en_count} en-dashes (\u2013)"


class TestOrcaEngineRuntimeIntegrity:
    """Verify that licensing changes did not affect orca_engine runtime or imports."""

    def test_orca_engine_import_and_models(self):
        import orca_engine
        from orca_engine import OrcaParser, ThermochemistryEngine, JobData, MoleculeData
        
        assert hasattr(orca_engine, "__version__") or hasattr(orca_engine, "OrcaParser")
        
        # Verify parser instantiate
        lines = [
            "  * O   R   C   A *",
            "Program Version 6.1.0",
            "FINAL SINGLE POINT ENERGY      -76.360770002990",
            "ORCA TERMINATED NORMALLY",
        ]
        parser = OrcaParser(iter(lines), source_name="dummy.out")
        jobs = parser.parse()
        assert len(jobs) == 1
        assert jobs[0].e_elec_eh is not None
        assert abs(jobs[0].e_elec_eh - (-76.360770002990)) < 1e-10

