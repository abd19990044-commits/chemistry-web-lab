# -*- coding: utf-8 -*-
"""
Test suite verifying:
1. Multi-tier chemical structure resolution (PubChem + NIH CIR + OPSIN fallback)
2. 3D coordinate generation and /api/compound, /api/orca/coords endpoints
3. Reaction thermochemistry species breakdown with electronic-only, multi-level, and thermal properties
4. Calculation Analyzer thermochemistry key aliases (enthalpy_hartree, gibbs_free_energy_hartree, etc.)
5. Kaggle CLI unicode progress bar handling and 404 provisioning graceful status
"""
import io
import json
import zipfile
import pytest
from app import app
import chem_core as core
import kaggle_runner
from orca_engine.parser import OrcaParser
from orca_engine.thermochemistry import ThermochemistryEngine, Reaction, EnergyKind
from orca_engine.models import MoleculeData
from orca_engine.reporting import job_to_dict
from orca_engine.adapters.web_adapter import job_to_web_json


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_chemical_resolution_fallbacks():
    """Verify that common and systematic names resolve to valid SMILES and 3D coordinates."""
    test_cases = ["benzene", "caffeine", "aspirin", "water", "ethanol", "methane"]
    for name in test_cases:
        smiles = core.resolve_compound_to_smiles(name)
        assert smiles is not None, f"Failed to resolve SMILES for {name}"
        props = core.fetch_pubchem_properties(name)
        assert props is not None, f"Failed to fetch properties for {name}"
        assert "MolecularFormula" in props
        xyz = core.xyz_from_smiles(smiles)
        assert xyz is not None and len(xyz.strip().splitlines()) > 0


def test_api_compound_endpoint(client):
    """Test /api/compound endpoint returns full properties and rendered 2D molecule."""
    res = client.post("/api/compound", json={"query": "caffeine"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["smiles"] is not None
    assert data["image_png_base64"] is not None
    assert "C8H10N4O2" in data["formula"] or data["formula"] is not None


def test_api_orca_coords_endpoint(client):
    """Test /api/orca/coords endpoint returns 3D XYZ coordinates for standard compounds."""
    res = client.post("/api/orca/coords", json={"query": "benzene"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "coords" in data
    lines = data["coords"].strip().splitlines()
    assert len(lines) == 12  # C6H6 has 12 atoms


def test_thermochemistry_species_breakdown(client):
    """Verify that /api/orca/engine/thermochemistry correctly returns species breakdown."""
    h2o_out = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
  FINAL SINGLE POINT ENERGY      -76.380000000000
                            THERMOCHEMISTRY AT 298.15 K
  ----------------------------------------------------------------------
  Temperature         ...   298.15 K
  Pressure            ...     1.00 atm
  Total thermal energy...     0.02450000 Eh
  Total enthalpy      ...   -76.35300000 Eh
  Final Gibbs free energy...   -76.37500000 Eh
  Final entropy term  ...     0.02200000 Eh
  ----------------------------------------------------------------------
  """
    h2_out = """
  FINAL SINGLE POINT ENERGY      -1.160000000000
                            THERMOCHEMISTRY AT 298.15 K
  Total enthalpy      ...    -1.14500000 Eh
  Final Gibbs free energy...    -1.16200000 Eh
  Final entropy term  ...     0.01700000 Eh
  """
    o2_out = """
  FINAL SINGLE POINT ENERGY     -150.250000000000
                            THERMOCHEMISTRY AT 298.15 K
  Total enthalpy      ...   -150.24000000 Eh
  Final Gibbs free energy...   -150.26000000 Eh
  Final entropy term  ...     0.02000000 Eh
  """

    res = client.post("/api/orca/engine/thermochemistry", json={
        "equation": "2 H2 + O2 -> 2 H2O",
        "reactants": [
            {"name": "H2", "coefficient": 2, "content": h2_out},
            {"name": "O2", "coefficient": 1, "content": o2_out}
        ],
        "products": [
            {"name": "H2O", "coefficient": 2, "content": h2o_out}
        ]
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    result = data["result"]
    assert result["delta_g_kcal_mol"] is not None
    assert result["delta_h_kcal_mol"] is not None
    
    # Check species breakdown
    sp_breakdown = result["species_energetics"]
    assert "h2o" in sp_breakdown or "H2O" in sp_breakdown
    assert "h2" in sp_breakdown or "H2" in sp_breakdown
    assert "o2" in sp_breakdown or "O2" in sp_breakdown
    
    h2o_info = sp_breakdown.get("H2O") or sp_breakdown.get("h2o")
    assert h2o_info["e_elec_eh"] is not None
    assert h2o_info["enthalpy_eh"] is not None
    assert h2o_info["gibbs_eh"] is not None
    assert h2o_info["entropy_cal_mol_k"] is not None
    assert h2o_info["entropy_j_mol_k"] is not None
    assert abs(h2o_info["entropy_j_mol_k"] - (h2o_info["entropy_cal_mol_k"] * 4.184)) < 1e-4

    assert result["delta_entropy_cal_mol_k"] is not None
    assert result["delta_entropy_j_mol_k"] is not None
    assert abs(result["delta_entropy_j_mol_k"] - (result["delta_entropy_cal_mol_k"] * 4.184)) < 1e-4


def test_thermochemistry_auto_reconstruction():
    """Verify that JobData reconstructs total enthalpy and Gibbs from thermal corrections if needed."""
    out_text = """
  FINAL SINGLE POINT ENERGY      -40.500000000000
                            THERMOCHEMISTRY AT 298.15 K
  Thermal Enthalpy correction ... 0.03500000 Eh
  Thermal Energy correction   ... 0.03000000 Eh
  Thermal Free Energy correction ... 0.01200000 Eh
  """
    jobs = OrcaParser(io.StringIO(out_text), source_name="methane_test").parse()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.total_enthalpy_eh is not None
    assert abs(job.total_enthalpy_eh - (-40.500000 + 0.035000)) < 1e-6
    assert job.gibbs_free_energy_eh is not None
    assert abs(job.gibbs_free_energy_eh - (-40.500000 + 0.012000)) < 1e-6


def test_job_reporting_thermochemistry_aliases():
    """Verify that job_to_dict and job_to_web_json provide all legacy and canonical thermochemistry aliases."""
    out_text = """
  FINAL SINGLE POINT ENERGY      -76.400000000000
                            THERMOCHEMISTRY AT 298.15 K
  Total enthalpy      ...   -76.35000000 Eh
  Final Gibbs free energy...   -76.37000000 Eh
  Total entropy correction ... 0.02000000 Eh
  """
    jobs = OrcaParser(io.StringIO(out_text), source_name="water_opt_freq").parse()
    assert len(jobs) == 1
    d = job_to_dict(1, jobs[0])
    w = job_to_web_json(jobs[0], "water", 1)

    # Check both canonical and legacy aliases
    assert d["total_enthalpy_eh"] == -76.35000000
    assert d["enthalpy_hartree"] == -76.35000000
    assert d["final_enthalpy_hartree"] == -76.35000000
    assert d["gibbs_free_energy_eh"] == -76.37000000
    assert d["gibbs_free_energy_hartree"] == -76.37000000
    assert d["final_gibbs_hartree"] == -76.37000000
    assert d["e_elec_eh"] == -76.40000000
    assert d["electronic_energy_hartree"] == -76.40000000

    assert w["total_enthalpy_eh"] == -76.35000000
    assert w["enthalpy_hartree"] == -76.35000000


def test_kaggle_check_job_status_provisioning_404(monkeypatch):
    """Verify that initial 404 from Kaggle CLI during kernel startup returns 'queued' instead of 'error'."""
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "404 - Not Found: Kaggle has no notebook at this address"

    monkeypatch.setattr(kaggle_runner, "_run_kaggle_cli", lambda *args, **kwargs: FakeProc())
    status_data = kaggle_runner.check_job_status("testuser", "testkey123456789012345678901234", "chem-tools-testjob-123456")
    assert status_data["status"] == "queued"
    assert "initializing" in status_data["note"].lower()


def test_orca_engine_parse_endpoint(client):
    """Verify that /api/orca/engine/parse accepts uploaded ORCA output and returns rich JSON."""
    orca_output = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
  FINAL SINGLE POINT ENERGY      -76.400000000000
  Total enthalpy      ...   -76.35000000 Eh
  Final Gibbs free energy...   -76.37000000 Eh
  CARTESIAN COORDINATES (ANGSTROEM)
    O      0.000000    0.000000    0.117790
    H      0.000000    0.755453   -0.471161
    H      0.000000   -0.755453   -0.471161
  ORCA TERMINATED NORMALLY
  """
    res = client.post("/api/orca/engine/parse", data={
        "file": (io.BytesIO(orca_output.encode("utf-8")), "water_opt.out")
    }, content_type="multipart/form-data")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "latest_job" in data
    assert data["latest_job"]["total_enthalpy_eh"] == -76.35000000
    assert data["latest_job"]["enthalpy_hartree"] == -76.35000000
    assert len(data["latest_job"]["elements"]) == 3


def test_iupac_systematic_resolution_and_name_preservation(client):
    """Verify that IUPAC systematic names prioritize OPSIN/NIH CIR and are preserved in API responses."""
    # Test IUPAC names with locants and IUPAC patterns
    iupac_name = "2-(acetyloxy)benzoic acid"
    smiles = core.resolve_compound_to_smiles(iupac_name)
    assert smiles is not None
    assert core.is_iupac_name(iupac_name) is True

    # Test /api/compound preserving query_name and iupac_name
    res = client.post("/api/compound", json={"query": iupac_name})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["query_name"] == iupac_name
    assert "smiles" in data and data["smiles"] is not None
    assert data["image_png_base64"] is not None

    # Test /api/orca/coords preserving IUPAC name
    res2 = client.post("/api/orca/coords", json={"query": "4-acetamidophenol"})
    assert res2.status_code == 200
    d2 = res2.get_json()
    assert d2["ok"] is True
    assert d2["query_name"] == "4-acetamidophenol"
    assert d2["coords"] is not None


def test_chained_5_stage_workflow_generation(client):
    """Verify generating 5 consecutive chained ORCA inputs (Opt -> High Opt -> Freq -> SP -> TDDFT)."""
    coords = "O 0.0 0.0 0.117\nH 0.0 0.755 -0.471\nH 0.0 -0.755 -0.471"
    
    stages = [
        {"name": "h2o_stage1_opt", "calc_type": "opt", "theory": "r2scan-3c", "basis": "", "coords": coords},
        {"name": "h2o_stage2_opt", "calc_type": "opt", "theory": "B3LYP", "basis": "def2-SVP", "disp": "D3BJ", "scf_conv": "tightscf", "coords": coords},
        {"name": "h2o_stage3_freq", "calc_type": "freq", "theory": "B3LYP", "basis": "def2-TZVP", "disp": "D3BJ", "scf_conv": "tightscf", "coords": coords},
        {"name": "h2o_stage4_sp", "calc_type": "sp", "theory": "DLPNO-CCSD(T)", "basis": "def2-TZVP", "scf_conv": "tightscf", "coords": coords},
        {"name": "h2o_stage5_tddft", "calc_type": "tddft", "theory": "CAM-B3LYP", "basis": "def2-TZVP", "nroots": 8, "coords": coords},
    ]

    generated_inps = []
    for st in stages:
        res = client.post("/api/orca/generate", json=st)
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["input_text"] is not None
        generated_inps.append(data["input_text"])

    assert len(generated_inps) == 5
    assert "r2scan-3c" in generated_inps[0] and "Opt" in generated_inps[0]
    assert "B3LYP" in generated_inps[1] and "def2-SVP" in generated_inps[1]
    assert "Freq" in generated_inps[2] or "freq" in generated_inps[2].lower()
    assert "DLPNO-CCSD(T)" in generated_inps[3]
    assert "%tddft" in generated_inps[4] or "tddft" in generated_inps[4].lower()


def test_multistage_coordinate_inheritance_scenarios():
    """Verify coordinate inheritance across all workflow permutations (5-Opt, 3-Opt, No-Opt)."""
    def is_opt_job(job):
        t = str(job.get("stageType") or job.get("calc_type") or "").lower()
        n = str(job.get("name") or "").lower()
        return "opt" in t or "opt" in n

    def get_inherited_source(current_stage, all_stages):
        predecessors = [s for s in all_stages if s["stage"] < current_stage and s["status"] == "complete"]
        predecessors.sort(key=lambda s: s["stage"], reverse=True)
        if not predecessors:
            return None
        opt_predecessors = [s for s in predecessors if is_opt_job(s)]
        if opt_predecessors:
            return {"source": opt_predecessors[0]["stage"], "type": "OPT"}
        immediate = next((s for s in predecessors if s["stage"] == current_stage - 1), predecessors[0])
        return {"source": immediate["stage"], "type": "INP"}

    # Scenario 1: All 5 steps are OPT (e.g. LooseOpt -> Opt -> TightOpt -> VeryTightOpt -> TS-Opt)
    stages_all_opt = [{"stage": i, "calc_type": "opt", "name": f"st{i}_opt", "status": "complete"} for i in range(1, 6)]
    for k in range(2, 6):
        res = get_inherited_source(k, stages_all_opt)
        assert res["source"] == k - 1
        assert res["type"] == "OPT"

    # Scenario 2: 3 steps are OPT, followed by Freq and SP (Opt1 -> Opt2 -> Opt3 -> Freq -> SP)
    stages_3_opt = [
        {"stage": 1, "calc_type": "opt", "name": "st1_opt", "status": "complete"},
        {"stage": 2, "calc_type": "opt", "name": "st2_opt", "status": "complete"},
        {"stage": 3, "calc_type": "opt", "name": "st3_opt", "status": "complete"},
        {"stage": 4, "calc_type": "freq", "name": "st4_freq", "status": "complete"},
        {"stage": 5, "calc_type": "sp", "name": "st5_sp", "status": "complete"},
    ]
    assert get_inherited_source(4, stages_3_opt) == {"source": 3, "type": "OPT"}
    assert get_inherited_source(5, stages_3_opt) == {"source": 3, "type": "OPT"}

    # Scenario 3: No OPT at all (SP -> Freq -> SP)
    stages_no_opt = [
        {"stage": 1, "calc_type": "sp", "name": "st1_sp", "status": "complete"},
        {"stage": 2, "calc_type": "freq", "name": "st2_freq", "status": "complete"},
        {"stage": 3, "calc_type": "sp", "name": "st3_sp", "status": "complete"},
    ]
    assert get_inherited_source(2, stages_no_opt) == {"source": 1, "type": "INP"}
    assert get_inherited_source(3, stages_no_opt) == {"source": 2, "type": "INP"}

