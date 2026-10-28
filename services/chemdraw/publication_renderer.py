from __future__ import annotations

"""Publication-grade chemical reaction rendering for the Chemistry Drawing Bot.

This module is deliberately isolated from the ORCA bot.  It depends only on
RDKit/Pillow plus Python's standard library and exposes backward-compatible
helpers that can be monkey-patched into ``app`` during staged rollout.
"""

import io
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence
from xml.sax.saxutils import escape

from PIL import Image, ImageChops, ImageDraw, ImageFont
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolDescriptors
from rdkit.Chem.Draw import rdMolDraw2D

RENDER_DPI = 600
DEFAULT_BOND_PX = 66.0
DEFAULT_FONT_PX = 44
DEFAULT_GAP_PX = 28
DEFAULT_ARROW_PX = 320
INK = (20, 20, 20)
WHITE = (255, 255, 255)

_FONT_CANDIDATES = (
    "DejaVuSans.ttf",
    "Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

_CONVENTIONAL_FORMULAS = {
    "O=O": "O2", "[H][H]": "H2", "N#N": "N2", "ClCl": "Cl2", "BrBr": "Br2",
    "II": "I2", "FF": "F2", "[O][O]": "O2", "O=[O+][O-]": "O3",
    "O=C=O": "CO2", "[C-]#[O+]": "CO", "O=S=O": "SO2", "O=S(=O)=O": "SO3",
    "[N]=O": "NO", "[O-][N+]=O": "NO2", "[N-]=[N+]=O": "N2O",
    "O": "H2O", "OO": "H2O2", "N": "NH3", "S": "H2S", "C": "CH4",
    "Cl": "HCl", "Br": "HBr", "I": "HI", "F": "HF", "P": "PH3",
    "OS(=O)(=O)O": "H2SO4", "O[N+](=O)[O-]": "HNO3", "OP(=O)(O)O": "H3PO4",
    "OC(=O)O": "H2CO3", "OS(=O)O": "H2SO3", "N#C": "HCN",
    "[OH-]": "OH-", "[H+]": "H+", "[NH4+]": "NH4+", "[OH3+]": "H3O+",
    "[Na+].[Cl-]": "NaCl", "[K+].[Cl-]": "KCl", "[Na+].[OH-]": "NaOH",
    "[K+].[OH-]": "KOH", "[Ca+2].[O-2]": "CaO",
    "[Na+].[Na+].[O-]C([O-])=O": "Na2CO3",
}


def _canonical_formula_table() -> dict[str, str]:
    out: dict[str, str] = {}
    for smiles, formula in _CONVENTIONAL_FORMULAS.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            out[Chem.MolToSmiles(mol)] = formula
    return out


_FORMULA_BY_CANONICAL_SMILES = _canonical_formula_table()


@dataclass(frozen=True)
class ParsedReactionTerm:
    coefficient: Fraction
    name: str

    @property
    def pretty_coefficient(self) -> str:
        if self.coefficient == 1:
            return ""
        if self.coefficient.denominator == 1:
            return str(self.coefficient.numerator)
        return f"{self.coefficient.numerator}/{self.coefficient.denominator}"


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def parse_coefficient(text: str) -> tuple[Fraction, str]:
    raw = re.sub(r"\s+", " ", text or "").strip()
    if not raw:
        return Fraction(1), ""
    m = re.match(r"^(\d+)\s*/\s*(\d+)\s+(.+)$", raw)
    if m and int(m.group(2)):
        return Fraction(int(m.group(1)), int(m.group(2))), m.group(3).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s+(.+)$", raw)
    if m:
        return Fraction(m.group(1)), m.group(2).strip()
    # glued integer coefficient only for a conservative bare formula
    m = re.match(r"^(\d+)((?:[A-Z][a-z]?\d*|\([A-Za-z0-9]+\)\d*)+)$", raw)
    if m:
        return Fraction(int(m.group(1))), m.group(2)
    return Fraction(1), raw


def split_reaction_side(text: str) -> list[ParsedReactionTerm]:
    """Split reaction species without destroying formal charges.

    Supports ``NH4+ + OH-`` and bracketed SMILES such as ``[NH4+] + [OH-]``.
    A top-level plus is treated as a separator when it is visibly separator-like
    (whitespace-delimited) or when another non-space token follows immediately.
    A terminal plus remains part of the species charge.
    """
    s = text or ""
    cuts: list[int] = []
    bracket = paren = 0
    for i, ch in enumerate(s):
        if ch == "[":
            bracket += 1
            continue
        if ch == "]" and bracket:
            bracket -= 1
            continue
        if bracket:
            continue
        if ch == "(":
            paren += 1
            continue
        if ch == ")" and paren:
            paren -= 1
            continue
        if ch != "+" or paren:
            continue

        prev = s[i - 1] if i else ""
        nxt = s[i + 1] if i + 1 < len(s) else ""
        right_nonspace = s[i + 1:].lstrip()
        if not right_nonspace:  # terminal charge, e.g. NH4+
            continue
        if right_nonspace.startswith("+"):  # NH4+ + OH- : first + is charge
            continue
        if prev.isspace() and nxt.isspace():
            cuts.append(i)
            continue
        if not prev.isspace() and not nxt.isspace():
            # compact equation, e.g. H2+O2.  Bracketed ionic charges were already protected.
            cuts.append(i)
            continue
        # one-sided whitespace: only split if both sides contain visible tokens.
        left = s[:i].rstrip()
        right = s[i + 1:].lstrip()
        if left and right and not (left[-1:].isdigit() and nxt.isspace()):
            cuts.append(i)

    if not cuts:
        coef, name = parse_coefficient(s)
        return [ParsedReactionTerm(coef, name)] if name else []

    parts: list[str] = []
    start = 0
    for idx in cuts:
        parts.append(s[start:idx])
        start = idx + 1
    parts.append(s[start:])
    out: list[ParsedReactionTerm] = []
    for part in parts:
        coef, name = parse_coefficient(part)
        if name:
            out.append(ParsedReactionTerm(coef, name.strip()))
    return out


def formula_for_display(smiles: str) -> str | None:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    canonical = Chem.MolToSmiles(mol)
    known = _FORMULA_BY_CANONICAL_SMILES.get(canonical)
    if known:
        return known
    carbons = sum(a.GetSymbol() == "C" for a in mol.GetAtoms())
    if mol.GetRingInfo().NumRings() or carbons > 1 or mol.GetNumHeavyAtoms() > 4:
        return None
    # Small species are clearer as formulae, but avoid collapsing common one-carbon
    # organic compounds (methanol, formamide, etc.) into ambiguous Hill formulae.
    if carbons and any(a.GetSymbol() in {"O", "N", "S"} and a.GetTotalNumHs() for a in mol.GetAtoms()):
        return None
    return rdMolDescriptors.CalcMolFormula(mol)


_FORMULA_RE = re.compile(r"(\^[^\s\^]+|[A-Z][a-z]?|\d+|[+-]\d*|\s+|.)")


def formula_tokens(text: str) -> list[tuple[str, str]]:
    """Return text chunks styled as normal/subscript/superscript."""
    raw = [p for p in _FORMULA_RE.findall(text or "") if p]
    out: list[tuple[str, str]] = []
    for i, part in enumerate(raw):
        if part.startswith("^"):
            if part[1:]:
                out.append((part[1:], "sup"))
        elif part.startswith(("+", "-")):
            out.append(((part[1:] + part[0]) if len(part) > 1 else part, "sup"))
        elif part.isdigit():
            prev = raw[i - 1] if i else ""
            out.append((part, "sub" if prev and prev[-1:].isalpha() else "normal"))
        else:
            out.append((part, "normal"))
    return out


def _formula_png(text: str, font_size: int = DEFAULT_FONT_PX) -> Image.Image:
    font = _load_font(font_size)
    small = _load_font(max(12, int(font_size * 0.68)))
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    pieces = formula_tokens(text)
    widths: list[int] = []
    normal_h = 0
    for chunk, style in pieces:
        f = small if style != "normal" else font
        box = probe.textbbox((0, 0), chunk, font=f)
        widths.append(max(1, box[2] - box[0]))
        normal_h = max(normal_h, box[3] - box[1])
    shift = max(5, int(font_size * 0.22))
    pad = max(8, font_size // 5)
    image = Image.new("RGB", (sum(widths) + 2 * pad, normal_h + 2 * shift + 2 * pad), WHITE)
    draw = ImageDraw.Draw(image)
    x = pad
    baseline = pad + shift
    for (chunk, style), width in zip(pieces, widths):
        f = small if style != "normal" else font
        box = draw.textbbox((0, 0), chunk, font=f)
        y = baseline - box[1]
        if style == "sub":
            y += normal_h - (box[3] - box[1]) + shift
        elif style == "sup":
            y -= shift
        draw.text((x - box[0], y), chunk, fill=INK, font=f)
        x += width
    return _trim(image, 4)


def _trim(image: Image.Image, pad: int = 8) -> Image.Image:
    bg = Image.new(image.mode, image.size, WHITE)
    box = ImageChops.difference(image, bg).getbbox()
    if not box:
        return image
    l, t, r, b = box
    return image.crop((max(0, l - pad), max(0, t - pad), min(image.width, r + pad), min(image.height, b + pad)))


def _prepare_mol(smiles: str) -> Chem.Mol | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        AllChem.Compute2DCoords(mol, canonOrient=True)
        Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass
    return mol


def _molecule_png(smiles: str, *, bond_px: float = DEFAULT_BOND_PX) -> Image.Image | None:
    mol = _prepare_mol(smiles)
    if mol is None:
        return None
    # Large temporary canvas + fixed bond length gives consistent visual scale
    # across different molecules; trim afterwards to content.
    w, h = 1600, 1100
    drawer = rdMolDraw2D.MolDraw2DCairo(w, h)
    opts = drawer.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = 2.4
    opts.fixedBondLength = float(bond_px)
    opts.fixedFontSize = 34
    opts.minFontSize = 28
    opts.maxFontSize = 40
    opts.padding = 0.04
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    return _trim(Image.open(io.BytesIO(drawer.GetDrawingText())).convert("RGB"), 10)


def _molecule_svg_fragment(smiles: str, *, bond_px: float = DEFAULT_BOND_PX) -> tuple[str, int, int] | None:
    mol = _prepare_mol(smiles)
    if mol is None:
        return None
    try:
        conf = mol.GetConformer()
        xs = [conf.GetAtomPosition(i).x for i in range(mol.GetNumAtoms())]
        ys = [conf.GetAtomPosition(i).y for i in range(mol.GetNumAtoms())]
        sx = max(xs) - min(xs) if xs else 1.0
        sy = max(ys) - min(ys) if ys else 1.0
    except Exception:
        sx, sy = 4.0, 3.0
    scale = bond_px / 1.5
    w = max(150, int(sx * scale + 90))
    h = max(120, int(sy * scale + 80))
    drawer = rdMolDraw2D.MolDraw2DSVG(w, h)
    opts = drawer.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = 2.4
    opts.fixedBondLength = float(bond_px)
    opts.fixedFontSize = 34
    opts.minFontSize = 28
    opts.maxFontSize = 40
    opts.padding = 0.06
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    raw = drawer.GetDrawingText()
    a = raw.find("<svg")
    b = raw.find(">", a) + 1 if a >= 0 else -1
    c = raw.rfind("</svg>")
    if b <= 0 or c <= b:
        return None
    return raw[b:c], w, h


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return max(0, box[2] - box[0])


def _draw_annotation(draw: ImageDraw.ImageDraw, center_x: int, baseline_y: int, text: str, font_size: int = 30) -> None:
    if not text:
        return
    font = _load_font(font_size)
    small = _load_font(max(12, int(font_size * 0.68)))
    pieces = formula_tokens(text)
    widths = []
    for chunk, style in pieces:
        f = small if style != "normal" else font
        widths.append(_text_width(draw, chunk, f))
    x = center_x - sum(widths) // 2
    for (chunk, style), width in zip(pieces, widths):
        f = small if style != "normal" else font
        box = draw.textbbox((0, 0), chunk, font=f)
        y = baseline_y - box[3]
        if style == "sup":
            y -= max(4, font_size // 3)
        elif style == "sub":
            y += max(3, font_size // 4)
        draw.text((x, y), chunk, fill=INK, font=f)
        x += width


def _annotation_width(text: str, font_size: int = 30) -> int:
    if not text:
        return 0
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    font = _load_font(font_size)
    small = _load_font(max(12, int(font_size * 0.68)))
    return sum(_text_width(probe, c, small if s != "normal" else font) for c, s in formula_tokens(text))


def _svg_formula(text: str, x: int, y: int, font_size: int = DEFAULT_FONT_PX) -> str:
    chunks = [f'<text x="{x}" y="{y}" font-family="Arial,Helvetica,sans-serif" font-size="{font_size}" fill="#141414">']
    for token, style in formula_tokens(text):
        e = escape(token)
        if style == "sub":
            chunks.append(f'<tspan font-size="{int(font_size*0.68)}" baseline-shift="sub">{e}</tspan>')
        elif style == "sup":
            chunks.append(f'<tspan font-size="{int(font_size*0.68)}" baseline-shift="super">{e}</tspan>')
        else:
            chunks.append(e)
    chunks.append("</text>")
    return "".join(chunks)


def _svg_annotation(text: str, cx: int, y: int, font_size: int = 30) -> str:
    if not text:
        return ""
    pieces = [f'<text x="{cx}" y="{y}" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-size="{font_size}" fill="#141414">']
    for token, style in formula_tokens(text):
        e = escape(token)
        if style == "sub":
            pieces.append(f'<tspan font-size="{int(font_size*0.68)}" baseline-shift="sub">{e}</tspan>')
        elif style == "sup":
            pieces.append(f'<tspan font-size="{int(font_size*0.68)}" baseline-shift="super">{e}</tspan>')
        else:
            pieces.append(e)
    pieces.append("</text>")
    return "".join(pieces)


def _build_tokens(reactant_pairs, product_pairs, small_as_formula: bool):
    tokens: list[tuple[str, object]] = []
    for side_i, pairs in enumerate((reactant_pairs, product_pairs)):
        if side_i:
            tokens.append(("arrow", None))
        for i, (coefficient, smiles) in enumerate(pairs):
            if i:
                tokens.append(("op", "+"))
            coef = Fraction(coefficient)
            if coef != 1:
                tokens.append(("coef", str(coef.numerator) if coef.denominator == 1 else str(coef)))
            formula = formula_for_display(smiles) if small_as_formula else None
            tokens.append(("formula" if formula else "mol", formula or smiles))
    return tokens


def render_reaction_png(
    reactant_pairs,
    product_pairs,
    *,
    small_as_formula: bool = True,
    arrow_top: str = "",
    arrow_bottom: str = "",
    dpi: int = RENDER_DPI,
) -> bytes | None:
    """Render a publication-ready raster scheme at 600 DPI.

    Molecular scale is fixed by bond length, not by arbitrary per-molecule
    resizing.  The canvas expands to accommodate long conditions instead of
    clipping or shrinking chemistry.
    """
    tokens = _build_tokens(reactant_pairs, product_pairs, small_as_formula)
    if not tokens:
        return None
    rendered: list[tuple[str, object, int, int]] = []
    font = _load_font(DEFAULT_FONT_PX)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    ann_w = max(_annotation_width(arrow_top), _annotation_width(arrow_bottom))
    arrow_w = max(DEFAULT_ARROW_PX, ann_w + 100)
    for kind, payload in tokens:
        if kind == "mol":
            image = _molecule_png(str(payload))
            if image is None:
                return None
            rendered.append((kind, image, image.width, image.height))
        elif kind == "formula":
            image = _formula_png(str(payload))
            rendered.append((kind, image, image.width, image.height))
        elif kind in {"op", "coef"}:
            box = probe.textbbox((0, 0), str(payload), font=font)
            rendered.append((kind, payload, max(1, box[2]-box[0]), max(1, box[3]-box[1])))
        else:
            rendered.append((kind, None, arrow_w, 30))

    gap = DEFAULT_GAP_PX
    margin_x = 48
    label_room_top = 55 if arrow_top else 0
    label_room_bottom = 60 if arrow_bottom else 0
    chemistry_h = max((h for _, _, _, h in rendered), default=120)
    width = sum(w for _, _, w, _ in rendered) + gap * (len(rendered)-1) + 2*margin_x
    height = chemistry_h + label_room_top + label_room_bottom + 70
    canvas = Image.new("RGB", (max(1, width), max(1, height)), WHITE)
    draw = ImageDraw.Draw(canvas)
    center_y = label_room_top + 35 + chemistry_h // 2
    x = margin_x
    for kind, payload, w, h in rendered:
        if kind in {"mol", "formula"}:
            image = payload
            canvas.paste(image, (x, center_y - image.height // 2))
        elif kind == "arrow":
            y = center_y
            head = 18
            draw.line((x, y, x+w-head, y), fill=INK, width=3)
            draw.polygon([(x+w, y), (x+w-head, y-8), (x+w-head, y+8)], fill=INK)
            cx = x + w//2
            if arrow_top:
                _draw_annotation(draw, cx, y-20, arrow_top)
            if arrow_bottom:
                _draw_annotation(draw, cx, y+58, arrow_bottom)
        else:
            box = draw.textbbox((0, 0), str(payload), font=font)
            draw.text((x-box[0], center_y-(box[3]-box[1])//2-box[1]), str(payload), fill=INK, font=font)
        x += w + gap
    out = io.BytesIO()
    canvas.save(out, format="PNG", dpi=(dpi, dpi), compress_level=6)
    return out.getvalue()


def render_reaction_svg(
    reactant_pairs,
    product_pairs,
    *,
    small_as_formula: bool = True,
    arrow_top: str = "",
    arrow_bottom: str = "",
) -> bytes | None:
    """Render a true vector SVG suitable for direct manuscript insertion."""
    tokens = _build_tokens(reactant_pairs, product_pairs, small_as_formula)
    if not tokens:
        return None
    items: list[tuple[str, object, int, int]] = []
    ann_w = max(len(arrow_top), len(arrow_bottom)) * 16
    arrow_w = max(DEFAULT_ARROW_PX, ann_w + 100)
    for kind, payload in tokens:
        if kind == "mol":
            frag = _molecule_svg_fragment(str(payload))
            if frag is None:
                return None
            items.append((kind, frag, frag[1], frag[2]))
        elif kind == "formula":
            w = max(70, len(str(payload))*25 + 20)
            items.append((kind, payload, w, 60))
        elif kind in {"op", "coef"}:
            items.append((kind, payload, max(36, len(str(payload))*26), 55))
        else:
            items.append((kind, None, arrow_w, 30))
    gap = DEFAULT_GAP_PX
    margin = 46
    top = 65 if arrow_top else 20
    bottom = 70 if arrow_bottom else 25
    chemistry_h = max((h for _, _, _, h in items), default=120)
    width = sum(w for _, _, w, _ in items) + gap*(len(items)-1) + 2*margin
    height = top + chemistry_h + bottom
    cy = top + chemistry_h//2
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#141414}</style>',
    ]
    x = margin
    for kind, payload, w, h in items:
        if kind == "mol":
            frag, _, _ = payload
            out.append(f'<g transform="translate({x},{cy-h//2})">{frag}</g>')
        elif kind == "formula":
            out.append(_svg_formula(str(payload), x+6, cy+15))
        elif kind in {"op", "coef"}:
            out.append(f'<text x="{x}" y="{cy+15}" font-size="{DEFAULT_FONT_PX}" font-weight="500">{escape(str(payload))}</text>')
        else:
            end = x+w-8
            head = end-18
            out.append(f'<line x1="{x}" y1="{cy}" x2="{head}" y2="{cy}" stroke="#141414" stroke-width="3"/>')
            out.append(f'<path d="M {end} {cy} L {head} {cy-8} L {head} {cy+8} Z" fill="#141414"/>')
            cx = x+w//2
            if arrow_top:
                out.append(_svg_annotation(arrow_top, cx, cy-20))
            if arrow_bottom:
                out.append(_svg_annotation(arrow_bottom, cx, cy+52))
        x += w+gap
    out.append("</svg>")
    return "".join(out).encode("utf-8")


def render_reaction_scheme(reactant_pairs, product_pairs, sub_size=None, small_as_formula: bool = True, arrow_top: str = "", arrow_bottom: str = "") -> bytes | None:
    """Backward-compatible alias used by the existing Telegram handler."""
    return render_reaction_png(
        reactant_pairs,
        product_pairs,
        small_as_formula=small_as_formula,
        arrow_top=arrow_top,
        arrow_bottom=arrow_bottom,
    )
