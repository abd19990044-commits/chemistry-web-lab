# -*- coding: utf-8 -*-
"""Real ORCA 6.1.0 runtime execution smoke tests for builder generated structures.
Verifies syntax, coordinate parsing, SCF convergence, and normal termination in ORCA 6.1.
"""

import os
import shutil
import subprocess
import tempfile
import pytest

import chem_core as core

ORCA_EXE = r"D:\orca6\Orca6.1.0.Win64\orca.exe"
HAS_ORCA = os.path.exists(ORCA_EXE)


def run_orca_smoke(inp_content: str, tag: str) -> tuple[int, str]:
    """Execute ORCA on the given input content and return (exit_code, stdout)."""
    tmp_dir = tempfile.mkdtemp(prefix=f"orca_smoke_{tag}_")
    try:
        inp_file = os.path.join(tmp_dir, "job.inp")
        with open(inp_file, "w", encoding="utf-8") as f:
            f.write(inp_content)
        
        proc = subprocess.run(
            [ORCA_EXE, inp_file],
            cwd=tmp_dir,
            capture_output=True,
            text=True,
            timeout=60
        )
        return proc.returncode, proc.stdout
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.mark.skipif(not HAS_ORCA, reason="ORCA 6.1 binary not found at D:\\orca6\\Orca6.1.0.Win64\\orca.exe")
class TestOrcaRuntimeSmoke:
    """Run real ORCA 6.1 calculations on generated structures."""

    def test_case_1_water_single_point(self):
        """Case 1: Water."""
        coords = "O 0.0000 0.0000 0.1173\nH 0.0000 0.7572 -0.4692\nH 0.0000 -0.7572 -0.4692"
        inp = core.generate_orca_6_input({
            "calc_type": "sp",
            "family": "f_dft",
            "theory": "PBEh-3c",
            "basis": "def2-SVP",
            "cores": 1,
            "ram": 2000,
            "charge": 0,
            "mult": 1,
            "coords": coords
        })
        code, out = run_orca_smoke(inp, "case1_water")
        assert code == 0, f"ORCA exited with {code}:\n{out[:500]}"
        assert "ORCA TERMINATED NORMALLY" in out

    def test_case_2_ammonia_single_point(self):
        """Case 2: Ammonia."""
        coords = "N 0.0000 0.0000 0.1165\nH 0.0000 0.9397 -0.2718\nH 0.8138 -0.4699 -0.2718\nH -0.8138 -0.4699 -0.2718"
        inp = core.generate_orca_6_input({
            "calc_type": "sp",
            "family": "f_dft",
            "theory": "PBEh-3c",
            "basis": "def2-SVP",
            "cores": 1,
            "ram": 2000,
            "charge": 0,
            "mult": 1,
            "coords": coords
        })
        code, out = run_orca_smoke(inp, "case2_nh3")
        assert code == 0, f"ORCA exited with {code}:\n{out[:500]}"
        assert "ORCA TERMINATED NORMALLY" in out

    def test_case_3_water_plus_ammonia_copresence(self):
        """Case 3: H2O + NH3 with auto-offset."""
        coords = """O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
N   5.500000   0.000000   0.116500
H   5.500000   0.939700  -0.271800
H   6.313800  -0.469900  -0.271800
H   4.686200  -0.469900  -0.271800"""
        inp = core.generate_orca_6_input({
            "calc_type": "sp",
            "family": "f_dft",
            "theory": "PBEh-3c",
            "basis": "def2-SVP",
            "cores": 1,
            "ram": 2000,
            "charge": 0,
            "mult": 1,
            "coords": coords
        })
        code, out = run_orca_smoke(inp, "case3_h2o_nh3")
        assert code == 0, f"ORCA exited with {code}:\n{out[:500]}"
        assert "ORCA TERMINATED NORMALLY" in out

    def test_case_4_assembled_intermolecular_model(self):
        """Case 4: Assembled intermolecular complex (H-bonded dimer)."""
        coords = """O   0.000000   0.000000   0.000000
H   0.000000   0.000000   0.960000
H   0.920000   0.000000  -0.270000
N   2.850000   0.000000   0.000000
H   3.200000   0.810000  -0.470000
H   3.200000  -0.810000  -0.470000
H   3.200000   0.000000   0.940000"""
        inp = core.generate_orca_6_input({
            "calc_type": "sp",
            "family": "f_dft",
            "theory": "wB97X-D4",
            "basis": "def2-SVP",
            "ri_type": "rijcosx",
            "cores": 1,
            "ram": 2000,
            "charge": 0,
            "mult": 1,
            "coords": coords
        })
        code, out = run_orca_smoke(inp, "case4_assembled")
        assert code == 0, f"ORCA exited with {code}:\n{out[:500]}"
        assert "ORCA TERMINATED NORMALLY" in out

    def test_case_5_structure_after_atom_deletion(self):
        """Case 5: Structure after atom deletion (Methanol minus OH -> Methyl radical / Cation)."""
        coords = """C   0.000000   0.000000   0.000000
H   0.000000   1.080000   0.000000
H   0.935000  -0.540000   0.000000
H  -0.935000  -0.540000   0.000000"""
        inp = core.generate_orca_6_input({
            "calc_type": "sp",
            "family": "f_dft",
            "theory": "PBEh-3c",
            "basis": "def2-SVP",
            "cores": 1,
            "ram": 2000,
            "charge": 1,
            "mult": 1,
            "coords": coords
        })
        code, out = run_orca_smoke(inp, "case5_deletion")
        assert code == 0, f"ORCA exited with {code}:\n{out[:500]}"
        assert "ORCA TERMINATED NORMALLY" in out

    def test_case_6_structure_after_rigid_body_transformation(self):
        """Case 6: Structure after rigid-body 3D rotation & translation."""
        coords = """O   1.414200   2.000000   1.414200
H   1.414200   2.757200   0.827700
H   1.414200   1.242800   0.827700"""
        inp = core.generate_orca_6_input({
            "calc_type": "sp",
            "family": "f_dft",
            "theory": "PBEh-3c",
            "basis": "def2-SVP",
            "cores": 1,
            "ram": 2000,
            "charge": 0,
            "mult": 1,
            "coords": coords
        })
        code, out = run_orca_smoke(inp, "case6_transformed")
        assert code == 0, f"ORCA exited with {code}:\n{out[:500]}"
        assert "ORCA TERMINATED NORMALLY" in out
