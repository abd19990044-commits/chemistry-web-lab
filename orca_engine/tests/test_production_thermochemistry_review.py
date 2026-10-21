"""Analytical regressions for pressure references and complete entropy data."""
import math

import pytest

from orca_engine.constants import PhysConst
from orca_engine.models import EnergyKind, JobData, MoleculeData
from orca_engine.parser import OrcaParser
from orca_engine.thermochemistry import ReactionParseError, ThermochemistryEngine


def _engine(pressure=10.0):
    a = JobData(e_elec_eh=0.0, gibbs_free_energy_eh=0.0)
    b = JobData(e_elec_eh=1.0 / PhysConst.HARTREE_TO_KCAL,
                gibbs_free_energy_eh=1.0 / PhysConst.HARTREE_TO_KCAL)
    for job in (a, b):
        job.metadata.temperature_k = 298.15
        job.metadata.pressure_atm = pressure
    return ThermochemistryEngine({'a': MoleculeData('a', jobs=[a]), 'b': MoleculeData('b', jobs=[b])})


def test_reported_pressure_is_not_applied_twice():
    result = _engine().evaluate('a -> 2 b')
    assert result.pressure_atm == 10.0
    assert result.standard_state == 'Gas Phase (10 atm)'
    assert result.delta_g_pressure_corrected_kcal_mol == pytest.approx(2.0)
    assert result.pressure_correction_applied is False


@pytest.mark.parametrize('target', [1.0, 5.0, 10.0, 20.0])
def test_pressure_shift_uses_orca_reference_including_target_one_atm(target):
    result = _engine().evaluate('a -> 2 b', custom_pressure_atm=target)
    expected = 2.0 + PhysConst.GAS_CONSTANT_KCAL_MOL_K * 298.15 * math.log(target / 10.0)
    assert result.delta_g_pressure_corrected_kcal_mol == pytest.approx(expected)
    assert result.keq_pressure_corrected == pytest.approx(math.exp(-expected / (PhysConst.GAS_CONSTANT_KCAL_MOL_K * 298.15)))


def test_missing_source_pressure_does_not_assume_one_atm():
    result = _engine(pressure=None).evaluate('a -> 2 b', custom_pressure_atm=2.0)
    assert result.delta_g_pressure_corrected_kcal_mol is None
    assert result.keq_pressure_status == 'NOT_REPORTED'
    assert result.keq_pressure_reason == 'PRESSURE_REFERENCE_MISSING'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 0])
def test_requested_thermodynamic_conditions_must_be_finite_positive(value):
    for keyword in ('custom_pressure_atm', 'custom_temperature_k'):
        with pytest.raises(ValueError):
            _engine().evaluate('a -> b', **{keyword: value})


def test_overflowing_stoichiometric_coefficient_is_rejected():
    with pytest.raises(ReactionParseError):
        ThermochemistryEngine.parse_reaction('1e999 a -> b')


def test_electronic_entropy_is_parsed_and_all_components_are_required():
    text = '''S_vib ... 10.0 cal/mol-K
S_rot ... 20.0 cal/mol-K
S_trans ... 30.0 cal/mol-K
S_elec ... 1.5 cal/mol-K
FINAL SINGLE POINT ENERGY -1.0
Final Gibbs free energy ... -1.01 Eh
'''
    job = OrcaParser(text).parse()[0]
    assert job.electronic_entropy_cal_mol_k == 1.5
    engine = ThermochemistryEngine({'a': MoleculeData('a', jobs=[job])})
    expected = 61.5 / (PhysConst.HARTREE_TO_KCAL * 1000.0)
    assert engine._get_best_energy('a', EnergyKind.ENTROPY) == pytest.approx(expected)
    job.electronic_entropy_cal_mol_k = None
    assert engine._get_best_energy('a', EnergyKind.ENTROPY) is None


def test_missing_species_temperature_does_not_borrow_from_other_species():
    engine = _engine()
    engine._molecules['b'].jobs[0].metadata.temperature_k = None
    result = engine.evaluate('a -> b')
    assert result.temperature_k is None
    assert result.temperature_source == 'NOT_REPORTED'
    assert result.equilibrium_constant_keq is None


def test_earlier_thermal_job_does_not_override_selected_conditions():
    engine = _engine()
    for molecule in engine._molecules.values():
        earlier = JobData(e_elec_eh=0.0, gibbs_free_energy_eh=0.0)
        earlier.metadata.temperature_k = 350.0
        earlier.metadata.pressure_atm = 3.0
        molecule.jobs.insert(0, earlier)
    result = engine.evaluate('a -> b')
    assert result.temperature_k == 298.15
    assert result.consistency.mixed_pressures_atm == [10.0]
    assert result.consistency.mixed_temperatures_k == [298.15]


def test_explicit_thermochemistry_header_is_a_reported_temperature():
    job = OrcaParser("THERMOCHEMISTRY AT 298.15 K\nFINAL SINGLE POINT ENERGY -1\n").parse()[0]
    assert job.metadata.temperature_k == 298.15
    assert job.metadata.temperature_source == "ORCA_OUTPUT"
