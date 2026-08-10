# -*- coding: utf-8 -*-
"""Publication-quality 2D molecular rendering for Chemistry Lab.

The implementation is based on the supplied Chemistry Drawing Bot renderer,
with additional deterministic CoordGen preparation, ACS-style drawing options,
high-resolution PNG output, whitespace trimming, and SVG export.
"""
from __future__ import annotations

import io
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import AllChem, rdDepictor, rdCoordGen
from rdkit.Chem.Draw import rdMolDraw2D

MOL_IMAGE_SIZE = (900, 700)
DRAWING_BOND_LINE_WIDTH = 2.2
DRAWING_FONT_SCALE = 0.95
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

    # CoordGen is preferred for cleaner fused rings and heterocycles. The
    # fallback keeps compatibility with RDKit builds where CoordGen fails on
    # an unusual structure.
    try:
        rdCoordGen.AddCoords(mol)
    except Exception:
        try:
            rdDepictor.Compute2DCoords(mol, canonOrient=True)
        except Exception:
            AllChem.Compute2DCoords(mol)

    try:
        rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass
    return mol


def apply_draw_options(drawer) -> None:
    """Apply a restrained, publication-oriented chemical drawing style."""
    opts = drawer.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = DRAWING_BOND_LINE_WIDTH
    opts.baseFontSize = DRAWING_FONT_SCALE
    opts.legendFontSize = 20
    opts.padding = DRAWING_PADDING
    try:
        opts.fixedBondLength = 38
    except Exception:
        pass
    try:
        opts.multipleBondOffset = 0.16
    except Exception:
        pass
    try:
        opts.minFontSize = 14
    except Exception:
        pass
    try:
        opts.annotationFontScale = 0.75
    except Exception:
        pass
    try:
        rdMolDraw2D.SetACS1996Mode(drawer)
    except Exception:
        pass


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
