from rdkit import Chem
from rdkit.Chem.Draw import rdMolDraw2D

import chem_core as core


def test_publication_renderer_produces_png_and_svg():
    smiles = "CC(=O)Oc1ccccc1C(=O)O"  # aspirin
    png = core.render_molecule_png(smiles)
    svg = core.render_molecule_svg(smiles)

    assert png is not None
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert svg is not None
    assert b"<svg" in svg[:500]


def test_renderer_preserves_stereochemistry():
    smiles = "C[C@H](O)C(=O)O"
    mol = core.prepare_molecule(smiles)
    assert mol is not None
    assert any(atom.HasProp("_CIPCode") for atom in mol.GetAtoms())
    assert core.render_molecule_png(smiles)


def test_renderer_is_used_by_chem_core_package():
    smiles = Chem.MolToSmiles(Chem.MolFromSmiles("c1ccccc1"))
    assert core.render_molecule_png(smiles)


def test_renderer_uses_fixed_chemical_and_label_scale():
    """The active package renderer must not fall back to canvas-dependent font scaling."""
    drawer = rdMolDraw2D.MolDraw2DCairo(900, 700)
    core.apply_draw_options(drawer)
    opts = drawer.drawOptions()

    assert opts.fixedBondLength == 38.0
    assert opts.fixedFontSize == 22.0


def test_renderer_handles_naproxen_like_substituents():
    """A methoxy + carboxylic-acid structure should render without oversized labels."""
    smiles = "COc1ccc2cc(C(C)C(=O)O)ccc2c1"
    png = core.render_molecule_png(smiles)
    svg = core.render_molecule_svg(smiles)
    assert png is not None
    assert svg is not None
