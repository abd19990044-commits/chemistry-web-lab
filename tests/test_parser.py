"""Parser regression tests.

Every test in this module pins behaviour that was observed to be wrong in the
pre-1.0 parser. The docstrings name the defect so that a future refactor cannot
reintroduce it without an explicit, deliberate change to a test.
"""

from __future__ import annotations

import pytest

from orca_engine.models import CoordinateUnit, MoleculeData, SpinChannel
from orca_engine.parser import OrcaParser
from orca_engine.regex import RegexLibrary
from tests.conftest import parse_fixture


class TestFrontierOrbitals:
    """Frontier orbital extraction across multi-step geometry optimizations."""

    def test_homo_and_lumo_come_from_the_same_optimization_step(self) -> None:
        """Regression: LUMO was retained from the first step, HOMO from the last.

        The fixture prints two ``ORBITAL ENERGIES`` blocks. The first has
        HOMO/LUMO = -5.5572/-1.4088 eV, the second -5.6987/-1.2146 eV. The
        pre-1.0 parser paired -5.6987 with -1.4088, inflating the gap by
        0.194 eV.
        """

        job = parse_fixture("opt_two_orbital_blocks.out")[-1]

        assert job.homo_ev == pytest.approx(-5.6987)
        assert job.lumo_ev == pytest.approx(-1.2146)
        assert job.homo_lumo_gap_ev == pytest.approx(4.4841)

    def test_unrestricted_channels_are_kept_separate(self) -> None:
        """Alpha and beta windows must not overwrite one another."""

        job = parse_fixture("unrestricted.out")[-1]

        assert job.alpha_homo_ev == pytest.approx(-5.0)
        assert job.alpha_lumo_ev == pytest.approx(-3.0)
        assert job.beta_homo_ev == pytest.approx(-8.4)
        assert job.beta_lumo_ev == pytest.approx(-4.0)

    def test_unrestricted_frontier_levels_span_both_channels(self) -> None:
        """HOMO is the highest occupied and LUMO the lowest virtual overall."""

        job = parse_fixture("unrestricted.out")[-1]

        assert job.homo_ev == pytest.approx(-5.0)
        assert job.lumo_ev == pytest.approx(-4.0)

    def test_a_spin_channel_with_no_electrons_still_yields_a_lumo(self) -> None:
        """Regression: an empty beta channel pushed the LUMO up by 7.5 eV.

        For a one-electron doublet the beta channel contains no occupied
        orbital. A guard requiring a HOMO before assigning a LUMO left that
        channel empty, so the reported LUMO fell back to the lowest *alpha*
        virtual at +6.07 eV instead of the beta level at -1.43 eV -- the true
        lowest unoccupied orbital of the system. The resulting gap was
        13.66 eV against a correct 6.16 eV.
        """

        job = parse_fixture("open_shell_empty_beta.out")[-1]

        assert job.beta_homo_ev is None
        assert job.beta_lumo_ev == pytest.approx(-1.4323)
        assert job.homo_ev == pytest.approx(-7.5931)
        assert job.lumo_ev == pytest.approx(-1.4323)
        assert job.homo_lumo_gap_ev == pytest.approx(6.1608)

    def test_a_truncated_final_block_does_not_inherit_earlier_values(self) -> None:
        """Regression: frontier values survived from a discarded geometry.

        The fixture optimizes one step, then starts a second ``ORBITAL
        ENERGIES`` section and crashes before printing any rows. Reporting the
        first step's HOMO/LUMO would present levels from an abandoned geometry
        as if they were converged results; the correct answer is that no
        frontier data is available.
        """

        job = parse_fixture("truncated_final_orbitals.out")[-1]

        assert job.homo_ev is None
        assert job.lumo_ev is None
        assert job.homo_lumo_gap_ev is None
        assert job.had_error_termination is True

    def test_a_further_occupied_orbital_invalidates_a_stale_lumo(self) -> None:
        """An out-of-order table must not leave a LUMO below the HOMO."""

        window = OrcaParser(iter(())).job.orbital_window(SpinChannel.RESTRICTED)
        window.update_from_occupation(2.0, -5.0)
        window.update_from_occupation(0.0, -1.0)
        window.update_from_occupation(2.0, -4.0)

        assert window.homo_ev == pytest.approx(-4.0)
        assert window.lumo_ev is None


class TestCoordinates:
    """Cartesian coordinate extraction and unit handling."""

    def test_angstrom_block_is_preferred_over_atomic_units(self) -> None:
        """Regression: the trailing A.U. block could overwrite Angstrom values.

        ORCA prints every geometry twice. The Bohr copy must never be reported
        as Angstrom, which would scale all coordinates by 1.889726.
        """

        job = parse_fixture("opt_two_orbital_blocks.out")[-1]

        assert job.coords_unit is CoordinateUnit.ANGSTROM
        assert job.coords[0] == pytest.approx((1.1, 0.0, 0.0))

    def test_final_geometry_replaces_earlier_steps(self) -> None:
        """Only the last optimization step is retained."""

        job = parse_fixture("opt_two_orbital_blocks.out")[-1]

        assert job.elements == ["C", "H"]
        assert job.coords[1] == pytest.approx((2.1, 0.0, 0.0))

    def test_stoichiometry_counts_elements(self) -> None:
        """Element counts back the reaction atom-balance check."""

        job = parse_fixture("rxn_h2o.out")[-1]

        assert dict(job.stoichiometry()) == {"O": 1, "H": 2}

    def test_a_bohr_only_geometry_is_labelled_bohr(self) -> None:
        """Regression: the unit was assumed rather than recorded.

        A counterpoise or property job may print only the atomic-unit block.
        Reporting those coordinates without a unit, or as Angstrom, scales the
        structure by 1.8897.
        """

        job = parse_fixture("coords_bohr_only.out")[-1]

        assert job.coords_unit is CoordinateUnit.BOHR
        assert job.coords[0] == pytest.approx((1.889726, 0.0, 0.0))

    def test_ghost_centres_do_not_truncate_the_geometry(self) -> None:
        """Regression: a counterpoise ghost centre ended the coordinate block.

        ORCA writes ghost centres as ``H:``. That token failed the element
        pattern, which terminated the whole section, so every real atom after
        the first ghost was silently dropped and the atom-balance check ran on
        a truncated structure.
        """

        job = parse_fixture("ghost_atoms.out")[-1]

        assert len(job.elements) == 5
        assert job.elements[2] == "H:"

    def test_ghost_centres_are_excluded_from_stoichiometry(self) -> None:
        """Ghost centres carry no electrons and are not atoms."""

        job = parse_fixture("ghost_atoms.out")[-1]

        assert dict(job.stoichiometry()) == {"O": 2, "H": 2}
        assert job.ghost_atom_count == 1

    def test_a_section_header_touching_a_table_is_not_swallowed(self) -> None:
        """Regression: the line that ended a section was consumed, not re-read.

        When ``ORBITAL ENERGIES`` follows a coordinate row with no blank or
        dashed line between them, the header is the line that terminates the
        coordinate state. Consuming it loses the entire orbital table.
        """

        job = parse_fixture("tight_section_boundary.out")[-1]

        assert job.elements == ["C", "H"]
        assert job.homo_ev == pytest.approx(-10.0)
        assert job.lumo_ev == pytest.approx(-1.4088)


class TestThermochemistry:
    """Scalar thermochemical observables."""

    def test_entropy_term_and_correction_are_separate_fields(self) -> None:
        """Regression: a single ``entropy_value`` conflated ``+T*S`` and ``-T*S``.

        Whichever label appeared last in the file won, so the sign of the
        stored quantity depended on ORCA's print order.
        """

        job = parse_fixture("thermo.out")[-1]

        assert job.entropy_term_eh == pytest.approx(0.02)
        assert job.entropy_correction_eh == pytest.approx(-0.02)

    def test_prose_mentioning_entropy_is_not_captured_as_a_value(self) -> None:
        """Regression: the loose ``Entropy`` pattern matched explanatory text.

        The fixture places "out the resulting rotational entropy values for
        sn=1,12:" *after* the labelled entropy line, followed by the sn table.
        A pattern matching the bare word "entropy" plus any number, combined
        with last-match-wins, captured 1.0 from the prose.
        """

        job = parse_fixture("entropy_prose_after.out")[-1]

        assert job.entropy_term_eh == pytest.approx(0.02)

    def test_gibbs_enthalpy_and_entropy_are_internally_consistent(self) -> None:
        """``H - G`` must equal the reported ``T*S`` term."""

        job = parse_fixture("thermo.out")[-1]
        assert job.total_enthalpy_eh is not None
        assert job.gibbs_free_energy_eh is not None

        assert job.total_enthalpy_eh - job.gibbs_free_energy_eh == pytest.approx(
            job.entropy_term_eh, abs=1e-8
        )

    def test_temperature_and_pressure_are_captured(self) -> None:
        """Gibbs energies are only comparable at a stated temperature."""

        job = parse_fixture("thermo.out")[-1]

        assert job.metadata.temperature_k == pytest.approx(298.15)
        assert job.metadata.pressure_atm == pytest.approx(1.0)

    def test_electronic_plus_zpe_is_summed(self) -> None:
        """``E0 = E_elec + ZPE`` is only returned when both parts exist."""

        job = parse_fixture("thermo.out")[-1]

        assert job.electronic_zpe_eh() == pytest.approx(-76.379)


class TestMetadata:
    """Calculation metadata used for comparability checks."""

    def test_charge_and_multiplicity_are_captured(self) -> None:
        """Charge balance cannot be checked without the molecular charge."""

        job = parse_fixture("unrestricted.out")[-1]

        assert job.metadata.charge == 0
        assert job.metadata.multiplicity == 2

    def test_basis_version_and_solvation_are_captured(self) -> None:
        """These three fields define the comparability key for a species."""

        job = parse_fixture("thermo.out")[-1]

        assert job.metadata.level_of_theory() == ("6.1.0", "def2-TZVP", "SMD")


class TestTermination:
    """Termination status detection."""

    def test_earlier_error_terminations_are_retained(self) -> None:
        """Regression: a successful rerun masked an earlier failure.

        A file that was appended to across reruns finishes with a normal
        banner, but the failed attempts must remain visible.
        """

        job = parse_fixture("rerun_after_error.out")[-1]

        assert job.terminated_normally is True
        assert job.had_error_termination is True
        assert job.error_messages

    def test_clean_runs_report_no_errors(self) -> None:
        """A file with no error banner must not be flagged."""

        job = parse_fixture("thermo.out")[-1]

        assert job.terminated_normally is True
        assert job.had_error_termination is False

    def test_a_diagnostic_error_line_is_not_a_termination(self) -> None:
        """Regression: a catch-all ``Error:`` pattern flagged successful runs.

        ORCA prints lines beginning "Error:" during healthy calculations. The
        fixture contains one, and must still be reported as clean.
        """

        job = parse_fixture("entropy_prose_after.out")[-1]

        assert job.terminated_normally is True
        assert job.had_error_termination is False

    def test_a_failure_in_an_earlier_job_block_is_not_hidden(self) -> None:
        """Regression: per-job flags let a compound job mask a failure.

        A ``$new_job`` file whose first block fails and whose second succeeds
        leaves ``jobs[-1]`` clean. Anyone inspecting only the final block --
        including the library's own validation script, originally -- sees a
        healthy calculation. The species-level flag must aggregate.
        """

        jobs = parse_fixture("compound_job_first_fails.out")
        molecule = MoleculeData(name="compound", jobs=jobs)

        assert len(jobs) == 2
        assert jobs[-1].had_error_termination is False
        assert molecule.had_error_termination is True


class TestExcitedStates:
    """TDDFT absorption spectrum extraction."""

    @pytest.mark.parametrize(
        ("fixture", "first_cm", "first_fosc"),
        [
            ("tddft_orca5.out", 33134.0, 0.039830350),
            ("tddft_orca6.out", 18146.3, 0.550147942),
        ],
    )
    def test_both_orca_table_layouts_are_parsed(
        self, fixture: str, first_cm: float, first_fosc: float
    ) -> None:
        """ORCA 5 and ORCA 6 print different absorption-spectrum headers."""

        job = parse_fixture(fixture)[-1]

        assert len(job.tddft_cm) == 2
        assert job.tddft_cm[0] == pytest.approx(first_cm)
        assert job.tddft_fosc[0] == pytest.approx(first_fosc)

    def test_transition_labels_are_not_read_as_data(self) -> None:
        """The ORCA 6 ``0-1A -> 1-1A`` column must not shift the energy fields."""

        job = parse_fixture("tddft_orca6.out")[-1]

        assert job.tddft_cm == pytest.approx([18146.3, 21792.0])

    def test_the_soc_header_does_not_match_the_plain_spectrum_pattern(self) -> None:
        """ORCA's SOC header contains the plain header as a substring.

        "SOC CORRECTED ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE
        MOMENTS" would be matched by a naive substring pattern, which is what
        the negative lookbehind exists to prevent.
        """

        soc = "  SOC CORRECTED ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS"
        plain = "     ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS"

        assert RegexLibrary.TDDFT_SECTION.search(plain) is not None
        assert RegexLibrary.TDDFT_SECTION.search(soc) is None
        assert RegexLibrary.TDDFT_SECTION_SOC.search(soc) is not None

    def test_soc_corrected_states_are_not_appended_to_the_plain_spectrum(self) -> None:
        """Two physically different spectra must not land in one array.

        The fixture places the SOC table's data rows directly beneath its
        header, with no column sub-headers in between. In real ORCA output
        those sub-headers happen to eject the parser from the excited-state
        state first, so the plain pattern is *incidentally* safe -- the same
        kind of accidental correctness that made the coordinate-unit handling
        fragile. The fixture removes that accident so the guard is tested on
        its own merits.
        """

        job = parse_fixture("tddft_soc.out")[-1]

        assert job.tddft_cm == pytest.approx([18146.3, 21792.0])
        assert 15324.1 not in job.tddft_cm


class TestStreamContract:
    """The parser must not materialize its input."""

    def test_parsing_consumes_an_arbitrary_iterator(self) -> None:
        """Any line iterator is a valid input, not only file handles."""

        lines = iter(
            [
                "Program Version 6.1.0",
                "FINAL SINGLE POINT ENERGY      -1.234567890000",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        jobs = OrcaParser(lines).parse()

        assert len(jobs) == 1
        assert jobs[0].e_elec_eh == pytest.approx(-1.23456789)

    def test_empty_stream_yields_no_jobs(self) -> None:
        """An empty input is not an error."""

        assert OrcaParser(iter(())).parse() == []


class TestElectronicProperties:
    """Dipole moment, spin expectation values, and spin contamination."""

    def test_dipole_moment_is_captured(self) -> None:
        """Total dipole moment in Debye is extracted from ORCA dipole blocks."""

        lines = iter(
            [
                "-------------",
                "DIPOLE MOMENT",
                "-------------",
                "Total Dipole Moment    :     1.85420",
                "Magnitude (Debye)      :     1.85420",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        job = OrcaParser(lines).parse()[-1]

        assert job.dipole_moment_debye == pytest.approx(1.85420)

    def test_spin_s2_and_contamination_are_calculated(self) -> None:
        """Unrestricted <S**2> and spin contamination are computed."""

        lines = iter(
            [
                "<S**2>                  :     0.7582",
                "Ideal <S**2>            :     0.7500",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        job = OrcaParser(lines).parse()[-1]

        assert job.s2_actual == pytest.approx(0.7582)
        assert job.s2_ideal == pytest.approx(0.7500)
        assert job.spin_contamination == pytest.approx(0.0082)


class TestVibrationalAnalysis:
    """Vibrational frequencies and transition state identification."""

    def test_minimum_has_zero_imaginary_frequencies(self) -> None:
        """A ground state local minimum has no imaginary modes."""

        lines = iter(
            [
                "-----------------------",
                "VIBRATIONAL FREQUENCIES",
                "-----------------------",
                "   0:         0.00 cm**-1",
                "   6:      1542.30 cm**-1",
                "   7:      3650.10 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        job = OrcaParser(lines).parse()[-1]

        assert job.imaginary_frequencies_count == 0
        assert job.is_transition_state is False

    def test_transition_state_has_one_imaginary_frequency(self) -> None:
        """A transition state has exactly one imaginary mode."""

        lines = iter(
            [
                "-----------------------",
                "VIBRATIONAL FREQUENCIES",
                "-----------------------",
                "   0:         0.00 cm**-1",
                "   6:      -450.20 cm**-1 ***imaginary mode***",
                "   7:       820.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        job = OrcaParser(lines).parse()[-1]

        assert job.imaginary_frequencies_count == 1
        assert job.imaginary_frequencies_cm == pytest.approx([-450.20])
        assert job.is_transition_state is True

    def test_higher_order_saddle_point_is_not_transition_state(self) -> None:
        """Two imaginary modes indicate a higher-order saddle point, not a TS."""

        lines = iter(
            [
                "VIBRATIONAL FREQUENCIES",
                "   6:      -510.00 cm**-1 ***imaginary mode***",
                "   7:      -220.00 cm**-1 ***imaginary mode***",
                "   8:       600.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        job = OrcaParser(lines).parse()[-1]

        assert job.imaginary_frequencies_count == 2
        assert job.is_transition_state is False


class TestConceptualDFT:
    """Conceptual DFT reactivity descriptors (Parr, Pearson, Gázquez)."""

    def test_cdft_reactivity_descriptors(self) -> None:
        """Electronegativity, hardness, electrophilicity, and donor/acceptor powers."""

        lines = iter(
            [
                "ORBITAL ENERGIES",
                "  0   2.0000   -0.2205   -6.0000",
                "  1   0.0000   -0.0367   -1.0000",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        job = OrcaParser(lines).parse()[-1]

        assert job.homo_ev == pytest.approx(-6.0)
        assert job.lumo_ev == pytest.approx(-1.0)
        assert job.homo_lumo_gap_ev == pytest.approx(5.0)
        assert job.ionization_potential_ev == pytest.approx(6.0)
        assert job.electron_affinity_ev == pytest.approx(1.0)
        assert job.chemical_hardness_ev == pytest.approx(2.5)
        assert job.chemical_potential_ev == pytest.approx(-3.5)
        assert job.electronegativity_ev == pytest.approx(3.5)
        assert job.chemical_softness_ev == pytest.approx(0.2)
        assert job.electrophilicity_index_ev == pytest.approx(2.45)
        assert job.electrodonating_power_ev == pytest.approx(4.5125)
        assert job.electroaccepting_power_ev == pytest.approx(1.0125)
        assert job.net_electrophilicity_ev == pytest.approx(5.525)

