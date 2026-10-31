"""NEB-TS phase completion and coordinate-preserving restart regressions."""
import pytest

from orca_orchestrator import orca_artifacts as art

BAND = "*** THE NEB OPTIMIZATION HAS CONVERGED ***\n"
TS = "*** THE OPTIMIZATION HAS CONVERGED ***\n"
NORMAL = "****ORCA TERMINATED NORMALLY****\n"
FREQ = "VIBRATIONAL FREQUENCIES\n"


def legacy_function(name):
    import ast
    import re
    import kaggle_runner
    source = kaggle_runner.KAGGLE_RUNNER_BODY
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {"re": re}
    exec(compile(ast.get_source_segment(source, node), "legacy-helper.py", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("implementation", ["modern", "legacy"])
@pytest.mark.parametrize("directives", [
    'NEB_Restart_XYZFile "initial.allxyz"',
    'neb_restart_xyzfile initial.allxyz',
    'Restart_ALLXYZFile "stale.allxyz" NEB_Restart_XYZFile "initial.allxyz"',
])
def test_avogadro_restart_alias_is_replaced_not_retained(implementation, directives):
    rewrite = art.set_neb_restart if implementation == "modern" else legacy_function("_set_neb_restart")
    inp = '# generated input\n! M062X NEB-TS Freq def2-SVP SMD(WATER)\n%neb\n  ' + directives + '\nend\n'
    output = rewrite(inp, "latest_MEP.allxyz")
    assert output.count("Restart_ALLXYZFile") == 1
    assert "NEB_Restart_XYZFile" not in output and "initial.allxyz" not in output
    assert "stale.allxyz" not in output
    assert '! M062X NEB-TS Freq def2-SVP SMD(WATER)' in output
    assert 'Restart_ALLXYZFile "latest_MEP.allxyz"' in output


def test_legacy_result_zip_includes_the_handoff_note_from_output_directory(tmp_path):
    import glob
    import os
    import shutil
    import zipfile
    work, output = tmp_path / "work", tmp_path / "output"
    work.mkdir()
    output.mkdir()
    primary = work / "sample.out"
    primary.write_text("unfinished NEB path\n", encoding="utf-8")
    package = legacy_function("_package_results")
    def write_note(path, text):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(text + "\n")
    package.__globals__.update({
        "os": os, "glob": glob, "shutil": shutil, "zipfile": zipfile,
        "WORKDIR": str(work), "OUTPUT_DIR": str(output), "BASENAME": "sample",
        "RESULT_BUDGET_BYTES": 1024 * 1024, "OUT_TAIL_BYTES": 1024,
        "_molden_status": "MOLDEN_UNAVAILABLE", "_molden_detail": "not installed",
        "_ordered_result_files": lambda: [str(primary)], "_wp": lambda name: str(work / name),
        "_write_text": write_note, "_gb": str, "log": lambda *_args: None,
    })
    package("Successor push failed; verified path is retained.")
    with zipfile.ZipFile(output / "results.zip") as archive:
        assert "Successor push failed" in archive.read("JOB_NOTE.txt").decode("utf-8")
        assert "sample.out" in archive.namelist()


@pytest.mark.parametrize("variant", ["NEB-TS", "FAST-NEB-TS", "LOOSE-NEB-TS", "TIGHT-NEB-TS", "ZOOM-NEB-TS"])
def test_converged_band_is_not_a_finished_transition_state(variant):
    inp = f"! B3LYP {variant}\n%neb NImages 8 end\n"
    result = art.classify_outcome(BAND + NORMAL, job_kind="neb", input_text=inp)
    assert result.is_continuable and not result.is_complete
    assert "transition-state" in result.reason
    assert art.classify_outcome(BAND + TS + NORMAL, job_kind="neb", input_text=inp).is_complete


def test_neb_requires_its_own_convergence_and_ignores_comment_requests():
    assert not art.classify_outcome(TS + NORMAL, job_kind="neb", input_text="! NEB\n").is_complete
    assert art.classify_outcome(BAND + NORMAL, job_kind="neb", input_text="! NEB-CI # NEB-TS Freq\n").is_complete
    assert not art.classify_outcome("| 1> # " + BAND + NORMAL, job_kind="neb", input_text="! NEB\n").is_complete


def test_endpoint_convergence_is_not_ts_convergence():
    result = art.classify_outcome(TS + BAND + NORMAL, job_kind="neb", input_text="! NEB-TS\n")
    assert result.is_continuable
    assert not art.classify_outcome(BAND + "| 1> # " + TS + NORMAL,
                                    job_kind="neb", input_text="! NEB-TS\n").is_complete


@pytest.mark.parametrize("keyword", ["Freq", "NumFreq"])
def test_requested_frequencies_must_follow_the_converged_ts(keyword):
    inp = f"! NEB-TS {keyword}\n"
    assert not art.classify_outcome(BAND + TS + NORMAL, job_kind="neb", input_text=inp).is_complete
    assert not art.classify_outcome(FREQ + BAND + TS + NORMAL, job_kind="neb", input_text=inp).is_complete
    assert art.classify_outcome(BAND + TS + FREQ + NORMAL, job_kind="neb", input_text=inp).is_complete


def test_fast_neb_can_switch_directly_to_ts_but_endpoint_preopt_is_insufficient():
    inp = "! FAST-NEB-TS\n"
    switch = "Next a TS optimization is performed on the CI from the NEB run.\n"
    assert art.classify_outcome(switch + TS + NORMAL, job_kind="neb", input_text=inp).is_complete
    assert not art.classify_outcome(TS + NORMAL, job_kind="neb", input_text=inp).is_complete


def test_later_unconverged_ts_cycle_and_watchdog_kill_override_old_success():
    inp = "! NEB-TS\n"
    out = BAND + TS + "GEOMETRY OPTIMIZATION CYCLE 1\n" + NORMAL
    assert not art.classify_outcome(out, job_kind="neb", input_text=inp).is_complete
    assert not art.classify_outcome(BAND + TS + NORMAL, job_kind="neb", input_text=inp,
                                    killed_by="time").is_complete
    assert not art.classify_outcome(BAND + "THE NEB OPTIMIZATION HAS NOT CONVERGED\n" + NORMAL,
                                    job_kind="neb", input_text="! NEB\n").is_complete


def path_text(x="1.0", comment="image"):
    return f"1\n{comment}\nH 0 0 0\n>\n1\n{comment}\nH {x} 0 0\n"


def test_path_identity_tracks_coordinates_not_comments_or_numeric_format(tmp_path):
    path = tmp_path / "mol_MEP.allxyz"
    path.write_text(path_text(), encoding="utf-8")
    first = art.validate_allxyz(str(path))
    path.write_text(path_text("1.00000", "a fresh timestamp"), encoding="utf-8")
    same = art.validate_allxyz(str(path))
    path.write_text(path_text("1.1", "a fresh timestamp"), encoding="utf-8")
    moved = art.validate_allxyz(str(path))
    assert first.ok and same.ok and moved.ok
    assert first.detail["images"] == moved.detail["images"] == 2
    assert first.detail["geometry_sha256"] == same.detail["geometry_sha256"]
    assert first.detail["geometry_sha256"] != moved.detail["geometry_sha256"]


@pytest.mark.parametrize("coordinate", ["nan", "inf", "-inf", "not-a-number"])
def test_nonfinite_or_invalid_path_coordinates_are_not_restartable(tmp_path, coordinate):
    path = tmp_path / "mol_MEP.allxyz"
    path.write_text(path_text(coordinate), encoding="utf-8")
    assert not art.validate_allxyz(str(path)).ok


def test_empty_xyz_comments_are_valid_but_torn_frames_are_rejected(tmp_path):
    path = tmp_path / "mol_MEP.allxyz"
    path.write_text(path_text(comment=""), encoding="utf-8")
    assert art.validate_allxyz(str(path)).ok
    path.write_text(path_text()[:-7], encoding="utf-8")
    assert not art.validate_allxyz(str(path)).ok


@pytest.mark.parametrize("product", ["product.xyz", "end.xyz", "scan.xyz"])
def test_restart_replaces_stale_path_and_preserves_product_and_comments(product):
    inp = f'! NEB-TS\n%neb Product "{product}" Restart_ALLXYZFile "old.allxyz" NImages 8 end\n'
    inp += '# Restart_ALLXYZFile "comment.allxyz"\n'
    result = art.set_neb_restart(inp, "latest_MEP.allxyz")
    assert 'Restart_ALLXYZFile "latest_MEP.allxyz"' in result
    assert '"old.allxyz"' not in result
    assert f'Product "{product}"' in result and "NImages 8" in result
    assert '# Restart_ALLXYZFile "comment.allxyz"' in result


def test_generated_kernel_uses_input_aware_neb_completion():
    from orca_orchestrator.credentials import KaggleCredentials
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.runner.builder import build_header, render_script

    job = JobManifest.create(job_id="chem-tools-neb-1a2b3c4d", owner="tester", title="neb",
                             input_filename="mol.inp", original_input_sha256="s", job_kind="neb")
    header = build_header(job=job, epoch=0, creds=KaggleCredentials(username="tester", key="0" * 32),
                          inline_files={"mol.inp": b"! NEB-TS Freq\n"})
    script = render_script(header)
    compile(script, "neb-kernel.py", "exec")
    assert "input_text=text" in script and '"neb_path"' in script

