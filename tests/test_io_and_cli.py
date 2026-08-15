"""File discovery, parallel loading, reporting, and CLI tests."""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import pytest

from orca_engine.cli import main
from orca_engine.io import (
    DuplicateSpeciesError,
    discover_orca_files,
    load_directory_parallel,
)
from orca_engine.reporting import build_report, write_report


class TestDiscovery:
    """Input discovery rules."""

    def test_uppercase_suffixes_are_discovered(self, data_dir: Path) -> None:
        """Regression: ``.OUT`` was invisible on case-sensitive filesystems.

        A case-sensitive glob returned different file sets on Linux and on
        Windows, so the same directory produced different results per platform.
        """

        names = {path.name for path in discover_orca_files(data_dir)}

        assert "UPPERCASE_SUFFIX.OUT" in names

    def test_each_file_is_discovered_once(self, data_dir: Path) -> None:
        """Case-insensitive matching must not double-count on Windows."""

        files = discover_orca_files(data_dir)

        assert len(files) == len(set(files))

    def test_a_missing_path_raises(self, tmp_path: Path) -> None:
        """A typo in the input path is an error, not an empty result."""

        with pytest.raises(FileNotFoundError):
            discover_orca_files(tmp_path / "nope")

    def test_an_unsupported_suffix_raises(self, tmp_path: Path) -> None:
        """Only ORCA outputs and archives are accepted as explicit inputs."""

        target = tmp_path / "notes.txt"
        target.write_text("hello", encoding="utf-8")

        with pytest.raises(ValueError):
            discover_orca_files(target)


class TestLoading:
    """Parallel loading and molecule merging."""

    def test_results_are_independent_of_worker_count(self, reaction_dir: Path) -> None:
        """Regression: completion order leaked into the merged result.

        The loader previously consumed futures with ``as_completed``, so the
        job order inside a molecule depended on scheduling.
        """

        serial = load_directory_parallel(reaction_dir, workers=1)
        parallel = load_directory_parallel(reaction_dir, workers=4, io_bound=True)

        assert list(serial) == list(parallel)
        assert [job.e_elec_eh for job in serial["rxn_h2o"].jobs] == [
            job.e_elec_eh for job in parallel["rxn_h2o"].jobs
        ]

    def test_repeated_runs_produce_identical_reports(self, reaction_dir: Path) -> None:
        """Byte-for-byte reproducibility is required for archived results."""

        first = build_report(load_directory_parallel(reaction_dir, workers=4, io_bound=True))
        second = build_report(load_directory_parallel(reaction_dir, workers=4, io_bound=True))

        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_colliding_stems_keep_a_deterministic_job_order(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        """Regression: completion order decided which calculation won.

        Job order within a merged species is only observable when stems
        collide, which is exactly the case the previous determinism tests did
        not construct. Results must follow sorted input path order, not the
        order in which workers happened to finish.
        """

        source = (data_dir / "rxn_h2.out").read_text(encoding="utf-8")
        for index, sub in enumerate(("a_run", "b_run", "c_run", "d_run", "e_run", "f_run")):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / "same.out").write_text(
                source.replace("-1.170000000000", f"-1.1{index}0000000000"), encoding="utf-8"
            )

        expected = [
            job.e_elec_eh for job in load_directory_parallel(tmp_path, workers=1)["same"].jobs
        ]
        for io_bound in (True, False):
            molecules = load_directory_parallel(tmp_path, workers=6, io_bound=io_bound)
            assert [job.e_elec_eh for job in molecules["same"].jobs] == expected

    def test_colliding_stems_can_be_made_fatal(self, tmp_path: Path, data_dir: Path) -> None:
        """Regression: two directories holding ``nap.out`` merged silently.

        The two files may be different species, or the same species at a
        different level of theory; either way the merge is unsafe.
        """

        source = (data_dir / "rxn_h2.out").read_text(encoding="utf-8")
        for sub in ("run_a", "run_b"):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / "same.out").write_text(source, encoding="utf-8")

        with pytest.raises(DuplicateSpeciesError):
            load_directory_parallel(tmp_path, workers=1, strict_duplicates=True)

    def test_colliding_stems_warn_by_default(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The default is permissive but never silent."""

        source = (data_dir / "rxn_h2.out").read_text(encoding="utf-8")
        for sub in ("run_a", "run_b"):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / "same.out").write_text(source, encoding="utf-8")

        with caplog.at_level("WARNING"):
            molecules = load_directory_parallel(tmp_path, workers=1)

        assert len(molecules["same"].sources) == 2
        assert any("maps to 2 sources" in record.message for record in caplog.records)

    def test_zip_archives_are_streamed(self, tmp_path: Path, data_dir: Path) -> None:
        """ZIP members are parsed without extraction to disk."""

        archive_path = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("runs/rxn_h2.out", (data_dir / "rxn_h2.out").read_text("utf-8"))
            archive.writestr("runs/rxn_o.out", (data_dir / "rxn_o.out").read_text("utf-8"))
            archive.writestr("runs/readme.txt", "ignored")

        molecules = load_directory_parallel(archive_path, workers=1)

        assert set(molecules) == {"rxn_h2", "rxn_o"}

    def test_an_unparseable_file_does_not_abort_the_run(self, tmp_path: Path) -> None:
        """One corrupt file must not lose the other 99."""

        (tmp_path / "junk.out").write_bytes(b"\x00\x01 not orca output")

        assert load_directory_parallel(tmp_path, workers=1) == {}


class TestReporting:
    """Report serialization."""

    def test_json_report_records_units_in_field_names(self, reaction_dir: Path) -> None:
        """Every numeric field must carry its unit in the key."""

        report = build_report(load_directory_parallel(reaction_dir, workers=1))
        job = report["molecules"]["rxn_h2o"]["jobs"][0]

        assert job["e_elec_eh"] == pytest.approx(-76.4)
        assert job["formula"] == "H2O"
        assert job["coords_unit"] == "angstrom"

    def test_csv_report_is_flat_and_round_trips(self, reaction_dir: Path, tmp_path: Path) -> None:
        """The CSV export must be loadable by a spreadsheet or pandas."""

        report = build_report(load_directory_parallel(reaction_dir, workers=1))
        destination = tmp_path / "out.csv"
        write_report(report, destination, "csv")

        with destination.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))

        assert len(rows) == 3
        assert {row["molecule"] for row in rows} == {"rxn_h2", "rxn_o", "rxn_h2o"}

    def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        """An unknown format is a caller error."""

        with pytest.raises(ValueError):
            write_report({}, tmp_path / "out.xml", "xml")


class TestCommandLine:
    """End-to-end CLI behaviour and exit codes."""

    def test_a_successful_run_exits_zero_and_writes_a_report(
        self, reaction_dir: Path, tmp_path: Path
    ) -> None:
        """The happy path."""

        destination = tmp_path / "report.json"
        code = main(["-d", str(reaction_dir), "-o", str(destination), "-w", "1"])

        assert code == 0
        assert json.loads(destination.read_text(encoding="utf-8"))["molecules"]

    def test_reaction_results_are_embedded_in_the_report(
        self, reaction_dir: Path, tmp_path: Path
    ) -> None:
        """``--rxn`` adds a reaction block with its consistency report."""

        destination = tmp_path / "report.json"
        main(
            [
                "-d",
                str(reaction_dir),
                "-o",
                str(destination),
                "-w",
                "1",
                "--rxn",
                "rxn_h2 + rxn_o -> rxn_h2o",
            ]
        )
        payload = json.loads(destination.read_text(encoding="utf-8"))

        assert payload["reaction"]["consistency"]["atom_balanced"] is True

    def test_strict_consistency_fails_an_unbalanced_reaction(
        self, reaction_dir: Path, tmp_path: Path
    ) -> None:
        """CI pipelines need a non-zero exit code for an invalid reaction."""

        code = main(
            [
                "-d",
                str(reaction_dir),
                "-o",
                str(tmp_path / "report.json"),
                "-w",
                "1",
                "--rxn",
                "rxn_h2 -> rxn_h2o",
                "--strict-consistency",
            ]
        )

        assert code == 3

    def test_a_missing_directory_exits_two(self, tmp_path: Path) -> None:
        """Usage errors are distinguished from empty results."""

        assert main(["-d", str(tmp_path / "absent"), "-w", "1"]) == 2

    def test_a_malformed_reaction_exits_two(self, reaction_dir: Path, tmp_path: Path) -> None:
        """A bad equation is a usage error."""

        code = main(
            ["-d", str(reaction_dir), "-o", str(tmp_path / "r.json"), "-w", "1", "--rxn", "a b c"]
        )

        assert code == 2

    def test_a_directory_with_no_outputs_exits_one(self, tmp_path: Path) -> None:
        """No parseable data is a distinct failure mode."""

        assert main(["-d", str(tmp_path), "-w", "1"]) == 1
