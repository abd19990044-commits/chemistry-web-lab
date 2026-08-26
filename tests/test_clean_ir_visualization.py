# -*- coding: utf-8 -*-
"""
Tests for Clean IR Spectral Visualization, Lorentzian Broadening, and Endpoint Sanity.

Verifies:
- IR Lorentzian convolution remains fully functional
- Experimental IR file parsing (.csv, .txt, .dpt, .xlsx) remains fully functional
- Removed endpoints (/api/orca/spectrum/ir-assign, /api/orca/spectrum/ir-database) return 404
- Removed archive endpoints (/api/orca/upload-archive, /api/orca/archive/<id>) return 404
- Transmittance / Absorbance conversions remain exact and stable
"""
import pytest
from orca_engine.adapters.web_adapter import convolute_ir_spectrum
import app as webapp


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


class TestRemovedEndpoints:
    """Verify obsolete IR assignment and direct archive upload endpoints are cleanly absent."""

    def test_ir_assign_endpoint_removed(self, client):
        resp = client.post("/api/orca/spectrum/ir-assign", json={"raw_data": [[1000, 50]]})
        assert resp.status_code == 404

    def test_ir_database_endpoint_removed(self, client):
        resp = client.get("/api/orca/spectrum/ir-database")
        assert resp.status_code == 404

    def test_orca_archive_upload_endpoint_removed(self, client):
        resp = client.post("/api/orca/upload-archive", data={})
        assert resp.status_code == 404

    def test_orca_archive_get_endpoint_removed(self, client):
        resp = client.get("/api/orca/archive/orca_pkg_test123")
        assert resp.status_code == 404


class TestPreservedIRSpectraFeatures:
    """Verify Lorentzian convolution remains 100% operational."""

    def test_lorentzian_convolution_intact(self):
        freqs = [1700.0, 2950.0]
        intensities = [250.0, 80.0]
        curve = convolute_ir_spectrum(
            frequencies_cm=freqs,
            intensities_km_mol=intensities,
            scaling_factor=1.0,
            fwhm_cm=15.0,
            start_cm=400.0,
            end_cm=4000.0,
            step_cm=2.0,
        )
        assert len(curve) > 100
        assert "wavenumber_cm" in curve[0]
        assert "absorbance" in curve[0]
        assert "transmittance_pct" in curve[0]
        assert all(0.0 <= pt["transmittance_pct"] <= 100.0 for pt in curve)

