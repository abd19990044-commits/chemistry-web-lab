# -*- coding: utf-8 -*-
"""Publication-Ready Reaction Thermodynamic Diagram Image Generator.

Generates high-resolution PNG summary diagrams containing:
- Reaction equation and rendered chemical structures
- Level of theory and computational conditions
- Core thermodynamic metrics (dG, dH, dS, dE_elec, dZPE, K_eq)
- Species-by-species energetic breakdown
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from PIL import Image, ImageDraw, ImageFont


def _get_font(size: int = 16, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        os.environ.get("CHEMISTRY_LAB_PDF_FONT", ""),
        os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def generate_reaction_thermo_diagram(
    reaction: Dict[str, Any],
    thermo: Dict[str, Any],
    rxn_image_bytes: Optional[bytes] = None,
    level_of_theory: Optional[str] = None,
) -> bytes:
    """Render a high-resolution PNG diagram of reaction thermodynamics."""
    width = 1280
    height = 960
    img = Image.new("RGB", (width, height), color="#f8fafc")
    draw = ImageDraw.Draw(img)

    f_title = _get_font(26, bold=True)
    f_sub = _get_font(15, bold=False)
    f_heading = _get_font(18, bold=True)
    f_card_label = _get_font(13, bold=False)
    f_card_val = _get_font(22, bold=True)
    f_card_sub = _get_font(13, bold=False)
    f_tbl_hdr = _get_font(13, bold=True)
    f_tbl_cell = _get_font(13, bold=False)

    # 1. Header Banner
    draw.rectangle([0, 0, width, 110], fill="#0f172a")
    draw.text((40, 25), "ORCA Web Lab - Reaction Thermochemistry Summary", fill="#ffffff", font=f_title)
    
    eq_str = reaction.get("equation") or reaction.get("display_name") or "Chemical Reaction"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    theory_str = level_of_theory or "DFT / Quantum Chemical Analysis"
    draw.text((40, 68), f"Equation: {eq_str}  |  Level of Theory: {theory_str}  |  {now_str}", fill="#94a3b8", font=f_sub)

    curr_y = 130

    # 2. Reaction 2D Drawing / Equation Card
    draw.rounded_rectangle([40, curr_y, width - 40, curr_y + 160], radius=8, fill="#ffffff", outline="#e2e8f0", width=1)
    draw.text((60, curr_y + 15), "Reaction Scheme & Stoichiometry", fill="#334155", font=f_heading)

    rxn_drawn = False
    if rxn_image_bytes:
        try:
            rxn_pil = Image.open(io.BytesIO(rxn_image_bytes))
            max_w, max_h = width - 120, 95
            rxn_pil.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
            paste_x = 40 + (width - 80 - rxn_pil.width) // 2
            paste_y = curr_y + 48
            if rxn_pil.mode in ("RGBA", "LA"):
                img.paste(rxn_pil, (paste_x, paste_y), rxn_pil)
            else:
                img.paste(rxn_pil, (paste_x, paste_y))
            rxn_drawn = True
        except Exception:
            rxn_drawn = False

    if not rxn_drawn:
        draw.text((60, curr_y + 65), eq_str, fill="#0f172a", font=_get_font(24, bold=True))

    curr_y += 180

    # 3. Core Thermodynamic Metrics Cards (6 tiles grid: 3x2)
    draw.text((40, curr_y), "Thermodynamic State Functions (T = 298.15 K, P = 1.0 atm)", fill="#0f172a", font=f_heading)
    curr_y += 30

    dG_kj = thermo.get("dG_kj_mol")
    if dG_kj is None:
        dG_kj = float(thermo.get("delta_G_hartree", 0) or 0) * 2625.4996
    dG_kcal = dG_kj / 4.184

    dH_kj = thermo.get("dH_kj_mol")
    if dH_kj is None:
        dH_kj = float(thermo.get("delta_H_hartree", 0) or 0) * 2625.4996
    dH_kcal = dH_kj / 4.184

    dS_si = thermo.get("dS_j_mol_k") or thermo.get("delta_S_j_mol_k", 0) or 0
    dS_cal = float(dS_si) / 4.184

    dE_kj = thermo.get("dE_elec_kj_mol")
    if dE_kj is None:
        dE_kj = float(thermo.get("delta_E_elec_hartree", 0) or 0) * 2625.4996

    dZPE_kj = thermo.get("dZPE_kj_mol")
    if dZPE_kj is None:
        dZPE_kj = float(thermo.get("delta_ZPE_hartree", 0) or 0) * 2625.4996
    
    k_val = thermo.get("k")
    log10_k = thermo.get("log10_k")
    if log10_k is not None:
        keq_str = f"log10 K = {float(log10_k):.2f}"
    elif k_val is not None:
        keq_str = f"{float(k_val):.3e}"
    else:
        keq_str = "-"

    tiles = [
        {
            "title": "Gibbs Free Energy (dG°)",
            "val": f"{dG_kcal:+.2f} kcal/mol",
            "sub": f"{dG_kj:+.2f} kJ/mol | {'Exergonic (Spontaneous)' if dG_kj < 0 else 'Endergonic (Non-spont.)'}",
            "color": "#0284c7",
            "bg": "#f0f9ff",
        },
        {
            "title": "Reaction Enthalpy (dH°)",
            "val": f"{dH_kcal:+.2f} kcal/mol",
            "sub": f"{dH_kj:+.2f} kJ/mol | {'Exothermic' if dH_kj < 0 else 'Endothermic'}",
            "color": "#0d9488",
            "bg": "#f0fdfa",
        },
        {
            "title": "Reaction Entropy (dS°)",
            "val": f"{dS_si:+.2f} J/(mol·K)",
            "sub": f"{dS_cal:+.2f} cal/(mol·K)",
            "color": "#6366f1",
            "bg": "#eef2ff",
        },
        {
            "title": "Equilibrium Constant (Keq)",
            "val": keq_str,
            "sub": "exp(-dG° / RT)",
            "color": "#8b5cf6",
            "bg": "#f5f3ff",
        },
        {
            "title": "Electronic Energy (dE_elec)",
            "val": f"{dE_kj:+.2f} kJ/mol",
            "sub": f"{dE_kj/4.184:+.2f} kcal/mol",
            "color": "#475569",
            "bg": "#f8fafc",
        },
        {
            "title": "Zero-Point Energy (dZPE)",
            "val": f"{dZPE_kj:+.2f} kJ/mol",
            "sub": f"{dZPE_kj/4.184:+.2f} kcal/mol",
            "color": "#475569",
            "bg": "#f8fafc",
        },
    ]

    card_w = (width - 80 - 40) // 3
    card_h = 100
    for idx, t in enumerate(tiles):
        row = idx // 3
        col = idx % 3
        bx = 40 + col * (card_w + 20)
        by = curr_y + row * (card_h + 15)

        draw.rounded_rectangle([bx, by, bx + card_w, by + card_h], radius=8, fill=t["bg"], outline="#cbd5e1", width=1)
        draw.text((bx + 16, by + 12), t["title"], fill="#475569", font=f_card_label)
        draw.text((bx + 16, by + 34), t["val"], fill=t["color"], font=f_card_val)
        draw.text((bx + 16, by + 68), t["sub"], fill="#64748b", font=f_card_sub)

    curr_y += 2 * (card_h + 15) + 20

    # 4. Species Breakdown Table
    draw.text((40, curr_y), "Species Energetic Breakdown", fill="#0f172a", font=f_heading)
    curr_y += 30

    table_top = curr_y
    draw.rounded_rectangle([40, table_top, width - 40, table_top + 210], radius=8, fill="#ffffff", outline="#e2e8f0", width=1)
    
    # Table Header Row
    draw.rectangle([40, table_top, width - 40, table_top + 38], fill="#f1f5f9")
    cols = [
        ("Role", 60),
        ("Coeff (nu)", 180),
        ("Species / Formula", 300),
        ("E_elec (Hartree)", 560),
        ("Enthalpy H (Eh)", 760),
        ("Gibbs Free Energy G (Eh)", 980),
    ]
    for col_name, cx in cols:
        draw.text((cx, table_top + 10), col_name, fill="#334155", font=f_tbl_hdr)

    # Table Data Rows
    row_y = table_top + 40
    species_list = reaction.get("species", [])
    for s in species_list[:6]:
        role = (s.get("role") or "reactant").capitalize()
        nu = s.get("nu") or s.get("stoichiometric_coefficient") or 1
        name = s.get("display_name") or s.get("formula") or "Unknown"
        
        fr = s.get("final_result") or {}
        e_el = f"{float(fr.get('E_final')):.6f}" if fr.get("E_final") is not None else "-"
        h_val = f"{float(fr.get('H_final')):.6f}" if fr.get("H_final") is not None else "-"
        g_val = f"{float(fr.get('G_final')):.6f}" if fr.get("G_final") is not None else "-"

        draw.line([40, row_y, width - 40, row_y], fill="#f1f5f9", width=1)
        draw.text((60, row_y + 6), role, fill="#0284c7" if role == "Reactant" else "#059669", font=f_tbl_cell)
        draw.text((180, row_y + 6), str(nu), fill="#1e293b", font=f_tbl_cell)
        draw.text((300, row_y + 6), name, fill="#1e293b", font=_get_font(13, bold=True))
        draw.text((560, row_y + 6), e_el, fill="#475569", font=f_tbl_cell)
        draw.text((760, row_y + 6), h_val, fill="#475569", font=f_tbl_cell)
        draw.text((980, row_y + 6), g_val, fill="#475569", font=f_tbl_cell)
        row_y += 28

    # 5. Footer
    draw.rectangle([0, height - 40, width, height], fill="#f1f5f9")
    draw.text((40, height - 28), "Generated by Chemistry Lab • ORCA Quantum Chemistry Engine • Automated Thermodynamic Workflow", fill="#64748b", font=f_sub)

    out_buf = io.BytesIO()
    img.save(out_buf, format="PNG", dpi=(300, 300))
    return out_buf.getvalue()
