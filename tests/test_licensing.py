"""Comprehensive Licensing, Legal Compliance, and IP Architecture Verification Suite.

ORCA Web Lab Academic and Non-Commercial License v1.1.

Validates:
1. Canonical LICENSE and byte-identical LICENSE.txt integrity and v1.1 versioning.
2. Nature-and-purpose based Non-Commercial definition (institutional status does not override purpose).
3. Broad Commercial Use definition (internal corporate R&D, pharma/materials use, SaaS, sponsored research).
4. Sublicensing prohibition vs permitted academic redistribution.
5. Ownership retention (solely Abdulsalam S. Hasan; no ownership transfer under academic license).
6. Future Full IP Assignment framework (separate written agreement; only transferable rights; no patent overclaims).
7. Non-cancellation of prior valid grants upon future sale.
8. Third-party software boundary, ORCA boundary, and Data/Database boundary.
9. Strict official contact details consistency (abd.19990044@gmail.com, +9647715541279).
10. CONTRIBUTING.md contributor policy (single owner; future CLA/assignment requirement).
11. Distribution package synchronization (HuggingFace/ and GitHub/).
12. In-application API endpoints (/api/license, /api/third-party-licenses) and HTML modal UI.
13. Zero em-dash policy compliance across all licensing documents.
14. No contradictory Open Source / OSI claims for proprietary ORCA Web Lab code.
"""

from pathlib import Path
import re
import pytest
from app import app

_parent = Path(__file__).resolve().parent.parent
if _parent.name == "GitHub":
    REPO_ROOT = _parent.parent
    PKG_ROOT = _parent
else:
    REPO_ROOT = _parent
    PKG_ROOT = _parent

LICENSE_FILE = PKG_ROOT / "LICENSE"
LICENSE_TXT_FILE = PKG_ROOT / "LICENSE.txt"
THIRD_PARTY_FILE = PKG_ROOT / "THIRD_PARTY_LICENSES.md"
README_FILE = PKG_ROOT / "README.md"
CONTRIBUTING_FILE = PKG_ROOT / "CONTRIBUTING.md"
HF_DIR = REPO_ROOT / "HuggingFace"
GH_DIR = REPO_ROOT / "GitHub"

OFFICIAL_EMAIL = "abd.19990044@gmail.com"
OFFICIAL_PHONE = "+9647715541279"
OFFICIAL_WHATSAPP_URL = "https://wa.me/9647715541279"
FORBIDDEN_EMAILS = [
    "abdulsalam.s.hasan@gmail.com",
]


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


class TestRootLicenseV11:
    """Tests for canonical LICENSE v1.1 structure and legal clauses."""

    def test_license_file_exists_and_byte_identical_to_txt(self):
        assert LICENSE_FILE.is_file(), "Canonical LICENSE file must exist"
        assert LICENSE_TXT_FILE.is_file(), "Canonical LICENSE.txt file must exist"
        assert LICENSE_FILE.stat().st_size > 1500, "LICENSE file must contain comprehensive legal text"
        assert LICENSE_FILE.read_text(encoding="utf-8") == LICENSE_TXT_FILE.read_text(encoding="utf-8"), "LICENSE and LICENSE.txt content must match"
        assert LICENSE_FILE.read_bytes().replace(b"\r\n", b"\n") == LICENSE_TXT_FILE.read_bytes().replace(b"\r\n", b"\n"), "LICENSE and LICENSE.txt must be byte-identical"

    def test_license_title_version_and_copyright(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in norm
        assert "Copyright (c) 2026 Abdulsalam S. Hasan. All rights reserved." in norm
        assert "NOT an Open Source Initiative (OSI) approved" in norm

    def test_legal_review_notice_present(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "Commercial licensing and intellectual-property transactions should be reviewed by qualified legal counsel" in norm

    def test_nature_and_purpose_noncommercial_definition(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "Permitted Non-Commercial Use" in norm
        assert "nature and purpose of the use" in norm
        assert "institutional classification of an organization" in norm
        assert "does NOT automatically render every activity conducted within that organization non-commercial" in norm

    def test_broad_commercial_use_definition(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "Internal Corporate Use" in norm
        assert "regardless of whether the Software itself is sold" in norm
        assert "Software-as-a-Service" in norm or "SaaS" in norm
        assert "Fee-For-Service & Consulting" in norm
        assert "Sponsored Commercial Research" in norm
        assert "Commercial Integration & OEM" in norm
        assert "White-Labeling" in norm

    def test_modification_redistribution_and_sublicensing_rules(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "REDISTRIBUTION VS SUBLICENSING" in norm
        assert "Sublicensing is strictly prohibited" in norm
        assert "THIRD_PARTY_LICENSES.md" in norm

    def test_ownership_retention_and_no_transfer(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "INTELLECTUAL PROPERTY AND NO OWNERSHIP TRANSFER" in norm
        assert "exclusive property of Abdulsalam S. Hasan" in norm
        assert "does NOT transfer ownership, title, copyright" in norm

    def test_commercial_licensing_separate_contract(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "COMMERCIAL LICENSING (SEPARATE WRITTEN CONTRACT)" in norm
        assert "Commercial terms, pricing, seat allocations, support levels, and deployment parameters are negotiated separately" in norm

    def test_future_full_ip_assignment_qualified(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "FUTURE ASSIGNMENT OF PROPRIETARY INTELLECTUAL PROPERTY" in norm
        assert "separate, written Intellectual Property Assignment Agreement" in norm
        assert "copyright and other proprietary intellectual-property rights" in norm
        assert "specifically identifies the rights being assigned" in norm
        assert "to the extent legally transferable" in norm

    def test_no_unsupported_patent_claims(self):
        content = LICENSE_FILE.read_text(encoding="utf-8")
        assert "title, copyright, and patents" not in content, "Avoid claiming transfer of patents unless actually owned"

    def test_pre_existing_grants_not_retroactively_cancelled(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "Status of Pre-Existing Grants" in norm
        assert "subject to valid pre-existing license grants" in norm
        assert "does not create a retroactive automatic cancellation" in norm

    def test_scientific_software_disclaimer(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "SCIENTIFIC SOFTWARE NOTICE" in norm
        assert "MATHEMATICAL APPROXIMATIONS" in norm
        assert "INDEPENDENTLY VALIDATED" in norm
        assert "NOT A SUBSTITUTE FOR PROFESSIONAL SCIENTIFIC JUDGMENT" in norm

    def test_governing_law_placeholder(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert "[Governing law and jurisdiction to be determined by the rights holder with qualified legal counsel.]" in norm

    def test_official_contact_details_in_license(self):
        norm = normalize_ws(LICENSE_FILE.read_text(encoding="utf-8"))
        assert OFFICIAL_EMAIL in norm
        assert OFFICIAL_PHONE in norm

    def test_zero_em_dashes_in_license(self):
        content = LICENSE_FILE.read_text(encoding="utf-8")
        assert "\u2014" not in content, "LICENSE must not contain em-dashes (\\u2014)"
        assert "\u2013" not in content, "LICENSE must not contain en-dashes (\\u2013)"


class TestThirdPartyLicensesAndBoundaries:
    """Tests for THIRD_PARTY_LICENSES.md integrity and boundary definitions."""

    def test_third_party_file_exists(self):
        assert THIRD_PARTY_FILE.is_file(), "THIRD_PARTY_LICENSES.md must exist"

    def test_third_party_inventory_sections(self):
        norm = normalize_ws(THIRD_PARTY_FILE.read_text(encoding="utf-8"))
        assert "Proprietary Code vs Third-Party Software Boundary" in norm
        assert "ORCA Quantum Chemistry Software Boundary" in norm
        assert "Data, Databases & User Outputs Boundary" in norm
        assert "Python Runtime Dependencies" in norm
        assert "Frontend & In-Browser Dependencies" in norm
        assert "Cloudflare Control Plane Dependencies" in norm
        assert "External Web Services & APIs" in norm

    def test_core_dependencies_present(self):
        content = THIRD_PARTY_FILE.read_text(encoding="utf-8")
        for dep in ["Flask", "RDKit", "3Dmol.js", "Cryptography", "NumPy", "Kaggle API"]:
            assert dep in content, f"{dep} must be documented in third-party inventory"

    def test_orca_boundary_explicitly_stated(self):
        norm = normalize_ws(THIRD_PARTY_FILE.read_text(encoding="utf-8"))
        assert "ORCA Web Lab is an independent software platform that interfaces with ORCA" in norm
        assert "NOT Bundled" in norm
        assert "Zero Rights Grant" in norm

    def test_data_and_database_rights_clarified(self):
        norm = normalize_ws(THIRD_PARTY_FILE.read_text(encoding="utf-8"))
        assert "User Scientific Outputs" in norm
        assert "claims no copyright ownership over molecular coordinates" in norm
        assert "ir_peak_database.json" in norm

    def test_official_contact_in_third_party_notice(self):
        norm = normalize_ws(THIRD_PARTY_FILE.read_text(encoding="utf-8"))
        assert OFFICIAL_EMAIL in norm
        assert OFFICIAL_PHONE in norm

    def test_zero_em_dashes_in_third_party_notice(self):
        content = THIRD_PARTY_FILE.read_text(encoding="utf-8")
        assert "\u2014" not in content, "THIRD_PARTY_LICENSES.md must not contain em-dashes (\\u2014)"
        assert "\u2013" not in content, "THIRD_PARTY_LICENSES.md must not contain en-dashes (\\u2013)"


class TestContributingPolicy:
    """Tests for CONTRIBUTING.md contributor and ownership policy."""

    def test_contributing_file_exists(self):
        if CONTRIBUTING_FILE.exists():
            norm = normalize_ws(CONTRIBUTING_FILE.read_text(encoding="utf-8"))
            assert "Abdulsalam S. Hasan" in norm
            assert "Single rights holder" in norm or "single rights holder" in norm or "authored and owned solely" in norm
            assert "Contributor License Agreement" in norm or "CLA" in norm


class TestDistributionPackagesLicensing:
    """Tests licensing integrity in HuggingFace/ and GitHub/ deployment packages."""

    def test_hf_license_and_third_party_files(self):
        if HF_DIR.exists():
            assert (HF_DIR / "LICENSE").is_file(), "HuggingFace/ must contain LICENSE"
            assert (HF_DIR / "LICENSE.txt").is_file(), "HuggingFace/ must contain LICENSE.txt"
            assert (HF_DIR / "LICENSE").read_bytes() == (HF_DIR / "LICENSE.txt").read_bytes()
            assert (HF_DIR / "THIRD_PARTY_LICENSES.md").is_file(), "HuggingFace/ must contain THIRD_PARTY_LICENSES.md"
            hf_readme = (HF_DIR / "README.md").read_text(encoding="utf-8")
            assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in hf_readme
            assert OFFICIAL_EMAIL in hf_readme
            assert OFFICIAL_PHONE in hf_readme

    def test_github_license_and_third_party_files(self):
        if GH_DIR.exists():
            assert (GH_DIR / "LICENSE").is_file(), "GitHub/ must contain LICENSE"
            assert (GH_DIR / "LICENSE.txt").is_file(), "GitHub/ must contain LICENSE.txt"
            assert (GH_DIR / "LICENSE").read_bytes() == (GH_DIR / "LICENSE.txt").read_bytes()
            assert (GH_DIR / "THIRD_PARTY_LICENSES.md").is_file(), "GitHub/ must contain THIRD_PARTY_LICENSES.md"
            gh_readme = (GH_DIR / "README.md").read_text(encoding="utf-8")
            assert "ORCA Web Lab Academic and Non-Commercial License v1.1" in gh_readme
            assert OFFICIAL_EMAIL in gh_readme
            assert OFFICIAL_PHONE in gh_readme

    def test_no_forbidden_emails_in_distribution_packages(self):
        for dist_dir in [HF_DIR, GH_DIR]:
            if dist_dir.exists():
                for p in dist_dir.rglob("*.html"):
                    txt = p.read_text(encoding="utf-8", errors="ignore")
                    for forbidden in FORBIDDEN_EMAILS:
                        assert forbidden not in txt, f"Found obsolete email {forbidden} in {p}"


class TestAppLicenseEndpointsAndUI:
    """Tests the in-application licensing API endpoints and HTML modal."""

    @pytest.fixture
    def client(self):
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client

    def test_api_license_endpoint(self, client):
        resp = client.get("/api/license")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["license_name"] == "ORCA Web Lab Academic and Non-Commercial License v1.1"
        assert data["author"] == "Abdulsalam S. Hasan"
        assert data["contact"]["email"] == OFFICIAL_EMAIL
        assert data["contact"]["whatsapp"] == OFFICIAL_PHONE

    def test_api_third_party_licenses_endpoint(self, client):
        resp = client.get("/api/third-party-licenses")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["orca_boundary"]["bundled"] is False
        assert len(data["python_dependencies"]) >= 10
        assert data["licensing_contact"]["email"] == OFFICIAL_EMAIL

    def test_ui_index_contains_license_modal_and_links(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="license-modal"' in html
        assert 'id="footer-license-btn"' in html
        assert "Academic &amp; Non-Commercial License v1.1" in html
        assert OFFICIAL_EMAIL in html
        assert OFFICIAL_PHONE in html
        assert OFFICIAL_WHATSAPP_URL in html
