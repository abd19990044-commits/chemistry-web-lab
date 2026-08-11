# -*- coding: utf-8 -*-
"""Publication-quality 2D molecular rendering for Chemistry Lab.

The implementation is based on the supplied Chemistry Drawing Bot renderer,
with deterministic CoordGen preparation, a fixed chemical bond scale, a fixed
atom-label scale, high-resolution PNG output, whitespace trimming, and SVG
export.
"""
from __future__ import annotations

import io

from rdkit import Chem
from rdkit.Chem import AllChem, rdDepictor, rdCoordGen
from rdkit.Chem.Draw import rdMolDraw2D

MOL_IMAGE_SIZE = (900, 700)
DRAWING_BOND_LINE_WIDTH = 2.2
DRAWING_FIXED_BOND_LENGTH = 38.0
DRAWING_FIXED_FONT_SIZE = 22.0
DRAWING_PADDING = 0.08


def prepare_molecule(smiles: str):
    """Parse, sanitize and deterministically prepare a molecule for 2D drawing."""
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        return None

    # CoordGen gives cleaner fused-ring and heterocycle layouts. The fallback
    # keeps compatibility with RDKit builds where CoordGen is unavailable or
    # fails on an unusual structure.
    try:
        rdCoordGen.AddCoords(mol)
    except Exception:
        try:
            rdDepictor.Compute2DCoords(mol, canonOrient=True, bondLength=1.0)
        except Exception:
            AllChem.Compute2DCoords(mol, bondLength=1.0)

    try:
        rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass
    return mol


def apply_draw_options(drawer) -> None:
    """Apply a stable, publication-oriented chemical drawing style.

    RDKit normally derives atom-label size from the final molecular scale. That
    is useful for fitting arbitrary molecules into a canvas, but it causes the
    O/OH/N labels to grow or shrink with the size of the whole structure. The
    application wants a chemically consistent drawing: bond lengths and atom
    labels remain stable when substituents are added or removed. RDKit exposes
    both controls explicitly, so both are fixed here.
    """
    opts = drawer.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = DRAWING_BOND_LINE_WIDTH
    opts.fixedBondLength = DRAWING_FIXED_BOND_LENGTH
    opts.fixedFontSize = DRAWING_FIXED_FONT_SIZE
    opts.padding = DRAWING_PADDING
    opts.annotationFontScale = 0.75
    opts.multipleBondOffset = 0.16
    opts.additionalAtomLabelPadding = 0.02
    # Do not call SetACS1996Mode here. Its Python API requires a MolDrawOptions
    # object plus a mean bond length, and it also overwrites fixedFontSize. The
    # previous one-argument call was silently caught, so it never configured
    # ACS mode while making the drawing code look as though it had.


def trim_white(image, pad: int = 12):
    from PIL import Image, ImageChops

    background = Image.new(image.mode, image.size, (255, 255, 255))
    box = ImageChops.difference(image, background).getbbox()
    if not box:
        return image
    left, top, right, bottom = box
    return image.crop(
        (
            max(0, left - pad),
            max(0, top - pad),
            min(image.width, right + pad),
            min(image.height, bottom + pad),
        )
    )


def render_molecule_png(smiles: str, legend: str = "", size=MOL_IMAGE_SIZE) -> bytes | None:
    """Return a high-resolution PNG suitable for web display and figures."""
    mol = prepare_molecule(smiles)
    if mol is None:
        return None
    try:
        drawer = rdMolDraw2D.MolDraw2DCairo(int(size[0]), int(size[1]))
        apply_draw_options(drawer)
        rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend or "")
        drawer.FinishDrawing()
        from PIL import Image

        image = Image.open(io.BytesIO(drawer.GetDrawingText())).convert("RGB")
        image = trim_white(image)
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True, dpi=(300, 300))
        return out.getvalue()
    except Exception:
        return None


def render_molecule_svg(smiles: str, legend: str = "", size=MOL_IMAGE_SIZE) -> bytes | None:
    """Return the same depiction as vector SVG for publication workflows."""
    mol = prepare_molecule(smiles)
    if mol is None:
        return None
    try:
        drawer = rdMolDraw2D.MolDraw2DSVG(int(size[0]), int(size[1]))
        apply_draw_options(drawer)
        rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend or "")
        drawer.FinishDrawing()
        return drawer.GetDrawingText().encode("utf-8")
    except Exception:
        return None
