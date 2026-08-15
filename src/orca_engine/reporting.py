"""Report builders and writers for parsed ORCA data."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from orca_engine.models import (
    BDEResult,
    ConsistencyReport,
    JobData,
    MoleculeData,
    ReactionResult,
)


def job_to_dict(index: int, job: JobData) -> dict[str, Any]:
    """Convert one parsed job to a JSON-serializable dictionary.

    Args:
        index: One-based job index within the molecule.
        job: Parsed job record.

    Returns:
        Dictionary containing scalar job data and frontier orbital values.
    """

    return {
        "job": index,
        "orca_version": job.metadata.orca_version,
        "basis_set": job.metadata.basis_set,
        "solvation": job.metadata.solvation,
        "charge": job.metadata.charge,
        "multiplicity": job.metadata.multiplicity,
        "temperature_k": job.metadata.temperature_k,
        "pressure_atm": job.metadata.pressure_atm,
        "e_elec_eh": job.e_elec_eh,
        "zpe_eh": job.zpe_eh,
        "electronic_zpe_eh": job.electronic_zpe_eh(),
        "gibbs_free_energy_eh": job.gibbs_free_energy_eh,
        "total_enthalpy_eh": job.total_enthalpy_eh,
        "entropy_term_ts_eh": job.entropy_term_eh,
        "entropy_correction_minus_ts_eh": job.entropy_correction_eh,
        "homo_ev": job.homo_ev,
        "lumo_ev": job.lumo_ev,
        "homo_lumo_gap_ev": job.homo_lumo_gap_ev,
        "alpha_homo_ev": job.alpha_homo_ev,
        "alpha_lumo_ev": job.alpha_lumo_ev,
        "beta_homo_ev": job.beta_homo_ev,
        "beta_lumo_ev": job.beta_lumo_ev,
        "dipole_moment_debye": job.dipole_moment_debye,
        "s2_actual": job.s2_actual,
        "s2_ideal": job.s2_ideal,
        "spin_contamination": job.spin_contamination,
        "imaginary_frequencies_count": job.imaginary_frequencies_count,
        "imaginary_frequencies_cm": job.imaginary_frequencies_cm,
        "is_transition_state": job.is_transition_state,
        "ionization_potential_ev": job.ionization_potential_ev,
        "electron_affinity_ev": job.electron_affinity_ev,
        "chemical_hardness_ev": job.chemical_hardness_ev,
        "chemical_potential_ev": job.chemical_potential_ev,
        "electronegativity_ev": job.electronegativity_ev,
        "chemical_softness_ev": job.chemical_softness_ev,
        "electrophilicity_index_ev": job.electrophilicity_index_ev,
        "electrodonating_power_ev": job.electrodonating_power_ev,
        "electroaccepting_power_ev": job.electroaccepting_power_ev,
        "net_electrophilicity_ev": job.net_electrophilicity_ev,
        "formula": _format_formula(job),
        "atoms_count": len(job.elements),
        "coords_unit": job.coords_unit.value if job.coords_unit else None,
        "tddft_states": len(job.tddft_cm),
        "terminated_normally": job.terminated_normally,
        "termination_message": job.termination_message,
        "had_error_termination": job.had_error_termination,
        "error_count": len(job.error_messages),
    }


def _format_formula(job: JobData) -> str | None:
    """Return a Hill-ordered empirical formula for a parsed structure."""

    counts = job.stoichiometry()
    if not counts:
        return None
    ordered: list[str] = []
    for element in ("C", "H"):
        if element in counts:
            ordered.append(f"{element}{counts[element] if counts[element] > 1 else ''}")
    for element in sorted(set(counts) - {"C", "H"}):
        ordered.append(f"{element}{counts[element] if counts[element] > 1 else ''}")
    return "".join(ordered)


def consistency_to_dict(report: ConsistencyReport) -> dict[str, Any]:
    """Convert a consistency report to a JSON-serializable dictionary."""

    return {
        "atom_balanced": report.atom_balanced,
        "charge_balanced": report.charge_balanced,
        "atom_imbalance": report.atom_imbalance,
        "charge_imbalance": report.charge_imbalance,
        "levels_of_theory": [
            {"orca_version": version, "basis_set": basis, "solvation": solvation}
            for version, basis, solvation in report.mixed_levels_of_theory
        ],
        "temperatures_k": report.mixed_temperatures_k,
        "species_with_errors": report.species_with_errors,
        "warnings": report.warnings,
    }


def build_report(
    molecules: dict[str, MoleculeData],
    reaction_result: ReactionResult | None = None,
    bde_result: BDEResult | None = None,
) -> dict[str, Any]:
    """Build a complete report dictionary.

    Args:
        molecules: Parsed molecule mapping.
        reaction_result: Optional reaction thermochemistry result.
        bde_result: Optional bond dissociation energy result.

    Returns:
        JSON-serializable report structure.
    """

    report: dict[str, Any] = {
        "molecules": {
            name: {
                "sources": molecule.sources,
                # Aggregated over every job, because a rerun appended to the
                # same file leaves the final job clean while an earlier one
                # failed.
                "had_error_termination": molecule.had_error_termination,
                "levels_of_theory": [
                    {"orca_version": version, "basis_set": basis, "solvation": solvation}
                    for version, basis, solvation in molecule.levels_of_theory()
                ],
                "jobs": [job_to_dict(index, job) for index, job in enumerate(molecule.jobs, 1)],
            }
            for name, molecule in molecules.items()
        }
    }
    if reaction_result is not None:
        report["reaction"] = reaction_result_to_dict(reaction_result)
    if bde_result is not None:
        report["bde"] = bde_result_to_dict(bde_result)
    return report


def reaction_result_to_dict(result: ReactionResult) -> dict[str, Any]:
    """Convert a reaction result to a JSON-serializable dictionary."""

    return {
        "equation": result.reaction.equation,
        "reactants": [
            {"coefficient": term.coefficient, "molecule": term.molecule_name}
            for term in result.reaction.reactants
        ],
        "products": [
            {"coefficient": term.coefficient, "molecule": term.molecule_name}
            for term in result.reaction.products
        ],
        "delta_electronic_kcal_mol": result.delta_electronic_kcal_mol,
        "delta_e0_kcal_mol": result.delta_e0_kcal_mol,
        "delta_g_kcal_mol": result.delta_g_kcal_mol,
        "delta_h_kcal_mol": result.delta_h_kcal_mol,
        "delta_entropy_cal_mol_k": result.delta_entropy_cal_mol_k,
        "equilibrium_constant_keq": result.equilibrium_constant_keq,
        "temperature_k": result.temperature_k,
        "is_exergonic": result.is_exergonic,
        "missing_references": result.missing_references,
        "consistency": consistency_to_dict(result.consistency),
    }


def bde_result_to_dict(result: BDEResult) -> dict[str, Any]:
    """Convert a BDE result to a JSON-serializable dictionary."""

    return {
        "equation": result.reaction.equation,
        "energy_kind": result.energy_kind.value,
        "reactants": [
            {"coefficient": term.coefficient, "molecule": term.molecule_name}
            for term in result.reaction.reactants
        ],
        "products": [
            {"coefficient": term.coefficient, "molecule": term.molecule_name}
            for term in result.reaction.products
        ],
        "bde_kcal_mol": result.bde_kcal_mol,
        "missing_references": result.missing_references,
        "consistency": consistency_to_dict(result.consistency),
    }


def write_report(report: dict[str, Any], output_path: Path, output_format: str) -> None:
    """Write a report as JSON or CSV.

    Args:
        report: Report dictionary from :func:`build_report`.
        output_path: Destination file path.
        output_format: Either ``"json"`` or ``"csv"``.

    Raises:
        ValueError: If ``output_format`` is unsupported.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        write_json_report(report, output_path)
        return
    if output_format == "csv":
        write_csv_report(report, output_path)
        return
    raise ValueError(f"Unsupported report format: {output_format}")


def write_json_report(report: dict[str, Any], output_path: Path) -> None:
    """Write a pretty JSON report."""

    with output_path.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


def write_csv_report(report: dict[str, Any], output_path: Path) -> None:
    """Write a flat CSV report with job and optional reaction rows."""

    fieldnames = [
        "record_type",
        "molecule",
        "sources",
        "job",
        "orca_version",
        "basis_set",
        "solvation",
        "charge",
        "multiplicity",
        "temperature_k",
        "pressure_atm",
        "e_elec_eh",
        "zpe_eh",
        "electronic_zpe_eh",
        "gibbs_free_energy_eh",
        "total_enthalpy_eh",
        "entropy_term_ts_eh",
        "entropy_correction_minus_ts_eh",
        "homo_ev",
        "lumo_ev",
        "homo_lumo_gap_ev",
        "alpha_homo_ev",
        "alpha_lumo_ev",
        "beta_homo_ev",
        "beta_lumo_ev",
        "dipole_moment_debye",
        "s2_actual",
        "s2_ideal",
        "spin_contamination",
        "imaginary_frequencies_count",
        "is_transition_state",
        "ionization_potential_ev",
        "electron_affinity_ev",
        "chemical_hardness_ev",
        "chemical_potential_ev",
        "electronegativity_ev",
        "chemical_softness_ev",
        "electrophilicity_index_ev",
        "electrodonating_power_ev",
        "electroaccepting_power_ev",
        "net_electrophilicity_ev",
        "formula",
        "atoms_count",
        "coords_unit",
        "tddft_states",
        "terminated_normally",
        "termination_message",
        "had_error_termination",
        "error_count",
        "reaction_equation",
        "delta_electronic_kcal_mol",
        "delta_e0_kcal_mol",
        "delta_g_kcal_mol",
        "delta_h_kcal_mol",
        "delta_entropy_cal_mol_k",
        "equilibrium_constant_keq",
        "is_exergonic",
        "missing_references",
        "consistency_warnings",
        "bde_equation",
        "bde_energy_kind",
        "bde_kcal_mol",
        "bde_missing_references",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in _iter_csv_rows(report):
            writer.writerow(row)


def _iter_csv_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return flat CSV rows from a report dictionary."""

    rows: list[dict[str, Any]] = []
    molecules = report.get("molecules", {})
    if isinstance(molecules, dict):
        for molecule_name, payload in molecules.items():
            if not isinstance(payload, dict):
                continue
            sources = payload.get("sources", [])
            source_text = (
                ";".join(str(source) for source in sources) if isinstance(sources, list) else ""
            )
            jobs = payload.get("jobs", [])
            if not isinstance(jobs, list):
                continue
            for job in jobs:
                if isinstance(job, dict):
                    row = {"record_type": "job", "molecule": molecule_name, "sources": source_text}
                    row.update(job)
                    rows.append(row)

    reaction = report.get("reaction")
    if isinstance(reaction, dict):
        rows.append(
            {
                "record_type": "reaction",
                "reaction_equation": reaction.get("equation"),
                "temperature_k": reaction.get("temperature_k"),
                "delta_electronic_kcal_mol": reaction.get("delta_electronic_kcal_mol"),
                "delta_e0_kcal_mol": reaction.get("delta_e0_kcal_mol"),
                "delta_g_kcal_mol": reaction.get("delta_g_kcal_mol"),
                "delta_h_kcal_mol": reaction.get("delta_h_kcal_mol"),
                "delta_entropy_cal_mol_k": reaction.get("delta_entropy_cal_mol_k"),
                "equilibrium_constant_keq": reaction.get("equilibrium_constant_keq"),
                "is_exergonic": reaction.get("is_exergonic"),
                "missing_references": ";".join(reaction.get("missing_references", [])),
                "consistency_warnings": _join_warnings(reaction),
            }
        )
    bde = report.get("bde")
    if isinstance(bde, dict):
        rows.append(
            {
                "record_type": "bde",
                "bde_equation": bde.get("equation"),
                "bde_energy_kind": bde.get("energy_kind"),
                "bde_kcal_mol": bde.get("bde_kcal_mol"),
                "bde_missing_references": ";".join(bde.get("missing_references", [])),
                "consistency_warnings": _join_warnings(bde),
            }
        )
    return rows


def _join_warnings(payload: dict[str, Any]) -> str:
    """Return the consistency warnings of a result block as one CSV cell."""

    consistency = payload.get("consistency")
    if not isinstance(consistency, dict):
        return ""
    warnings = consistency.get("warnings", [])
    return " | ".join(str(warning) for warning in warnings) if isinstance(warnings, list) else ""
