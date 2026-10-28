from __future__ import annotations

"""Publication renderer V2.

Focused visual refinement layer over :mod:`publication_renderer`.
It keeps the established parser/RDKit machinery while improving manuscript
layout and using condensed structural formulae for very small organic species
whose skeletal depiction would otherwise be visually ambiguous.

Condensed structural formulae are rendered chemically: bond symbols are not
font glyphs. Single, double and triple bonds are drawn as real vector/raster
lines aligned to the centres of the adjacent atom/group labels.
"""

import io
import re
from fractions import Fraction
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw
from rdkit import Chem

from . import publication_renderer as base

RENDER_DPI = 600
DEFAULT_BOND_PX = 62.0
DEFAULT_FONT_PX = 42
DEFAULT_GAP_PX = 23
DEFAULT_ARROW_PX = 216
INK = base.INK
WHITE = base.WHITE

# Only species whose line-angle representation becomes uninformative at this
# scale belong here. Larger molecules remain full RDKit skeletal structures.
# ASCII bond markers are intentional; the renderer converts them into actual
# drawn bonds, so they never appear as typographic '-'/'=' characters.
_CONDENSED_SMALL_ORGANICS = {
    "C=C": "CH2=CH2",          # ethene
    "CC": "CH3-CH3",           # ethane
    "CO": "CH3OH",             # methanol
    "C=O": "H2C=O",            # formaldehyde
    "CCO": "CH3CH2OH",         # ethanol
    "CC=O": "CH3CHO",          # acetaldehyde
    "CC(=O)O": "CH3COOH",      # acetic acid
    "C#N": "HCN",              # hydrogen cyanide
    "CC#N": "CH3CN",           # acetonitrile
}


def _canonical_text_table(source: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for smiles, label in source.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            out[Chem.MolToSmiles(mol)] = label
    return out


_CONDENSED_BY_CANONICAL_SMILES = _canonical_text_table(_CONDENSED_SMALL_ORGANICS)


def _condensed_for_display(smiles: str) -> str | None:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    return _CONDENSED_BY_CANONICAL_SMILES.get(Chem.MolToSmiles(mol))


def formula_for_display(smiles: str) -> str | None:
    """Prefer condensed structural text for tiny organics, then base rules."""
    condensed = _condensed_for_display(smiles)
    if condensed:
        return condensed
    return base.formula_for_display(smiles)


def split_reaction_side(text: str):
    return base.split_reaction_side(text)


def _molecule_png(smiles: str):
    return base._molecule_png(smiles, bond_px=DEFAULT_BOND_PX)


def _molecule_svg_fragment(smiles: str):
    return base._molecule_svg_fragment(smiles, bond_px=DEFAULT_BOND_PX)


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
            condensed = _condensed_for_display(smiles) if small_as_formula else None
            if condensed:
                tokens.append(("condensed", condensed))
                continue
            formula = base.formula_for_display(smiles) if small_as_formula else None
            tokens.append(("formula" if formula else "mol", formula or smiles))
    return tokens


_BOND_SPLIT_RE = re.compile(r"([=#-])")


def _split_condensed(text: str) -> list[tuple[str, str]]:
    """Return alternating ('group', text) / ('bond', symbol) elements."""
    parts = [p for p in _BOND_SPLIT_RE.split(text or "") if p]
    out: list[tuple[str, str]] = []
    for part in parts:
        out.append(("bond" if part in {"-", "=", "#"} else "group", part))
    return out


def _condensed_png(text: str, font_size: int = DEFAULT_FONT_PX) -> Image.Image:
    elements = _split_condensed(text)
    group_images: dict[int, Image.Image] = {}
    bond_width = max(34, int(font_size * 0.92))
    bond_pad = max(4, int(font_size * 0.10))
    total_w = 0
    max_h = 0

    for i, (kind, value) in enumerate(elements):
        if kind == "group":
            img = base._formula_png(value, font_size)
            group_images[i] = img
            total_w += img.width
            max_h = max(max_h, img.height)
        else:
            total_w += bond_width + 2 * bond_pad

    pad_x = max(8, font_size // 5)
    pad_y = max(8, font_size // 5)
    canvas = Image.new("RGB", (total_w + 2 * pad_x, max_h + 2 * pad_y), WHITE)
    draw = ImageDraw.Draw(canvas)
    center_y = canvas.height // 2
    x = pad_x

    for i, (kind, value) in enumerate(elements):
        if kind == "group":
            img = group_images[i]
            canvas.paste(img, (x, center_y - img.height // 2))
            x += img.width
            continue

        x1 = x + bond_pad
        x2 = x1 + bond_width
        line_w = 3
        if value == "-":
            draw.line((x1, center_y, x2, center_y), fill=INK, width=line_w)
        elif value == "=":
            offset = max(4, int(font_size * 0.11))
            draw.line((x1, center_y - offset, x2, center_y - offset), fill=INK, width=line_w)
            draw.line((x1, center_y + offset, x2, center_y + offset), fill=INK, width=line_w)
        else:  # triple bond
            offset = max(5, int(font_size * 0.13))
            draw.line((x1, center_y - offset, x2, center_y - offset), fill=INK, width=line_w)
            draw.line((x1, center_y, x2, center_y), fill=INK, width=line_w)
            draw.line((x1, center_y + offset, x2, center_y + offset), fill=INK, width=line_w)
        x += bond_width + 2 * bond_pad

    return base._trim(canvas, 4)


def _svg_group(text: str, x: int, y: int, font_size: int) -> str:
    return _svg_formula(text, x, y, font_size)


def _condensed_svg(text: str, x: int, cy: int, font_size: int = DEFAULT_FONT_PX) -> tuple[str, int, int]:
    elements = _split_condensed(text)
    bond_width = max(34, int(font_size * 0.92))
    bond_pad = max(4, int(font_size * 0.10))

    # Approximate text widths to match the SVG typography closely enough for
    # stable layout. Formula groups are deliberately conservative in width.
    def group_width(group: str) -> int:
        normal = sum(1 for c in group if not c.isdigit())
        subs = sum(1 for c in group if c.isdigit())
        return max(36, int(normal * font_size * 0.60 + subs * font_size * 0.42 + 8))

    widths: list[int] = []
    total = 0
    for kind, value in elements:
        w = group_width(value) if kind == "group" else bond_width + 2 * bond_pad
        widths.append(w)
        total += w

    out: list[str] = []
    px = x
    baseline = cy + int(font_size * 0.32)
    for (kind, value), w in zip(elements, widths):
        if kind == "group":
            out.append(_svg_group(value, px, baseline, font_size))
            px += w
            continue

        x1 = px + bond_pad
        x2 = x1 + bond_width
        if value == "-":
            out.append(f'<line x1="{x1}" y1="{cy}" x2="{x2}" y2="{cy}" stroke="#141414" stroke-width="3" stroke-linecap="round"/>')
        elif value == "=":
            offset = max(4, int(font_size * 0.11))
            out.append(f'<line x1="{x1}" y1="{cy-offset}" x2="{x2}" y2="{cy-offset}" stroke="#141414" stroke-width="3" stroke-linecap="round"/>')
            out.append(f'<line x1="{x1}" y1="{cy+offset}" x2="{x2}" y2="{cy+offset}" stroke="#141414" stroke-width="3" stroke-linecap="round"/>')
        else:
            offset = max(5, int(font_size * 0.13))
            out.append(f'<line x1="{x1}" y1="{cy-offset}" x2="{x2}" y2="{cy-offset}" stroke="#141414" stroke-width="3" stroke-linecap="round"/>')
            out.append(f'<line x1="{x1}" y1="{cy}" x2="{x2}" y2="{cy}" stroke="#141414" stroke-width="3" stroke-linecap="round"/>')
            out.append(f'<line x1="{x1}" y1="{cy+offset}" x2="{x2}" y2="{cy+offset}" stroke="#141414" stroke-width="3" stroke-linecap="round"/>')
        px += w

    return "".join(out), total, max(58, int(font_size * 1.45))


def render_reaction_png(
    reactant_pairs,
    product_pairs,
    *,
    small_as_formula: bool = True,
    arrow_top: str = "",
    arrow_bottom: str = "",
    dpi: int = RENDER_DPI,
) -> bytes | None:
    """Render a compact publication PNG with chemically drawn condensed bonds."""
    tokens = _build_tokens(reactant_pairs, product_pairs, small_as_formula)
    if not tokens:
        return None

    rendered: list[tuple[str, object, int, int]] = []
    font = base._load_font(DEFAULT_FONT_PX)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    ann_w = max(base._annotation_width(arrow_top), base._annotation_width(arrow_bottom))
    arrow_w = max(DEFAULT_ARROW_PX, ann_w + 72)

    for kind, payload in tokens:
        if kind == "mol":
            image = _molecule_png(str(payload))
            if image is None:
                return None
            rendered.append((kind, image, image.width, image.height))
        elif kind == "condensed":
            image = _condensed_png(str(payload), DEFAULT_FONT_PX)
            rendered.append((kind, image, image.width, image.height))
        elif kind == "formula":
            image = base._formula_png(str(payload), DEFAULT_FONT_PX)
            rendered.append((kind, image, image.width, image.height))
        elif kind in {"op", "coef"}:
            box = probe.textbbox((0, 0), str(payload), font=font)
            rendered.append((kind, payload, max(1, box[2] - box[0]), max(1, box[3] - box[1])))
        else:
            rendered.append((kind, None, arrow_w, 28))

    gap = DEFAULT_GAP_PX
    margin_x = 40
    label_room_top = 46 if arrow_top else 0
    label_room_bottom = 43 if arrow_bottom else 0
    chemistry_h = max((h for _, _, _, h in rendered), default=108)
    width = sum(w for _, _, w, _ in rendered) + gap * (len(rendered) - 1) + 2 * margin_x
    height = chemistry_h + label_room_top + label_room_bottom + 54

    canvas = Image.new("RGB", (max(1, width), max(1, height)), WHITE)
    draw = ImageDraw.Draw(canvas)
    center_y = label_room_top + 27 + chemistry_h // 2
    x = margin_x

    for kind, payload, w, h in rendered:
        if kind in {"mol", "formula", "condensed"}:
            image = payload
            canvas.paste(image, (x, center_y - image.height // 2))
        elif kind == "arrow":
            y = center_y
            head = 16
            draw.line((x, y, x + w - head, y), fill=INK, width=3)
            draw.polygon(
                [(x + w, y), (x + w - head, y - 7), (x + w - head, y + 7)],
                fill=INK,
            )
            cx = x + w // 2
            if arrow_top:
                base._draw_annotation(draw, cx, y - 16, arrow_top, font_size=29)
            if arrow_bottom:
                base._draw_annotation(draw, cx, y + 40, arrow_bottom, font_size=29)
        else:
            box = draw.textbbox((0, 0), str(payload), font=font)
            draw.text(
                (x - box[0], center_y - (box[3] - box[1]) // 2 - box[1]),
                str(payload),
                fill=INK,
                font=font,
            )
        x += w + gap

    out = io.BytesIO()
    canvas.save(out, format="PNG", dpi=(dpi, dpi), compress_level=6)
    return out.getvalue()


def _svg_formula(text: str, x: int, y: int, font_size: int = DEFAULT_FONT_PX) -> str:
    chunks = [
        f'<text x="{x}" y="{y}" font-family="Arial,Helvetica,sans-serif" '
        f'font-size="{font_size}" fill="#141414">'
    ]
    for token, style in base.formula_tokens(text):
        e = escape(token)
        if style == "sub":
            chunks.append(
                f'<tspan font-size="{int(font_size * 0.68)}" baseline-shift="sub">{e}</tspan>'
            )
        elif style == "sup":
            chunks.append(
                f'<tspan font-size="{int(font_size * 0.68)}" baseline-shift="super">{e}</tspan>'
            )
        else:
            chunks.append(e)
    chunks.append("</text>")
    return "".join(chunks)


def render_reaction_svg(
    reactant_pairs,
    product_pairs,
    *,
    small_as_formula: bool = True,
    arrow_top: str = "",
    arrow_bottom: str = "",
) -> bytes | None:
    """Render the compact layout as true vector SVG, including internal bonds."""
    tokens = _build_tokens(reactant_pairs, product_pairs, small_as_formula)
    if not tokens:
        return None

    items: list[tuple[str, object, int, int]] = []
    ann_w = max(len(arrow_top), len(arrow_bottom)) * 15
    arrow_w = max(DEFAULT_ARROW_PX, ann_w + 72)

    for kind, payload in tokens:
        if kind == "mol":
            frag = _molecule_svg_fragment(str(payload))
            if frag is None:
                return None
            items.append((kind, frag, frag[1], frag[2]))
        elif kind == "condensed":
            # placeholder dimensions; actual fragment is positioned once cy is known
            parts = _split_condensed(str(payload))
            approx_w = 0
            for pk, pv in parts:
                approx_w += max(36, int(len(pv) * DEFAULT_FONT_PX * 0.48 + 8)) if pk == "group" else max(34, int(DEFAULT_FONT_PX * 0.92)) + 2 * max(4, int(DEFAULT_FONT_PX * 0.10))
            items.append((kind, payload, approx_w, 62))
        elif kind == "formula":
            w = max(68, len(str(payload)) * 23 + 18)
            items.append((kind, payload, w, 58))
        elif kind in {"op", "coef"}:
            items.append((kind, payload, max(34, len(str(payload)) * 24), 52))
        else:
            items.append((kind, None, arrow_w, 28))

    gap = DEFAULT_GAP_PX
    margin = 40
    top = 54 if arrow_top else 18
    bottom = 50 if arrow_bottom else 22
    chemistry_h = max((h for _, _, _, h in items), default=108)
    width = sum(w for _, _, w, _ in items) + gap * (len(items) - 1) + 2 * margin
    height = top + chemistry_h + bottom
    cy = top + chemistry_h // 2

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#141414}</style>',
    ]

    x = margin
    for kind, payload, w, h in items:
        if kind == "mol":
            frag, _, _ = payload
            out.append(f'<g transform="translate({x},{cy - h // 2})">{frag}</g>')
        elif kind == "condensed":
            frag, _, _ = _condensed_svg(str(payload), x, cy, DEFAULT_FONT_PX)
            out.append(frag)
        elif kind == "formula":
            out.append(_svg_formula(str(payload), x + 5, cy + 14))
        elif kind in {"op", "coef"}:
            out.append(
                f'<text x="{x}" y="{cy + 14}" font-size="{DEFAULT_FONT_PX}" '
                f'font-weight="500">{escape(str(payload))}</text>'
            )
        else:
            end = x + w - 7
            head = end - 16
            out.append(
                f'<line x1="{x}" y1="{cy}" x2="{head}" y2="{cy}" '
                'stroke="#141414" stroke-width="3"/>'
            )
            out.append(
                f'<path d="M {end} {cy} L {head} {cy - 7} L {head} {cy + 7} Z" fill="#141414"/>'
            )
            cx = x + w // 2
            if arrow_top:
                out.append(base._svg_annotation(arrow_top, cx, cy - 16, font_size=29))
            if arrow_bottom:
                out.append(base._svg_annotation(arrow_bottom, cx, cy + 37, font_size=29))
        x += w + gap

    out.append("</svg>")
    return "".join(out).encode("utf-8")


def render_reaction_scheme(
    reactant_pairs,
    product_pairs,
    sub_size=None,
    small_as_formula: bool = True,
    arrow_top: str = "",
    arrow_bottom: str = "",
) -> bytes | None:
    return render_reaction_png(
        reactant_pairs,
        product_pairs,
        small_as_formula=small_as_formula,
        arrow_top=arrow_top,
        arrow_bottom=arrow_bottom,
    )
