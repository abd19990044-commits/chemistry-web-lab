"""Regression cases found by the October 2026 full project review."""

import json
import math
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("failure", ["engine", "orchestrator"])
def test_readiness_rejects_unavailable_core_components(monkeypatch, failure):
    from services import health_service

    health = {"storage_writable": True, "orchestrator": True, "orca_engine": True}
    if failure == "engine":
        health["orca_engine"] = False
    else:
        health["orchestrator_error"] = "database unavailable"
    monkeypatch.setattr(health_service, "build_health", lambda **kwargs: health)
    result = health_service.build_ready(orchestrator_available=True, orca_engine_available=True)
    assert result["ready"] is False
    assert result["checks"]["orca_engine" if failure == "engine" else "orchestrator"] is False


def test_health_checks_actual_state_storage(monkeypatch, tmp_path):
    import kaggle_runner
    import orca_orchestrator
    from services import health_service

    monkeypatch.setattr(kaggle_runner, "cli_health", lambda: {"ok": True})
    service = SimpleNamespace(health=lambda: {"ok": True, "config": {"state_dir": str(tmp_path / "missing")}})
    monkeypatch.setattr(orca_orchestrator, "get_service", lambda: service)
    result = health_service.build_health(orchestrator_available=True, orca_engine_available=True)
    assert result["storage_writable"] is False


def _species(gibbs=-1.0, temperature=298.15):
    return {
        "nu": 1,
        "role": "product",
        "display_name": "test",
        "final_result": {
            "complete_thermochemistry": True,
            "E_final": -1.0,
            "H_final": -0.9,
            "G_final": gibbs,
            "temperature_k": temperature,
            "entropy_j_mol_k": 10.0,
            "zpe_hartree": 0.01,
        },
    }


@pytest.mark.parametrize("gibbs", [-5.0, 5.0])
def test_extreme_equilibrium_exports_strict_json(gibbs):
    from services.reaction_workflow_service import compute_reaction_thermodynamics

    result = compute_reaction_thermodynamics([_species(gibbs)])
    assert result["k"] is None
    assert result["k_note"]
    assert math.isfinite(result["ln_k"]) and math.isfinite(result["log10_k"])
    json.dumps(result, allow_nan=False)


def test_missing_temperature_does_not_borrow_another_species_temperature():
    from services.reaction_workflow_service import compute_reaction_thermodynamics

    result = compute_reaction_thermodynamics([_species(), _species(temperature=None)])
    assert result["temperature_k"] is None
    assert result["ln_k"] is None and result["k"] is None
    assert any("temperature" in warning.lower() for warning in result["warnings"])


@pytest.mark.parametrize("field", ["G_final", "E_final", "H_final", "temperature_k", "entropy_j_mol_k"])
def test_nonfinite_thermochemistry_is_rejected(field):
    from services.reaction_workflow_service import ReactionValidationError, compute_reaction_thermodynamics

    species = _species()
    species["final_result"][field] = float("nan")
    with pytest.raises(ReactionValidationError):
        compute_reaction_thermodynamics([species])


def test_missing_optional_thermal_data_is_not_reported_as_zero():
    from services.reaction_workflow_service import compute_reaction_thermodynamics

    species = _species()
    species["final_result"]["entropy_j_mol_k"] = None
    species["final_result"]["zpe_hartree"] = None
    result = compute_reaction_thermodynamics([species])
    assert result["dS_j_mol_k"] is None
    assert result["dZPE_hartree"] is None and result["dE0_hartree"] is None
    assert math.isfinite(result["dG_hartree"])


def test_thermodynamics_table_handles_zero_and_unknown_values():
    script = r"""
    const fs=require('fs'), vm=require('vm'), assert=require('assert');
    const source=fs.readFileSync('static/js/reaction-unified.js','utf8');
    const body=source.split('function renderThermodynamicsResults(rxn) {')[1].split('// Set download links')[0];
    const make=()=>({children:[],classList:{remove(){}},appendChild(value){this.children.push(value);}});
    const table=make();
    const context={document:{getElementById(id){return id==='rxn-wf-thermo-tbody'?table:make();},createElement:make}};
    vm.createContext(context);
    vm.runInContext('function renderThermodynamicsResults(rxn) {'+body+'}',context);
    context.renderThermodynamicsResults({thermodynamics:{dE_elec_kj_mol:0,dH_kj_mol:0,dG_kj_mol:0,dS_j_mol_k:0,log10_k:null,k:null,k_note:'K > 1e300'}});
    assert.deepStrictEqual(table.children[0].children.map(x=>x.textContent),['0.00','0.00','0.00','0.00','-','K > 1e300']);
    """
    result = subprocess.run(
        ["node", "-e", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
