"""Electron-state edits preserve chemistry, hydrogen count and file metadata."""
from tests.test_editor_integration import node


def test_deletion_preserves_hydrogens_and_marks_each_surviving_end():
    node(r'''
require('./static/js/editor-electrons.js');const assert=require('node:assert/strict');
const atom=(h=3,charge=0,radical=0)=>({label:'C',h,charge,radical,fixedH:null});
const before={atoms:new Map([[0,atom()],[1,atom()]]),bonds:new Map([[0,{begin:0,end:1,type:1}]])};
const after={atoms:new Map([[0,atom(4)],[1,atom(4)]]),bonds:new Map()};
const edits=EditorElectrons.deletionEdits(before,after);assert.equal(edits.length,2);assert.deepEqual(edits[0],{id:0,attrs:{implicitHCount:3,radical:2}});
assert.equal(EditorElectrons.deletionEdits(before,after,'cap').length,0);
assert.deepEqual(EditorElectrons.deletionEdits(before,after,'manual')[0].attrs,{implicitHCount:3});
const removedAtom={atoms:new Map([[0,atom(4)]]),bonds:new Map()};assert.equal(EditorElectrons.deletionEdits(before,removedAtom).length,1);
const redo={atoms:new Map([[0,{...atom(),radical:2,fixedH:3}],[1,{...atom(),radical:2,fixedH:3}]]),bonds:new Map()};assert.equal(EditorElectrons.deletionEdits(before,redo).length,0);
const replacement={atoms:new Map([[0,{...atom(),label:'N'}]]),bonds:new Map()};assert.equal(EditorElectrons.deletionEdits(before,replacement).length,0);
const bondEdit={atoms:before.atoms,bonds:new Map([[0,{begin:0,end:1,type:2}]])};assert.equal(EditorElectrons.deletionEdits(before,bondEdit).length,0);
const withH={atoms:new Map([[0,atom()],[1,atom()],[2,{label:'H'}]]),bonds:new Map()};assert(EditorElectrons.hydrogenOnlyChange(withH,after));assert(!EditorElectrons.hydrogenOnlyChange(before,removedAtom));
''')


def test_atom_state_preserves_other_atom_properties_and_validates_input():
    node(r'''
require('./static/js/editor-electrons.js');const assert=require('node:assert/strict');
const atom={label:'N',isotope:15,charge:0,clone(){return {...this};}};
const ion=EditorElectrons.atomAttrs(atom,1,0,0);assert.equal(ion.label,'N');assert.equal(ion.isotope,15);assert.equal(ion.charge,1);assert.equal(ion.implicitHCount,0);assert.equal(atom.charge,0);
assert.equal(EditorElectrons.atomAttrs(atom,-1,2,null).radical,2);
assert.throws(()=>EditorElectrons.atomAttrs(atom,.5,0,0));assert.throws(()=>EditorElectrons.atomAttrs(atom,0,4,0));assert.throws(()=>EditorElectrons.atomAttrs(atom,0,0,9));
''')


def test_radical_and_ion_chemical_files_keep_mdl_states():
    import json
    from pathlib import Path
    import subprocess
    from rdkit import Chem

    code = r'''
require('./static/js/molecule-graph.js');
const atoms=[{id:1,elem:'C',radical:2,x:0,y:0,z:0},{id:2,elem:'O',charge:-1,radical:0,x:5,y:0,z:0}];
console.log(JSON.stringify(MoleculeGraph.serialize(atoms,[],'sdf','Radical and ion')));
'''
    result = subprocess.run(['node', '-e', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
    text = json.loads(result.stdout)['text']
    assert 'M  RAD' in text and 'M  CHG' in text
    molecule = Chem.MolFromMolBlock(text.split('$$$$')[0], sanitize=False, removeHs=False)
    assert molecule.GetAtomWithIdx(0).GetNumRadicalElectrons() == 1
    assert molecule.GetAtomWithIdx(1).GetFormalCharge() == -1
