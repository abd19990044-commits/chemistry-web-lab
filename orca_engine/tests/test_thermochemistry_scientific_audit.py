import math
import pytest
from pathlib import Path
from orca_engine.parser import OrcaParser
from orca_engine.models import JobData, MoleculeData, Reaction, ReactionResult, ConsistencyReport
from orca_engine.thermochemistry import ThermochemistryEngine
from orca_engine.constants import PhysConst

class TestScientificAudit:
    """Scientific audit tests for parsing, consistency, and thermodynamics."""

    def test_parse_quasi_rrho_grimme(self):
        content = "The quasi-RRHO method of Grimme is used\nFrequency cutoff for the RRHO ... 100.00 cm**-1\nFINAL SINGLE POINT ENERGY -1.0"
        parser = OrcaParser(content.splitlines())
        jobs = parser.parse()
        assert jobs[0].metadata.quasi_rrho_treatment == "Grimme-qRRHO"
        assert jobs[0].metadata.quasi_rrho_cutoff_cm == 100.0

    def test_parse_quasi_rrho_standard(self):
        content = "The Standard-RRHO method is used\nFINAL SINGLE POINT ENERGY -1.0"
        parser = OrcaParser(content.splitlines())
        jobs = parser.parse()
        assert jobs[0].metadata.quasi_rrho_treatment == "Standard-RRHO"

    def test_total_thermal_energy_parsing(self):
        content = "Total thermal energy                  ...      -76.38137397 Eh\nFINAL SINGLE POINT ENERGY -1.0"
        jobs = OrcaParser(content.splitlines()).parse()
        assert jobs[0].thermal_energy_eh == -76.38137397

    def test_entropy_components_parsing(self):
        content = """
S_vib                   ...       11.660 cal/mol-K
S_rot                   ...       20.217 cal/mol-K
S_trans                 ...       34.611 cal/mol-K
FINAL SINGLE POINT ENERGY -1.0
        """
        jobs = OrcaParser(content.splitlines()).parse()
        assert jobs[0].vibrational_entropy_cal_mol_k == 11.660
        assert jobs[0].rotational_entropy_cal_mol_k == 20.217
        assert jobs[0].translational_entropy_cal_mol_k == 34.611

    def test_relativistic_zora_parsing(self):
        content = "ZORA IS SWITCHED ON\nFINAL SINGLE POINT ENERGY -1.0"
        jobs = OrcaParser(content.splitlines()).parse()
        assert jobs[0].metadata.relativistic == "ZORA"

    def test_relativistic_dkh_parsing(self):
        content = "DOUGLAS-KROLL-HESS-HAMILTONIAN IS SWITCHED ON\nFINAL SINGLE POINT ENERGY -1.0"
        jobs = OrcaParser(content.splitlines()).parse()
        assert jobs[0].metadata.relativistic == "DKH"

    def test_smd_solvent_parsing(self):
        content = "SMD solvent                           ... WATER\nFINAL SINGLE POINT ENERGY -1.0"
        jobs = OrcaParser(content.splitlines()).parse()
        assert jobs[0].metadata.solvation == "SMD"
        assert jobs[0].metadata.solvent == "WATER"

    def test_dispersion_correction_parsing_d4(self):
        content = "Dispersion correction                    ... D4\nFINAL SINGLE POINT ENERGY -1.0"
        jobs = OrcaParser(content.splitlines()).parse()
        assert jobs[0].metadata.dispersion == "D4"

    def test_stationary_point_likely_minimum(self):
        content = """CARTESIAN COORDINATES (ANGSTROEM)
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
VIBRATIONAL FREQUENCIES
   0:    0.00 cm**-1
   1:    0.00 cm**-1
   2:    0.00 cm**-1
   3:    0.00 cm**-1
   4:    0.00 cm**-1
   5:    0.00 cm**-1
   6: 1595.00 cm**-1
   7: 3657.00 cm**-1
   8: 3756.00 cm**-1
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
****ORCA TERMINATED NORMALLY****
FINAL SINGLE POINT ENERGY -76.400000"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert jobs[0].expected_total_frequencies_count == 9
        assert jobs[0].stationary_point_status == "LIKELY_MINIMUM"
        assert jobs[0].thermochemistry_reliability == "HIGH"

    def test_stationary_point_linear_molecule(self):
        content = """CARTESIAN COORDINATES (ANGSTROEM)
C   0.000000   0.000000   0.000000
O   0.000000   0.000000   1.160000
O   0.000000   0.000000  -1.160000
VIBRATIONAL FREQUENCIES
   0:    0.00 cm**-1
   1:    0.00 cm**-1
   2:    0.00 cm**-1
   3:    0.00 cm**-1
   4:    0.00 cm**-1
   5:  667.00 cm**-1
   6:  667.00 cm**-1
   7: 1388.00 cm**-1
   8: 2349.00 cm**-1
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
****ORCA TERMINATED NORMALLY****
FINAL SINGLE POINT ENERGY -188.000000"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert jobs[0].is_linear_geometry() is True
        assert jobs[0].expected_vibrational_modes_count == 4
        assert jobs[0].expected_total_frequencies_count == 9
        assert jobs[0].stationary_point_status == "LIKELY_MINIMUM"

    def test_stationary_point_transition_state(self):
        content = """CARTESIAN COORDINATES (ANGSTROEM)
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
VIBRATIONAL FREQUENCIES
   0:    0.00 cm**-1
   1:    0.00 cm**-1
   2:    0.00 cm**-1
   3:    0.00 cm**-1
   4:    0.00 cm**-1
   5:    0.00 cm**-1
   6: -500.00 cm**-1 ***imaginary mode***
   7: 1500.00 cm**-1
   8: 3600.00 cm**-1
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
****ORCA TERMINATED NORMALLY****
FINAL SINGLE POINT ENERGY -76.400000"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert jobs[0].imaginary_frequencies_count == 1
        assert jobs[0].stationary_point_status == "TRANSITION_STATE"
        assert jobs[0].thermochemistry_reliability == "TRANSITION_STATE"

    def test_stationary_point_higher_order_saddle(self):
        content = """CARTESIAN COORDINATES (ANGSTROEM)
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
VIBRATIONAL FREQUENCIES
   0:    0.00 cm**-1
   1:    0.00 cm**-1
   2:    0.00 cm**-1
   3:    0.00 cm**-1
   4:    0.00 cm**-1
   5:    0.00 cm**-1
   6: -500.00 cm**-1 ***imaginary mode***
   7: -200.00 cm**-1 ***imaginary mode***
   8: 3600.00 cm**-1
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
****ORCA TERMINATED NORMALLY****
FINAL SINGLE POINT ENERGY -76.400000"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert jobs[0].imaginary_frequencies_count == 2
        assert jobs[0].stationary_point_status == "HIGHER_ORDER_SADDLE"
        assert jobs[0].thermochemistry_reliability == "UNRELIABLE_FOR_MINIMUM"

    def test_stationary_point_moderately_truncated_frequencies(self):
        # H2O (N=3, expected 3N=9 modes), but only 5 modes printed
        content = """CARTESIAN COORDINATES (ANGSTROEM)
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
VIBRATIONAL FREQUENCIES
   0:    0.00 cm**-1
   1:    0.00 cm**-1
   2:    0.00 cm**-1
   3:    0.00 cm**-1
   4:    0.00 cm**-1
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
****ORCA TERMINATED NORMALLY****
FINAL SINGLE POINT ENERGY -76.400000"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert len(jobs[0].vibrational_frequencies_cm) == 5
        assert jobs[0].expected_total_frequencies_count == 9
        assert jobs[0].stationary_point_status == "INCOMPLETE_FREQUENCIES"
        assert jobs[0].thermochemistry_reliability == "INCOMPLETE_FREQUENCIES"

    def test_stationary_point_severely_truncated_frequencies(self):
        # H2O (N=3, expected 3N=9 modes), only 1 mode printed
        content = """CARTESIAN COORDINATES (ANGSTROEM)
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
VIBRATIONAL FREQUENCIES
   0: 1595.00 cm**-1
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
****ORCA TERMINATED NORMALLY****
FINAL SINGLE POINT ENERGY -76.400000"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert jobs[0].stationary_point_status == "INCOMPLETE_FREQUENCIES"
        assert jobs[0].thermochemistry_reliability == "INCOMPLETE_FREQUENCIES"

    def test_stationary_point_abnormal_termination(self):
        content = """CARTESIAN COORDINATES (ANGSTROEM)
O   0.000000   0.000000   0.117300
H   0.000000   0.757200  -0.469200
H   0.000000  -0.757200  -0.469200
VIBRATIONAL FREQUENCIES
   0:    0.00 cm**-1
   1:    0.00 cm**-1
   2:    0.00 cm**-1
   3:    0.00 cm**-1
   4:    0.00 cm**-1
   5:    0.00 cm**-1
   6: 1595.00 cm**-1
   7: 3657.00 cm**-1
   8: 3756.00 cm**-1
****ORCA TERMINATED ABNORMALLY****
An error has occurred in the SCF module"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert jobs[0].stationary_point_status == "FAILED_CALCULATION"
        assert jobs[0].thermochemistry_reliability == "FAILED_CALCULATION"

    def test_stationary_point_electronic_only(self):
        content = """FINAL SINGLE POINT ENERGY      -76.400000
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
****ORCA TERMINATED NORMALLY****"""
        jobs = OrcaParser(content.splitlines()).parse()
        assert len(jobs) == 1
        assert jobs[0].stationary_point_status == "NO_FREQUENCY_CALCULATION"
        assert jobs[0].thermochemistry_reliability == "ELECTRONIC_ONLY"

    def _make_engine(self, mol1_meta=None, mol2_meta=None):
        m1 = JobData()
        if mol1_meta: m1.metadata.__dict__.update(mol1_meta)
        m1.e_elec_eh = -1.0
        
        m2 = JobData()
        if mol2_meta: m2.metadata.__dict__.update(mol2_meta)
        m2.e_elec_eh = -2.0
        
        molecules = {
            "A": MoleculeData("A", jobs=[m1]),
            "B": MoleculeData("B", jobs=[m2]),
        }
        return ThermochemistryEngine(molecules)

    def test_pressure_mismatch_flagged(self):
        e = self._make_engine(mol1_meta={"pressure_atm": 1.0}, mol2_meta={"pressure_atm": 2.0})
        report = e.evaluate("A -> B").consistency
        assert not report.pressure_balanced
        assert any("PRESSURE_MISMATCH" in w for w in report.warnings)

    def test_pressure_mismatch_tolerance(self):
        e = self._make_engine(mol1_meta={"pressure_atm": 1.000}, mol2_meta={"pressure_atm": 1.005})
        report = e.evaluate("A -> B").consistency
        assert report.pressure_balanced
        assert not any("PRESSURE_MISMATCH" in w for w in report.warnings)

    def test_solvation_mismatch_flagged(self):
        e = self._make_engine(mol1_meta={"solvation": "CPCM"}, mol2_meta={"solvation": "SMD"})
        report = e.evaluate("A -> B").consistency
        assert any("SOLVATION_MISMATCH" in w for w in report.warnings)

    def test_dispersion_mismatch_flagged(self):
        e = self._make_engine(mol1_meta={"dispersion": "D3BJ"}, mol2_meta={"dispersion": "D4"})
        report = e.evaluate("A -> B").consistency
        assert any("DISPERSION_MISMATCH" in w for w in report.warnings)

    def test_method_mismatch_flagged(self):
        e = self._make_engine(mol1_meta={"method": "B3LYP"}, mol2_meta={"method": "PBE"})
        report = e.evaluate("A -> B").consistency
        assert any("METHOD_MISMATCH" in w for w in report.warnings)

    def test_temperature_source_not_reported(self):
        e = self._make_engine(mol1_meta={"temperature_k": None}, mol2_meta={"temperature_k": None})
        res = e.evaluate("A -> B")
        assert res.temperature_source == "NOT_REPORTED"
        assert res.temperature_k is None

    def test_pressure_source_not_reported(self):
        e = self._make_engine(mol1_meta={"pressure_atm": None}, mol2_meta={"pressure_atm": None})
        res = e.evaluate("A -> B")
        assert res.pressure_source == "NOT_REPORTED"
        
    def test_temperature_source_orca_output(self):
        e = self._make_engine(mol1_meta={"temperature_k": 298.15}, mol2_meta={"temperature_k": 298.15})
        res = e.evaluate("A -> B")
        assert res.temperature_source == "ORCA_OUTPUT"
        assert res.temperature_k == 298.15

    def test_equilibrium_constant_overflow(self):
        e = self._make_engine(mol1_meta={"temperature_k": 298.15}, mol2_meta={"temperature_k": 298.15})
        e._molecules["a"].jobs[0].gibbs_free_energy_eh = -10.0
        e._molecules["b"].jobs[0].gibbs_free_energy_eh = -2000.0  # Massive negative delta G
        res = e.evaluate("A -> B")
        assert res.keq_status == "OVERFLOW"
        assert res.equilibrium_constant_keq == float("inf")

    def test_equilibrium_constant_underflow(self):
        e = self._make_engine(mol1_meta={"temperature_k": 298.15}, mol2_meta={"temperature_k": 298.15})
        e._molecules["a"].jobs[0].gibbs_free_energy_eh = -2000.0
        e._molecules["b"].jobs[0].gibbs_free_energy_eh = -10.0  # Massive positive delta G
        res = e.evaluate("A -> B")
        assert res.keq_status == "UNDERFLOW"
        assert res.equilibrium_constant_keq == 0.0

    def test_equilibrium_constant_missing_temperature(self):
        e = self._make_engine()
        e._molecules["a"].jobs[0].gibbs_free_energy_eh = -10.0
        e._molecules["b"].jobs[0].gibbs_free_energy_eh = -15.0
        res = e.evaluate("A -> B")
        assert res.keq_status == "NOT_REPORTED"
        assert res.equilibrium_constant_keq is None

    def test_delta_s_direct_vs_hg_consistency_passed(self):
        m1 = JobData()
        m1.metadata.temperature_k = 300.0
        m1.e_elec_eh = 1.0
        m1.total_enthalpy_eh = 1.0
        m1.gibbs_free_energy_eh = 0.9
        # Direct S: T*S = H - G = 0.1 Eh -> S = 0.1/300
        # Wait, the entropy term parsed by models is Final Entropy Term in Eh.
        # It's T*S. So we set entropy_term_eh to 0.1
        m1.entropy_term_eh = 0.1
        
        m2 = JobData()
        m2.metadata.temperature_k = 300.0
        m2.e_elec_eh = 1.0
        m2.total_enthalpy_eh = 1.0
        m2.gibbs_free_energy_eh = 0.9
        m2.entropy_term_eh = 0.1
        
        molecules = {"A": MoleculeData("A", jobs=[m1]), "B": MoleculeData("B", jobs=[m2])}
        e = ThermochemistryEngine(molecules)
        res = e.evaluate("A -> B")
        
        assert res.delta_s_discrepancy_cal_mol_k is not None
        assert res.delta_s_discrepancy_cal_mol_k < 0.15
        assert res.consistency.entropy_consistency_passed is True

    def test_delta_s_direct_vs_hg_consistency_failed(self):
        m1 = JobData()
        m1.metadata.temperature_k = 300.0
        m1.e_elec_eh = 1.0
        m1.total_enthalpy_eh = 1.0
        m1.gibbs_free_energy_eh = 0.9
        # Direct S perturbed
        m1.entropy_term_eh = 0.15
        
        m2 = JobData()
        m2.metadata.temperature_k = 300.0
        m2.e_elec_eh = 1.0
        m2.total_enthalpy_eh = 1.0
        m2.gibbs_free_energy_eh = 0.9
        m2.entropy_term_eh = 0.1
        
        molecules = {"A": MoleculeData("A", jobs=[m1]), "B": MoleculeData("B", jobs=[m2])}
        e = ThermochemistryEngine(molecules)
        res = e.evaluate("A -> B")
        
        assert res.delta_s_discrepancy_cal_mol_k is not None
        assert res.delta_s_discrepancy_cal_mol_k > 0.15
        assert res.consistency.entropy_consistency_passed is False
        assert any("THERMOCHEMISTRY_INCONSISTENCY" in w for w in res.consistency.warnings)

    def test_geometry_mismatch_flagged_in_composite(self):
        m1_opt = JobData()
        m1_opt.e_elec_eh = -100.0
        m1_opt.elements = ["O"]; m1_opt.coords = [(0.0,0.0,0.0)]
        m1_opt.compute_geometry_hash()
        
        m1_sp = JobData()
        m1_sp.e_elec_eh = -100.1
        m1_sp.elements = ["O"]; m1_sp.coords = [(1.0,0.0,0.0)]  # Different geometry
        m1_sp.compute_geometry_hash()
        
        # Opt has frequency (enthalpy), SP has better electronic
        m1_opt.total_enthalpy_eh = -99.9
        
        molecules = {"A": MoleculeData("A", jobs=[m1_opt, m1_sp])}
        e = ThermochemistryEngine(molecules)
        res = e.evaluate("A -> A")
        assert any("GEOMETRY_MISMATCH" in w for w in res.consistency.geometry_mismatches)
        assert any("mismatched geometries" in w for w in res.consistency.warnings)

    def test_pressure_corrected_keq_calculation_analytical(self):
        # A(g) -> 2 B(g), delta_n_gas = +1
        # At T = 298.15 K, P = 10 atm
        # delta_G(1 atm) = 2 kcal/mol
        # delta_G(10 atm) = 2 + (1) * R * T * ln(10) = 2 + (1.98720425864083e-3 * 298.15 * 2.302585092994046) = 2 + 1.364233 = 3.364233 kcal/mol
        # K_eq(1 atm) = exp(-2 / (R * T)) = exp(-2 / 0.5924849) = 0.034177
        # K_eq(10 atm) = exp(-3.364233 / 0.5924849) = 0.0034177 = K_eq(1 atm) / 10
        r_gas = PhysConst.GAS_CONSTANT_KCAL_MOL_K
        t_ref = 298.15
        
        j_a = JobData()
        j_a.metadata.temperature_k = t_ref
        j_a.metadata.pressure_atm = 1.0
        j_a.metadata.phase = "Gas"
        j_a.e_elec_eh = 0.0
        j_a.gibbs_free_energy_eh = 0.0
        
        j_b = JobData()
        j_b.metadata.temperature_k = t_ref
        j_b.metadata.pressure_atm = 1.0
        j_b.metadata.phase = "Gas"
        j_b.e_elec_eh = (1.0 / PhysConst.HARTREE_TO_KCAL)  # 2 * 1.0 = 2 kcal/mol product Gibbs
        j_b.gibbs_free_energy_eh = (1.0 / PhysConst.HARTREE_TO_KCAL)
        
        molecules = {
            "A": MoleculeData("A", jobs=[j_a]),
            "B": MoleculeData("B", jobs=[j_b]),
        }
        engine = ThermochemistryEngine(molecules)
        res = engine.evaluate("A -> 2 B", custom_pressure_atm=10.0)
        
        assert res.pressure_atm == 10.0
        assert res.pressure_correction_applied is True
        assert abs(res.delta_g_kcal_mol - 2.0) < 1e-4
        expected_delta_g_press = 2.0 + 1.0 * r_gas * t_ref * math.log(10.0)
        assert abs(res.delta_g_pressure_corrected_kcal_mol - expected_delta_g_press) < 1e-4
        
        expected_keq_std = math.exp(-2.0 / (r_gas * t_ref))
        expected_keq_press = math.exp(-expected_delta_g_press / (r_gas * t_ref))
        assert abs(res.equilibrium_constant_keq - expected_keq_std) < 1e-5
        assert abs(res.keq_standard - expected_keq_std) < 1e-5
        assert abs(res.keq_pressure_corrected - expected_keq_press) < 1e-5
        assert abs(res.keq_pressure_corrected - (expected_keq_std / 10.0)) < 1e-5

    def test_pressure_correction_delta_n_zero(self):
        # A(g) -> B(g), delta_n_gas = 0 -> Pressure change has zero effect on Gibbs or K_eq
        j_a = JobData()
        j_a.metadata.temperature_k = 298.15
        j_a.metadata.phase = "Gas"
        j_a.e_elec_eh = 0.0
        j_a.gibbs_free_energy_eh = 0.0
        
        j_b = JobData()
        j_b.metadata.temperature_k = 298.15
        j_b.metadata.phase = "Gas"
        j_b.e_elec_eh = 1.0 / PhysConst.HARTREE_TO_KCAL
        j_b.gibbs_free_energy_eh = 1.0 / PhysConst.HARTREE_TO_KCAL
        
        engine = ThermochemistryEngine({"A": MoleculeData("A", jobs=[j_a]), "B": MoleculeData("B", jobs=[j_b])})
        res = engine.evaluate("A -> B", custom_pressure_atm=5.0)
        
        assert res.pressure_correction_applied is False
        assert res.delta_g_pressure_corrected_kcal_mol == res.delta_g_kcal_mol
        assert res.keq_pressure_corrected == res.equilibrium_constant_keq

    def test_temperature_extrapolation_van_t_hoff_analytical(self):
        # Calculation at T0 = 298.15 K:
        # Delta H = -10 kcal/mol, Delta S = -20 cal/(mol*K) = -0.02 kcal/(mol*K)
        # At T_req = 373.15 K:
        # Delta G(373.15) = -10 - 373.15 * (-0.02) = -10 + 7.463 = -2.537 kcal/mol
        # K_eq(373.15) = exp(-(-2.537) / (1.98720425864083e-3 * 373.15)) = exp(2.537 / 0.741525) = exp(3.42133) = 30.610
        t0 = 298.15
        r_gas = PhysConst.GAS_CONSTANT_KCAL_MOL_K
        
        j_a = JobData()
        j_a.metadata.temperature_k = t0
        j_a.e_elec_eh = 0.0
        j_a.total_enthalpy_eh = 0.0
        j_a.gibbs_free_energy_eh = 0.0
        j_a.entropy_term_eh = 0.0
        
        # Product B has Delta H = -10 kcal/mol, Delta S = -20 cal/(mol*K) -> TS = 298.15 * (-0.02) = -5.963 kcal/mol
        # G = H - TS = -10 - (-5.963) = -4.037 kcal/mol
        delta_h_eh = -10.0 / PhysConst.HARTREE_TO_KCAL
        ts_eh = (-20.0 * t0 / 1000.0) / PhysConst.HARTREE_TO_KCAL
        gibbs_eh = delta_h_eh - ts_eh
        
        j_b = JobData()
        j_b.metadata.temperature_k = t0
        j_b.e_elec_eh = delta_h_eh
        j_b.total_enthalpy_eh = delta_h_eh
        j_b.gibbs_free_energy_eh = gibbs_eh
        j_b.entropy_term_eh = ts_eh
        
        engine = ThermochemistryEngine({"A": MoleculeData("A", jobs=[j_a]), "B": MoleculeData("B", jobs=[j_b])})
        t_req = 373.15
        res = engine.evaluate("A -> B", custom_temperature_k=t_req)
        
        assert res.temperature_treatment_mode == "VAN_T_HOFF_DELTA_CP_ZERO"
        assert res.temperature_requested_k == t_req
        expected_g_req = -10.0 - t_req * (-20.0 / 1000.0)
        assert abs(res.delta_g_requested_kcal_mol - expected_g_req) < 1e-4
        expected_keq_req = math.exp(-expected_g_req / (r_gas * t_req))
        assert abs(res.keq_requested - expected_keq_req) < 1e-3

    def test_fractional_stoichiometry_golden(self):
        # 1 H2 + 0.5 O2 -> 1 H2O (elements: H:2, O:1)
        j_h2 = JobData()
        j_h2.elements = ["H", "H"]
        j_h2.coords = [(0,0,0), (0,0,0.74)]
        
        j_o2 = JobData()
        j_o2.elements = ["O", "O"]
        j_o2.coords = [(0,0,0), (0,0,1.21)]
        
        j_h2o = JobData()
        j_h2o.elements = ["H", "H", "O"]
        j_h2o.coords = [(0,0,0), (0.75,0.58,0), (-0.75,0.58,0)]
        
        molecules = {
            "H2": MoleculeData("H2", jobs=[j_h2]),
            "O2": MoleculeData("O2", jobs=[j_o2]),
            "H2O": MoleculeData("H2O", jobs=[j_h2o]),
        }
        engine = ThermochemistryEngine(molecules)
        res_balanced = engine.evaluate("H2 + 0.5 O2 -> H2O")
        assert res_balanced.consistency.atom_balanced is True
        assert res_balanced.consistency.element_imbalances == {}
        
        # Imbalanced fractional stoichiometry: H2 + 0.4 O2 -> H2O (O has +0.2 imbalance)
        res_imbalanced = engine.evaluate("H2 + 0.4 O2 -> H2O")
        assert res_imbalanced.consistency.atom_balanced is False
        assert "O" in res_imbalanced.consistency.element_imbalances
        assert abs(res_imbalanced.consistency.element_imbalances["O"] - 0.2) < 1e-5

    def test_consistency_report_stationary_point_warnings(self):
        # Test that higher order saddle or failed calculation triggers warning in ConsistencyReport
        j_saddle = JobData()
        j_saddle.stationary_point_status = "HIGHER_ORDER_SADDLE"
        j_saddle.imaginary_frequencies_count = 2
        j_saddle.e_elec_eh = -1.0
        
        j_ok = JobData()
        j_ok.stationary_point_status = "LIKELY_MINIMUM"
        j_ok.e_elec_eh = -2.0
        
        molecules = {
            "A": MoleculeData("A", jobs=[j_saddle]),
            "B": MoleculeData("B", jobs=[j_ok]),
        }
        engine = ThermochemistryEngine(molecules)
        res = engine.evaluate("A -> B")
        assert any("HIGHER_ORDER_SADDLE" in w for w in res.consistency.stationary_point_warnings)
        assert any("HIGHER_ORDER_SADDLE" in w for w in res.consistency.warnings)
