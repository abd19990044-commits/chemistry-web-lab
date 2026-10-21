"""Geometry imports must preserve every atom or fail explicitly."""
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem
import chem_core as core


@pytest.mark.parametrize('comment', ['', 'water geometry'])
def test_xyz_comment_line_preserves_first_atom(comment):
    text = f'3\n{comment}\nO 0 0 0\nH 0 0 1\nH 0 1 0\n'
    assert core.normalize_xyz_text(text).splitlines() == ['O 0 0 0', 'H 0 0 1', 'H 0 1 0']


@pytest.mark.parametrize('text', [
    '3\nwater\nO 0 0 0\nH 0 0 1\n',
    '1\nwater\nO 0 0 0\nH 0 0 1\n',
    'C nan 0 0', 'C 1e999 0 0', 'C 0 inf 0', 'water 1 2 3', 'C 0 0',
])
def test_xyz_corruption_is_rejected_instead_of_partial_import(text):
    assert core.normalize_xyz_text(text) is None


def test_bare_xyz_accepts_finite_fortran_exponent():
    assert core.normalize_xyz_text('C 1D-1 0 0') == 'C 0.1 0 0'


@pytest.mark.parametrize('v3000', [False, True])
def test_mol_v2000_and_v3000_preserve_hydrogens_and_geometry(v3000):
    mol = Chem.AddHs(Chem.MolFromSmiles('O'))
    assert AllChem.EmbedMolecule(mol, randomSeed=42) == 0
    text = Chem.MolToMolBlock(mol, forceV3000=v3000)
    xyz = core.mol_block_to_xyz(text)
    assert xyz is not None
    assert [line.split()[0] for line in xyz.splitlines()] == ['O', 'H', 'H']
    for i, line in enumerate(xyz.splitlines()):
        point = mol.GetConformer().GetAtomPosition(i)
        assert [float(v) for v in line.split()[1:4]] == pytest.approx([point.x, point.y, point.z], abs=0.0001)


def test_truncated_mol_fails_instead_of_returning_partial_geometry():
    mol = Chem.AddHs(Chem.MolFromSmiles('O'))
    AllChem.EmbedMolecule(mol, randomSeed=42)
    lines = Chem.MolToMolBlock(mol).splitlines()
    assert core.mol_block_to_xyz('\n'.join(lines[:5])) is None
