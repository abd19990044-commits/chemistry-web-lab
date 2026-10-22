"""Topology invariants independent of the browser/WASM integration CI job."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(script):
    if not shutil.which('node'):
        pytest.skip('Node.js is required')
    graph = ROOT / 'static/js/molecule-graph.js'
    result = subprocess.run(['node', '-e', f'require({str(graph)!r});const G=globalThis.MoleculeGraph;const a=require("node:assert/strict");\n' + script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_authoritative_orders_survive_crossing_drag_and_redraw():
    node('''
const atoms=[{id:10,elem:'C',x:0,y:0,z:0},{id:20,elem:'O',x:1.2,y:0,z:0},{id:30,elem:'C',x:4,y:0,z:0}];
const bonds=[{a1:10,a2:20,order:2}],model={selectedAtoms:()=>atoms};
G.apply(model,bonds);a.deepEqual(atoms[0].bondOrder,[2]);
atoms[2].x=.8;atoms[1].x=5;
G.apply(model,bonds);a.deepEqual(atoms.map(x=>x.bonds),[[1],[0],[]]);a.deepEqual(atoms[1].bondOrder,[2]);
G.apply(model,[]);a.deepEqual(atoms.map(x=>x.bonds),[[],[],[]]);
''')


def test_valence_uses_orders_charge_and_zero_for_noble_gases():
    node('''
const atoms=[{id:1,elem:'C'},{id:2,elem:'O'},{id:3,elem:'O'},{id:4,elem:'H'},{id:5,elem:'He'},{id:6,elem:'N',charge:1}];
const bonds=[{a1:1,a2:2,order:2},{a1:1,a2:3,order:2}];
a.equal(G.canAdd(atoms,bonds,1,4),false);a.equal(G.canAdd(atoms,bonds,5,4),false);a.equal(G.maxValence(atoms[5]),4);
a.equal(G.canAdd(atoms,bonds,4,4),false);
a.throws(()=>G.validate([{id:1,elem:'C',x:NaN,y:0,z:0}],[]));
''')


def test_estimated_connectivity_never_bonds_fragments_or_noble_gases():
    node('''
const atoms=[{id:1,elem:'C',molId:1,x:0,y:0,z:0},{id:2,elem:'C',molId:2,x:1.4,y:0,z:0},{id:3,elem:'He',molId:1,x:1,y:0,z:0}];
a.deepEqual(G.infer(atoms),[]);
''')


def test_studio_operations_use_worker_and_never_server_geometry_endpoints():
    app = (ROOT / 'static/js/app.js').read_text(encoding='utf-8')
    for endpoint in ['/api/orca/builder/clean', '/api/orca/coords/file', 'postJSON("/api/orca/coords",']:
        assert endpoint not in app
    assert 'BrowserChemistry.lookup(q)' in app
    assert 'BrowserChemistry.file(file)' in app
    assert 'BrowserChemistry.optimize({atoms,bonds,forceField}' in app
    html = (ROOT / 'templates/index.html').read_text(encoding='utf-8')
    assert html.index('js/molecule-graph.js') < html.index('js/browser-chemistry.js') < html.index('js/app.js')
