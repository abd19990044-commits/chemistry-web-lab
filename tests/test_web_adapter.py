"""Tests for web adapter, 3D XYZ generation, and UV-Vis spectral convolution."""

from __future__ import annotations

import pytest

from orca_engine.adapters.web_adapter import (
    convolute_uvvis_spectrum,
    generate_xyz_string,
    job_to_web_json,
    molecule_to_web_json,
)
from orca_engine.models import JobData, MoleculeData


class TestWebAdapter:
    """Test web data serialization and spectral convolution."""

    def test_generate_xyz_string(self) -> None:
        """XYZ string must follow the standard chemical format."""

        job = JobData(
            elements=["C", "H"],
            coords=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
        )
        xyz = generate_xyz_string(job, comment="Ethyne fragment")

        lines = xyz.strip().splitlines()
        assert lines[0] == "2"
        assert lines[1] == "Ethyne fragment"
        assert lines[2].startswith("C")
        assert lines[3].startswith("H")

    def test_convolute_uvvis_spectrum_with_shift(self) -> None:
        """Gaussian convolution must reflect vertical transitions and wavelength offset."""

        tddft_cm = [25000.0]  # 1e7 / 25000 = 400.0 nm
        tddft_fosc = [0.5]

        # Convolute without shift (peak at 400 nm)
        spectrum_0 = convolute_uvvis_spectrum(
            tddft_cm,
            tddft_fosc,
            wavelength_shift_nm=0.0,
            sigma_nm=10.0,
            start_nm=350,
            end_nm=450,
            step_nm=1,
        )
        max_pt_0 = max(spectrum_0, key=lambda p: p["intensity"])
        assert max_pt_0["wavelength_nm"] == pytest.approx(400.0, abs=1.0)

        # Convolute with +20 nm red-shift (peak shifts to 420 nm)
        spectrum_shift = convolute_uvvis_spectrum(
            tddft_cm,
            tddft_fosc,
            wavelength_shift_nm=20.0,
            sigma_nm=10.0,
            start_nm=350,
            end_nm=450,
            step_nm=1,
        )
        max_pt_shift = max(spectrum_shift, key=lambda p: p["intensity"])
        assert max_pt_shift["wavelength_nm"] == pytest.approx(420.0, abs=1.0)

    def test_job_to_web_json(self) -> None:
        """Web JSON payload must include excited states, CDFT descriptors, and 3D coords."""

        job = JobData(
            elements=["O", "H", "H"],
            coords=[(0.0, 0.0, 0.0), (0.0, 0.7, 0.5), (0.0, -0.7, 0.5)],
            tddft_cm=[30000.0],
            tddft_fosc=[0.2],
            dipole_moment_debye=1.85,
        )
        web_json = job_to_web_json(job, "water", job_index=1)

        assert web_json["molecule"] == "water"
        assert web_json["elements"] == ["O", "H", "H"]
        assert len(web_json["transitions"]) == 1  # type: ignore[arg-type]
        assert web_json["dipole_moment_debye"] == pytest.approx(1.85)

    def test_molecule_to_web_json(self) -> None:
        """Molecule container serialization."""

        job = JobData(elements=["N"], coords=[(0.0, 0.0, 0.0)])
        mol = MoleculeData(name="nitrogen", jobs=[job], sources=["n.out"])
        mol_json = molecule_to_web_json(mol)

        assert mol_json["name"] == "nitrogen"
        assert len(mol_json["jobs"]) == 1  # type: ignore[arg-type]
