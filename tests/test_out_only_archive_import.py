"""Mixed calculation archives retain only outputs in both analysis workflows."""
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import zipfile

import pytest
import app as webapp

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "orca_engine/tests/data"


def archive_bytes(kind, members, compression=zipfile.ZIP_STORED):
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w", compression=compression) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
    else:
        mode = {"tar": "w", "tar.gz": "w:gz", "tar.xz": "w:xz", "tar.bz2": "w:bz2"}[kind]
        with tarfile.open(fileobj=buf, mode=mode) as archive:
            for name, content in members.items():
                item = tarfile.TarInfo(name)
                item.size = len(content)
                archive.addfile(item, io.BytesIO(content))
    return buf.getvalue()


@pytest.mark.parametrize("kind", ["zip", "tar", "tar.gz", "tar.xz", "tar.bz2"])
@pytest.mark.parametrize("workflow", ["parse", "thermochemistry"])
def test_mixed_archives_keep_only_nested_outputs(monkeypatch, tmp_path, kind, workflow):
    monkeypatch.setattr(webapp, "UPLOADS_BASE_DIR", str(tmp_path))
    members = {
        f"results/{name}.{'OUT' if name == 'O' else 'out'}": (DATA / source).read_bytes()
        for name, source in [("H2", "rxn_h2.out"), ("O", "rxn_o.out"), ("H2O", "rxn_h2o.out")]
    }
    # These are valid ORCA text too: filtering must happen before parsing.
    content = (DATA / "rxn_h2o.out").read_bytes()
    members.update({f"extras/calc{suffix}": content for suffix in [".log", ".txt", ".property.txt", ".xyz", ".gbw", ".out.bak", ".molden.input"]})
    members["__MACOSX/ignored.out"] = content
    members["../escape.out"] = content
    field = "file" if workflow == "parse" else "archive"
    with webapp.app.test_client() as client:
        response = client.post(f"/api/orca/engine/{workflow}", data={
            field: (io.BytesIO(archive_bytes(kind, members)), f"results.{kind}"),
            "equation": "H2 + O -> H2O",
        })
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["ok"]
    if workflow == "parse":
        assert {e["filename"] for e in payload["archive_entries"]} == set(list(members)[:3])
    else:
        assert "delta_gibbs_kcal_mol" in payload["result"] or "delta_g_kcal_mol" in payload["result"]
    saved = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert {p.name for p in saved} == {"H2.out", "O.OUT", "H2O.out"}
    assert not (tmp_path.parent / "escape.out").exists()


@pytest.mark.parametrize("workflow", ["parse", "thermochemistry"])
def test_no_output_does_not_parse_archive_bytes_as_orca(monkeypatch, tmp_path, workflow):
    monkeypatch.setattr(webapp, "UPLOADS_BASE_DIR", str(tmp_path))
    # ZIP_STORED leaves recognizable ORCA text visible in the archive itself.
    raw = archive_bytes("zip", {"calc.log": (DATA / "thermo.out").read_bytes()})
    field = "file" if workflow == "parse" else "archive"
    with webapp.app.test_client() as client:
        response = client.post(f"/api/orca/engine/{workflow}", data={
            field: (io.BytesIO(raw), "no_outputs.zip"), "equation": "A -> B",
        })
    assert response.status_code == 422
    assert ".out" in str(response.get_json())
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_rar_filters_before_reading_member_content(monkeypatch, tmp_path):
    class Rar:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def infolist(self):
            return [SimpleNamespace(filename=name, file_size=4, is_dir=lambda: False)
                    for name in ["folder/calc.OUT", "calc.log", "coords.xyz", "../evil.out"]]

        def read(self, item):
            assert item.filename == "folder/calc.OUT"
            return b"ORCA"

    monkeypatch.setattr(webapp, "RAR_AVAILABLE", True)
    monkeypatch.setattr(webapp.rarfile, "RarFile", Rar)
    entries = webapp.extract_calculation_files_from_archive(b"fake rar", "calc.rar", str(tmp_path))
    assert [entry["basename"] for entry in entries] == ["calc.OUT"]
    assert [p.name for p in tmp_path.iterdir()] == ["calc.OUT"]


@pytest.mark.parametrize("kind,compression", [("zip", zipfile.ZIP_STORED), ("zip", zipfile.ZIP_DEFLATED), ("tar", 0), ("tar.gz", 0)])
def test_browser_worker_drops_non_outputs_before_size_checks(kind, compression):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    members = {"nested/calc.OUT": b"ORCA one", "other/calc.out": b"ORCA two",
               "READMe.out": b"ORCA three", "large.gbw": b"x" * 2000,
               "calc.log": b"ORCA", "../escape.out": b"ORCA", "C:/evil.out": b"ORCA",
               "__MACOSX/hidden.out": b"ORCA"}
    raw = archive_bytes(kind, members, compression)
    script = r'''
const fs = require('node:fs'), vm = require('node:vm');
globalThis.crypto = require('node:crypto').webcrypto;
const messages = [];
globalThis.self = {postMessage: m => messages.push(m)};
vm.runInThisContext(fs.readFileSync('static/js/workers/archive-worker.js', 'utf8'));
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const buffer = Uint8Array.from(Buffer.from(input.bytes, 'base64')).buffer;
(async () => {
    await self.onmessage({data: {action: 'PROCESS_ARCHIVE', filename: input.name, buffer,
        config: {maxIndividualFileSizeBytes: 32}}});
    console.log(JSON.stringify(messages.at(-1)));
})().catch(e => {console.error(e); process.exit(1);});
'''
    result = subprocess.run([node, "-e", script], input=json.dumps({
        "bytes": base64.b64encode(raw).decode(), "name": f"mixed.{kind}",
    }), cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["type"] == "COMPLETE", payload
    assert {e["name"] for e in payload["entries"]} == {"nested/calc.OUT", "other/calc.out", "READMe.out"}
    assert all(e["tier"] == "ANALYZE" and e["content"].startswith("ORCA") for e in payload["entries"])
