# -*- coding: utf-8 -*-
"""Comprehensive tests for IR Spectrum parsing, convolution, and FTIR overlay."""

import pytest
import io
from orca_engine.parser import OrcaParser
from orca_engine.adapters.web_adapter import convolute_ir_spectrum, job_to_web_json
from orca_engine.models import JobData
from app import app


SAMPLE_ORCA_IR_OUTPUT = """
-------------------------------------------------------------------------------
                                 IR SPECTRUM
-------------------------------------------------------------------------------

 Mode   freq (cm**-1)   T**2 (km/mol)    TX       TY       TZ
------------------------------------------------------------------
    6:     1595.23         12.345      0.123    0.456    0.789
    7:     3657.05         45.678      0.345    0.678    0.901
    8:     3755.93         89.123      0.567    0.890    0.123

-----------------------
VIBRATIONAL FREQUENCIES
-----------------------
   0:         0.00 cm**-1
   1:         0.00 cm**-1
   2:         0.00 cm**-1
   3:         0.00 cm**-1
   4:         0.00 cm**-1
   5:         0.00 cm**-1
   6:      1595.23 cm**-1
   7:      3657.05 cm**-1
   8:      3755.93 cm**-1

****ORCA TERMINATED NORMALLY****
"""

SAMPLE_FTIR_TEXT_TRANSMITTANCE = """# FTIR Experimental Spectrum (H2O vapor)
Wavenumber (cm-1)    Transmittance (%)
4000.0              98.5
3755.0              22.4
3655.0              35.1
2500.0              99.2
1595.0              18.7
500.0               97.8
"""


def test_orca_ir_spectrum_parsing():
    """Verify ORCA IR spectrum table parsing with frequencies and intensities."""
    parser = OrcaParser(SAMPLE_ORCA_IR_OUTPUT)
    jobs = parser.parse()
    assert len(jobs) == 1
    job = jobs[0]
    
    assert len(job.ir_frequencies_cm) == 3
    assert job.ir_frequencies_cm[0] == 1595.23
    assert job.ir_frequencies_cm[1] == 3657.05
    assert job.ir_frequencies_cm[2] == 3755.93
    
    assert len(job.ir_intensities_km_mol) == 3
    assert job.ir_intensities_km_mol[0] == 12.345
    assert job.ir_intensities_km_mol[1] == 45.678
    assert job.ir_intensities_km_mol[2] == 89.123

    assert len(job.ir_spectrum) == 3
    assert job.ir_spectrum[0]["mode"] == 6


def test_convolute_ir_spectrum_lorentzian():
    """Verify Lorentzian line broadening and %T calculation."""
    freqs = [1595.23, 3657.05, 3755.93]
    intensities = [12.345, 45.678, 89.123]
    
    curve = convolute_ir_spectrum(
        frequencies_cm=freqs,
        intensities_km_mol=intensities,
        scaling_factor=1.0,
        fwhm_cm=15.0,
        start_cm=400.0,
        end_cm=4000.0,
        step_cm=5.0
    )
    
    assert len(curve) > 100
    assert "wavenumber_cm" in curve[0]
    assert "absorbance" in curve[0]
    assert "transmittance_pct" in curve[0]
    
    # Transmittance should be between 0% and 100%
    for pt in curve:
        assert 0.0 <= pt["transmittance_pct"] <= 100.0


def test_api_ir_experimental_parse_text():
    """Test API endpoint for experimental FTIR text parsing."""
    client = app.test_client()
    data = {
        "file": (io.BytesIO(SAMPLE_FTIR_TEXT_TRANSMITTANCE.encode("utf-8")), "water_ftir.txt"),
        "file_name": "water_ftir.txt"
    }
    response = client.post("/api/orca/engine/ir-spectrum/parse-experimental", data=data, content_type="multipart/form-data")
    assert response.status_code == 200
    res_json = response.get_json()
    assert res_json["success"] is True
    spectrum = res_json["spectrum"]
    assert spectrum["points_count"] == 6
    assert len(spectrum["points"]) == 6
