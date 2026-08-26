"""Reaction parsing, thermochemistry, and physical consistency tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from orca_engine.constants import PhysConst
from orca_engine.io import load_directory_parallel
from orca_engine.models import EnergyKind, MoleculeData
from orca_engine.thermochemistry import ReactionParseError, ThermochemistryEngine


@pytest.fixture
def engine(reaction_dir: Path) -> ThermochemistryEngine:
    """Return an engine loaded with the balanced H2 + O -> H2O fixtures."""

    return ThermochemistryEngine(load_directory_parallel(reaction_dir, workers=1))


class TestEquationParsing:
    """Syntax handling for reaction equations."""

    @pytest.mark.parametrize("arrow", ["->", "=>", "-->", "=", "<=>", "<->", "<==>"])
    def test_common_arrow_spellings_are_accepted(self, arrow: str) -> None:
        """Chemists write the arrow in several ways, including equilibrium arrows."""

        reaction = ThermochemistryEngine.parse_reaction(f"a {arrow} b")

        assert [term.molecule_name for term in reaction.reactants] == ["a"]
        assert [term.molecule_name for term in reaction.products] == ["b"]

    def test_coefficients_default_to_one(self) -> None:
        """A bare species name carries an implicit coefficient of 1."""

        reaction = ThermochemistryEngine.parse_reaction("a + 2b -> 3c")

        assert [term.coefficient for term in reaction.reactants] == [1.0, 2.0]
        assert [term.coefficient for term in reaction.products] == [3.0]

    def test_explicit_multiplication_is_accepted(self) -> None:
        """``2*b`` removes the ambiguity of ``2b`` at the source."""

        reaction = ThermochemistryEngine.parse_reaction("2*b -> c")

        assert reaction.reactants[0].coefficient == 2.0
        assert reaction.reactants[0].molecule_name == "b"

    def test_iupac_names_starting_with_numbers_are_parsed(self) -> None:
        """Molecules with positional numbers (e.g. 1-butanol, 2-naphthol) are parsed."""

        reaction = ThermochemistryEngine.parse_reaction("1-butanol + 2-naphthol -> 3-pentanone")

        assert [term.molecule_name for term in reaction.reactants] == ["1-butanol", "2-naphthol"]
        assert [term.coefficient for term in reaction.reactants] == [1.0, 1.0]
        assert reaction.products[0].coefficient == 1.0
        assert reaction.products[0].molecule_name == "3-pentanone"

        reaction_coeff = ThermochemistryEngine.parse_reaction("2 1-butanol -> 1 3-pentanone")
        assert reaction_coeff.reactants[0].coefficient == 2.0
        assert reaction_coeff.reactants[0].molecule_name == "1-butanol"
        assert reaction_coeff.products[0].coefficient == 1.0
        assert reaction_coeff.products[0].molecule_name == "3-pentanone"

        reaction_star = ThermochemistryEngine.parse_reaction("2 * 1-butanol -> 1-product")
        assert reaction_star.reactants[0].coefficient == 2.0
        assert reaction_star.reactants[0].molecule_name == "1-butanol"

    @pytest.mark.parametrize("equation", ["a", "a -> ", " -> b", "a -> -> b", "a + -> b"])
    def test_malformed_equations_are_rejected(self, equation: str) -> None:
        """Malformed input must raise rather than silently produce a number."""

        with pytest.raises(ReactionParseError):
            ThermochemistryEngine.parse_reaction(equation)

    def test_non_positive_coefficients_are_rejected(self) -> None:
        """A negative coefficient signals a mis-typed equation."""

        with pytest.raises(ReactionParseError):
            ThermochemistryEngine.parse_reaction("-1a -> b")

    def test_species_named_like_a_coefficient_are_resolved(self) -> None:
        """Regression: a file named ``2b.out`` was unreachable.

        ``2b`` parses syntactically as "two of species b". When species ``2b``
        exists and species ``b`` does not, the token is a molecule name.
        """

        molecules = {"2b": MoleculeData(name="2b"), "c": MoleculeData(name="c")}
        resolved = ThermochemistryEngine(molecules).resolve_reaction("2b -> c")

        assert resolved.reactants[0].coefficient == 1.0
        assert resolved.reactants[0].molecule_name == "2b"

    def test_genuine_coefficients_are_left_alone(self) -> None:
        """Resolution must not fire when the split name does exist."""

        molecules = {"b": MoleculeData(name="b"), "c": MoleculeData(name="c")}
        resolved = ThermochemistryEngine(molecules).resolve_reaction("2b -> c")

        assert resolved.reactants[0].coefficient == 2.0
        assert resolved.reactants[0].molecule_name == "b"


class TestReactionEnergies:
    """Numerical reaction thermochemistry."""

    def test_delta_electronic_matches_a_hand_calculation(
        self, engine: ThermochemistryEngine
    ) -> None:
        """E(H2O) - E(H2) - E(O), converted to kcal/mol."""

        expected = (-76.40 - (-1.17 + -75.05)) * PhysConst.HARTREE_TO_KCAL
        result = engine.evaluate("rxn_h2 + rxn_o -> rxn_h2o")

        assert result.delta_electronic_kcal_mol == pytest.approx(expected)

    def test_delta_e0_includes_zero_point_energy(self, engine: ThermochemistryEngine) -> None:
        """E0 differs from the electronic energy by the ZPE sum."""

        expected = (
            (-76.40 + 0.021) - ((-1.17 + 0.01) + (-75.05 + 0.0))
        ) * PhysConst.HARTREE_TO_KCAL
        result = engine.evaluate("rxn_h2 + rxn_o -> rxn_h2o")

        assert result.delta_e0_kcal_mol == pytest.approx(expected)

    def test_gibbs_and_enthalpy_are_computed(self, engine: ThermochemistryEngine) -> None:
        """Delta G and Delta H use their own reference fields."""

        result = engine.evaluate("rxn_h2 + rxn_o -> rxn_h2o")

        assert result.delta_g_kcal_mol == pytest.approx(
            (-76.39 - (-1.16 + -75.045)) * PhysConst.HARTREE_TO_KCAL
        )
        assert result.delta_h_kcal_mol == pytest.approx(
            (-76.37 - (-1.15 + -75.04)) * PhysConst.HARTREE_TO_KCAL
        )
        assert result.is_exergonic is True
        assert result.delta_entropy_cal_mol_k is not None
        assert result.equilibrium_constant_keq is not None
        assert result.equilibrium_constant_keq > 1.0e10

    def test_stoichiometric_coefficients_are_applied(self, engine: ThermochemistryEngine) -> None:
        """Doubling a reaction doubles its energy."""

        single = engine.evaluate("rxn_h2 + rxn_o -> rxn_h2o")
        double = engine.evaluate("2 rxn_h2 + 2 rxn_o -> 2 rxn_h2o")
        assert single.delta_electronic_kcal_mol is not None
        assert double.delta_electronic_kcal_mol is not None

        assert double.delta_electronic_kcal_mol == pytest.approx(
            2 * single.delta_electronic_kcal_mol
        )

    def test_missing_species_reports_none_and_names_the_gap(
        self, engine: ThermochemistryEngine
    ) -> None:
        """An absent reference must never be silently treated as zero."""

        result = engine.evaluate("rxn_h2 -> does_not_exist")

        assert result.delta_electronic_kcal_mol is None
        assert any("does_not_exist" in item for item in result.missing_references)


class TestConsistencyChecks:
    """Physical validity checks applied alongside every reaction energy."""

    def test_a_balanced_reaction_passes_every_check(self, engine: ThermochemistryEngine) -> None:
        """H2 + O -> H2O is balanced, neutral, and single-level."""

        report = engine.evaluate("rxn_h2 + rxn_o -> rxn_h2o").consistency

        assert report.atom_balanced is True
        assert report.charge_balanced is True
        assert report.is_clean()

    def test_atom_imbalance_is_detected_and_quantified(self, engine: ThermochemistryEngine) -> None:
        """Regression: unbalanced equations produced a number with no warning."""

        report = engine.evaluate("rxn_h2 -> rxn_h2o").consistency

        assert report.atom_balanced is False
        assert report.atom_imbalance == {"O": 1}
        assert any("not atom balanced" in warning for warning in report.warnings)

    def test_coefficients_are_included_in_the_balance(self, engine: ThermochemistryEngine) -> None:
        """Balance is checked on weighted counts, not bare formulas."""

        report = engine.evaluate("2 rxn_h2 + rxn_o -> rxn_h2o").consistency

        assert report.atom_balanced is False
        assert report.atom_imbalance == {"H": -2}

    def test_energy_is_still_reported_alongside_warnings(
        self, engine: ThermochemistryEngine
    ) -> None:
        """Checks annotate the result; they do not suppress it."""

        result = engine.evaluate("rxn_h2 -> rxn_h2o")

        assert result.delta_electronic_kcal_mol is not None
        assert not result.consistency.is_clean()

    def test_mixed_levels_of_theory_are_flagged(self, tmp_path: Path, data_dir: Path) -> None:
        """Regression: species from different basis sets combined silently."""

        (tmp_path / "a.out").write_text(
            (data_dir / "rxn_h2.out").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (tmp_path / "b.out").write_text(
            (data_dir / "rxn_h2.out").read_text(encoding="utf-8").replace("def2-TZVP", "def2-SVP"),
            encoding="utf-8",
        )
        engine = ThermochemistryEngine(load_directory_parallel(tmp_path, workers=1))

        report = engine.evaluate("a -> b").consistency

        assert len(report.mixed_levels_of_theory) == 2
        assert any("different levels of theory" in item for item in report.warnings)

    def test_mixed_levels_within_one_species_are_flagged(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Regression: only the energy-supplying job's level was inspected.

        Different quantities are drawn from different jobs on purpose -- an
        electronic energy from one, a ZPE from another. Checking a single
        reference job certified reactions whose terms actually came from two
        different basis sets.
        """

        source = (data_dir / "rxn_h2.out").read_text(encoding="utf-8")
        freq_only = source.replace("def2-TZVP", "def2-SVP").replace(
            "FINAL SINGLE POINT ENERGY      -1.170000000000", ""
        )
        (tmp_path / "x.out").write_text(f"{source}\n$new_job\n{freq_only}", encoding="utf-8")
        (tmp_path / "y.out").write_text(source, encoding="utf-8")
        engine = ThermochemistryEngine(load_directory_parallel(tmp_path, workers=1))

        report = engine.evaluate("x -> y").consistency

        assert len(report.mixed_levels_of_theory) > 1
        assert not report.is_clean()

    def test_a_failed_calculation_does_not_pass_silently(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Regression: no check asked whether the calculation actually finished.

        A reaction energy taken from an error-terminated job reported
        ``is_clean() == True``, which is exactly the false reassurance that
        filtering on "terminated normally" is meant to prevent.
        """

        source = (data_dir / "rxn_h2.out").read_text(encoding="utf-8")
        broken = source.replace(
            "****ORCA TERMINATED NORMALLY****",
            "ORCA finished by error termination in SCF",
        )
        (tmp_path / "good.out").write_text(source, encoding="utf-8")
        (tmp_path / "bad.out").write_text(broken, encoding="utf-8")
        engine = ThermochemistryEngine(load_directory_parallel(tmp_path, workers=1))

        report = engine.evaluate("good -> bad").consistency

        assert report.species_with_errors == ["bad"]
        assert not report.is_clean()

    def test_temperature_disagreement_is_flagged(self, tmp_path: Path, data_dir: Path) -> None:
        """Gibbs energies from different temperatures are not comparable."""

        source = (data_dir / "rxn_h2.out").read_text(encoding="utf-8")
        (tmp_path / "a.out").write_text(source, encoding="utf-8")
        (tmp_path / "b.out").write_text(source.replace("298.15 K", "373.15 K"), encoding="utf-8")
        engine = ThermochemistryEngine(load_directory_parallel(tmp_path, workers=1))

        report = engine.evaluate("a -> b").consistency

        assert sorted(report.mixed_temperatures_k) == [298.15, 373.15]
        assert any("different temperatures" in warning for warning in report.warnings)

    def test_unknown_geometry_leaves_the_check_unevaluated(self) -> None:
        """A check that cannot run must report ``None``, not ``True``."""

        molecules = {"a": MoleculeData(name="a"), "b": MoleculeData(name="b")}
        report = ThermochemistryEngine(molecules).check_consistency(
            ThermochemistryEngine.parse_reaction("a -> b")
        )

        assert report.atom_balanced is None
        assert report.charge_balanced is None


class TestBondDissociationEnergies:
    """Bond dissociation energy helpers."""

    def test_bde_equals_the_dissociation_reaction_energy(
        self, engine: ThermochemistryEngine
    ) -> None:
        """A BDE is a dissociation reaction evaluated for one energy kind."""

        bde = engine.evaluate_bde_equation("rxn_h2o -> rxn_h2 + rxn_o")
        reaction = engine.evaluate("rxn_h2o -> rxn_h2 + rxn_o")

        assert bde.bde_kcal_mol == pytest.approx(reaction.delta_electronic_kcal_mol)

    def test_bde_from_fragments_matches_the_equation_form(
        self, engine: ThermochemistryEngine
    ) -> None:
        """Both entry points must agree."""

        from_parts = engine.evaluate_bde("rxn_h2o", ["rxn_h2", "rxn_o"])
        from_equation = engine.evaluate_bde_equation("rxn_h2o -> rxn_h2 + rxn_o")

        assert from_parts.bde_kcal_mol == pytest.approx(from_equation.bde_kcal_mol)

    def test_zero_point_corrected_bde_differs_from_the_electronic_one(
        self, engine: ThermochemistryEngine
    ) -> None:
        """D0 and De are distinct quantities and must not be conflated."""

        de = engine.evaluate_bde_equation("rxn_h2o -> rxn_h2 + rxn_o", EnergyKind.ELECTRONIC)
        d0 = engine.evaluate_bde_equation("rxn_h2o -> rxn_h2 + rxn_o", EnergyKind.ELECTRONIC_ZPE)
        assert de.bde_kcal_mol is not None
        assert d0.bde_kcal_mol is not None

        assert de.bde_kcal_mol != pytest.approx(d0.bde_kcal_mol)

    def test_multiple_parents_are_rejected(self, engine: ThermochemistryEngine) -> None:
        """A BDE is defined for exactly one parent species."""

        with pytest.raises(ReactionParseError):
            engine.evaluate_bde_equation("rxn_h2 + rxn_o -> rxn_h2o")
