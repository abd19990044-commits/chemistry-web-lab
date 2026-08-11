from pathlib import Path


def replace_once(text, old, new, label):
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f"Missing patch point: {label}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# chem_core.py: true vector SVG output
# ---------------------------------------------------------------------------
core_path = Path("chem_core.py")
s = core_path.read_text(encoding="utf-8")
if "def render_molecule_svg(" not in s:
    marker = "\n\n# ---------------------------------------------------------------------------\n# Small molecules are written, not drawn\n"
    code = r'''


def render_molecule_svg(smiles: str, legend: str = "", size=MOL_IMAGE_SIZE) -> bytes | None:
    """Return a genuine vector SVG molecular drawing for publication."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        AllChem.Compute2DCoords(mol)
        Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass
    drawer = rdMolDraw2D.MolDraw2DSVG(size[0], size[1])
    _apply_common_draw_options(drawer)
    opts = drawer.drawOptions()
    try:
        opts.fixedBondLength = 90.0
        opts.fixedFontSize = 60.0
        opts.minFontSize = 48.0
        opts.maxFontSize = 66.0
        opts.padding = 0.05
    except Exception:
        pass
    try:
        drawer.SetFontSize(60)
    except Exception:
        pass
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
    drawer.FinishDrawing()
    return drawer.GetDrawingText().encode("utf-8")


def _molecule_svg_fragment(smiles: str, width: int = 900, height: int = 720) -> str | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        AllChem.Compute2DCoords(mol)
        Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass
    drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
    _apply_common_draw_options(drawer)
    opts = drawer.drawOptions()
    try:
        opts.fixedBondLength = 70.0
        opts.fixedFontSize = 46.0
        opts.minFontSize = 34.0
        opts.maxFontSize = 52.0
        opts.padding = 0.08
    except Exception:
        pass
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    raw = drawer.GetDrawingText()
    start = raw.find(">") + 1
    end = raw.rfind("</svg>")
    return raw[start:end] if start > 0 and end > start else None


def _svg_formula_markup(formula: str, x: int, y: int, font_size: int = 88) -> str:
    parts = [f'<text x="{x}" y="{y}" font-family="Arial,Helvetica,sans-serif" font-size="{font_size}" fill="#141414">']
    for text, style in _formula_tokens(formula):
        if style == "sub":
            parts.append(f'<tspan font-size="{int(font_size * 0.62)}" baseline-shift="sub">{text}</tspan>')
        elif style == "sup":
            parts.append(f'<tspan font-size="{int(font_size * 0.62)}" baseline-shift="super">{text}</tspan>')
        else:
            parts.append(text)
    parts.append("</text>")
    return "".join(parts)


def render_reaction_svg(reactant_pairs, product_pairs, small_as_formula: bool = True) -> bytes | None:
    """Return a genuine vector SVG reaction scheme."""
    tokens = []
    for side_index, pairs in enumerate((reactant_pairs, product_pairs)):
        if side_index:
            tokens.append(("arrow", None))
        for i, (coefficient, smiles) in enumerate(pairs):
            if i:
                tokens.append(("op", "+"))
            formula = formula_for_display(smiles) if small_as_formula else None
            if formula:
                tokens.append(("formula", formula))
            else:
                fragment = _molecule_svg_fragment(smiles)
                if fragment is None:
                    return None
                tokens.append(("mol", fragment))
            label = ReactionTerm(Fraction(coefficient), "").pretty_coefficient
            if label:
                tokens.append(("coefficient", label))
    if not tokens:
        return None

    gap = 54
    mol_w, mol_h = 900, 720
    widths = []
    for kind, payload in tokens:
        if kind == "mol":
            widths.append(mol_w)
        elif kind == "formula":
            widths.append(max(250, 70 * len(payload) + 100))
        elif kind == "coefficient":
            widths.append(110)
        elif kind == "op":
            widths.append(90)
        else:
            widths.append(260)
    total_width = sum(widths) + gap * (len(tokens) - 1) + 120
    total_height = 900
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="{total_height}" viewBox="0 0 {total_width} {total_height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#141414}</style>',
    ]
    x = 60
    center = total_height // 2
    for i, (kind, payload) in enumerate(tokens):
        if kind == "mol":
            out.append(f'<g transform="translate({x},{center - mol_h // 2})">{payload}</g>')
        elif kind == "formula":
            out.append(_svg_formula_markup(payload, x + 35, center + 30, 88))
        elif kind == "coefficient":
            out.append(f'<text x="{x}" y="{center + 30}" font-size="76" font-weight="600">{payload}</text>')
        elif kind == "op":
            out.append(f'<text x="{x}" y="{center + 30}" font-size="82">+</text>')
        else:
            y = center
            out.append(f'<line x1="{x}" y1="{y}" x2="{x + 190}" y2="{y}" stroke="#141414" stroke-width="6"/>')
            out.append(f'<path d="M {x + 190} {y} L {x + 158} {y - 22} L {x + 158} {y + 22} Z" fill="#141414"/>')
        x += widths[i] + gap
    out.append("</svg>")
    return "".join(out).encode("utf-8")
'''
    if marker not in s:
        raise RuntimeError("Could not locate chem_core SVG insertion marker")
    s = s.replace(marker, code + marker, 1)
    core_path.write_text(s, encoding="utf-8")


# ---------------------------------------------------------------------------
# app.py: expose SVG alongside PNG
# ---------------------------------------------------------------------------
app_path = Path("app.py")
s = app_path.read_text(encoding="utf-8")
s = replace_once(
    s,
    '        mol_bytes = core.generate_mol_file_bytes(smiles)\n',
    '        mol_bytes = core.generate_mol_file_bytes(smiles)\n        svg_bytes = core.render_molecule_svg(smiles)\n',
    "molecule SVG generation",
)
s = replace_once(
    s,
    '            "image_png_base64": b64(image_bytes),\n            "mol_file_base64": b64(mol_bytes),\n',
    '            "image_png_base64": b64(image_bytes),\n            "image_svg_base64": b64(svg_bytes),\n            "mol_file_base64": b64(mol_bytes),\n',
    "molecule SVG payload",
)
s = replace_once(
    s,
    '        image_bytes = core.render_reaction_png(as_pairs(reactant_terms), as_pairs(product_terms),\n                                               small_as_formula=small_as_formula)\n',
    '        image_bytes = core.render_reaction_png(as_pairs(reactant_terms), as_pairs(product_terms),\n                                               small_as_formula=small_as_formula)\n        svg_bytes = core.render_reaction_svg(as_pairs(reactant_terms), as_pairs(product_terms),\n                                               small_as_formula=small_as_formula)\n',
    "reaction SVG generation",
)
# Restrict payload replacement to reaction section.
pos = s.index("def api_reaction")
head, tail = s[:pos], s[pos:]
tail = replace_once(
    tail,
    '            "image_png_base64": b64(image_bytes),\n            "rxn_file_base64": b64(rxn_bytes),\n',
    '            "image_png_base64": b64(image_bytes),\n            "image_svg_base64": b64(svg_bytes),\n            "rxn_file_base64": b64(rxn_bytes),\n',
    "reaction SVG payload",
)
app_path.write_text(head + tail, encoding="utf-8")


# ---------------------------------------------------------------------------
# Browser: add SVG download links
# ---------------------------------------------------------------------------
js_path = Path("static/js/app.js")
s = js_path.read_text(encoding="utf-8")
s = replace_once(
    s,
    'setDownload(document.getElementById("explorer-download-mol"), data.mol_file_base64, "chemical/x-mdl-molfile", `${data.filename}.mol`);',
    'setDownload(document.getElementById("explorer-download-mol"), data.mol_file_base64, "chemical/x-mdl-molfile", `${data.filename}.mol`);\n      setDownload(document.getElementById("explorer-download-svg"), data.image_svg_base64, "image/svg+xml", `${data.filename}.svg`);',
    "explorer SVG download",
)
s = replace_once(
    s,
    'setDownload(document.getElementById("reaction-download-rxn"), data.rxn_file_base64, "chemical/x-mdl-rxnfile", "reaction.rxn");',
    'setDownload(document.getElementById("reaction-download-rxn"), data.rxn_file_base64, "chemical/x-mdl-rxnfile", "reaction.rxn");\n      setDownload(document.getElementById("reaction-download-svg"), data.image_svg_base64, "image/svg+xml", "reaction.svg");',
    "reaction SVG download",
)
js_path.write_text(s, encoding="utf-8")

html_path = Path("templates/index.html")
s = html_path.read_text(encoding="utf-8")
s = replace_once(
    s,
    '<a id="explorer-download-mol" class="btn btn-ghost btn-small" download>⬇ Download MOL file</a>',
    '<a id="explorer-download-mol" class="btn btn-ghost btn-small" download>⬇ Download MOL file</a>\n          <a id="explorer-download-svg" class="btn btn-outline btn-small" download>⬇ Download SVG</a>',
    "explorer SVG button",
)
s = replace_once(
    s,
    '<a id="reaction-download-rxn" class="btn btn-ghost btn-small" download>⬇ Download RXN file</a>',
    '<a id="reaction-download-rxn" class="btn btn-ghost btn-small" download>⬇ Download RXN file</a>\n          <a id="reaction-download-svg" class="btn btn-outline btn-small" download>⬇ Download SVG</a>',
    "reaction SVG button",
)
html_path.write_text(s, encoding="utf-8")

print("Publication SVG patch applied successfully.")
