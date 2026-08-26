from __future__ import annotations

"""Reaction-condition helpers and SVG renderer correction.

This renderer keeps conventional formulas visually proportional to RDKit-drawn
organic structures. Formula text, coefficients, plus signs and arrow conditions
share one typography scale instead of allowing small molecules such as H2 or O2
to dominate the equation.
"""

from fractions import Fraction
from xml.sax.saxutils import escape as _xml_escape

import chem_core as _core


def _svg_formula_spans(text: str, font_size: int) -> str:
    """Return SVG tspans with chemical subscripts/superscripts, stripping any ^ characters."""
    out = []
    for chunk, style in _core._formula_tokens(text):
        escaped = _xml_escape(chunk)
        if style == "sub":
            out.append(
                f'<tspan font-size="{int(font_size * 0.62)}" '
                f'baseline-shift="sub">{escaped}</tspan>'
            )
        elif style == "sup":
            out.append(
                f'<tspan font-size="{int(font_size * 0.62)}" '
                f'baseline-shift="super">{escaped}</tspan>'
            )
        else:
            out.append(escaped)
    return "".join(out)


def _annotation_markup(text: str, center_x: float, baseline_y: float, font_size: int) -> str:
    """Render one arrow annotation with chemical typography."""
    if not text:
        return ""
    pieces = []
    for part in str(text).split():
        pieces.append(_svg_formula_spans(part, font_size))
        pieces.append('<tspan> </tspan>')
    if pieces:
        pieces.pop()
    return (
        f'<text x="{center_x:.1f}" y="{baseline_y:.1f}" '
        f'font-family="Arial,Helvetica,sans-serif" font-size="{font_size}" '
        f'fill="#141414" text-anchor="middle">{"".join(pieces)}</text>'
    )


def _annotation_estimated_width(text: str, font_size: int) -> int:
    """Conservative width estimate used only to reserve arrow space."""
    if not text:
        return 0
    width = 0.0
    for part in str(text).split():
        for chunk, style in _core._formula_tokens(part):
            factor = 0.62 if style in ("sub", "sup") else 1.0
            width += len(chunk) * font_size * 0.56 * factor
        width += font_size * 0.28
    return int(width)


def _render_reaction_svg_fixed(reactant_pairs, product_pairs, small_as_formula: bool = True,
                               arrow_top: str = "", arrow_bottom: str = "") -> bytes | None:
    """SVG reaction renderer with proportional formulas, compact spacing, and conditions."""
    return _core.render_reaction_svg(
        reactant_pairs,
        product_pairs,
        small_as_formula=small_as_formula,
        arrow_top=arrow_top,
        arrow_bottom=arrow_bottom,
    )


def overlay_png(image_bytes: bytes, arrow_top: str = "", arrow_bottom: str = "") -> bytes:
    """Return the PNG renderer output unchanged."""
    return image_bytes


def overlay_svg(svg_bytes: bytes, arrow_top: str = "", arrow_bottom: str = "") -> bytes:
    """Return the SVG unchanged; the renderer owns the labels completely."""
    return svg_bytes
