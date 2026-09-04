# -*- coding: utf-8 -*-
"""Thermodynamics PDF report service with a 48-hour secure retention policy.

- Server-generated from validated stored thermodynamic results only (AA).
- Reports stored under controlled app storage with random internal identities
  (report_<uuid>.pdf) - never user-controlled paths (AB1).
- Owner isolation: every report carries the owning identity; other owners get
  404 (AB2, consistent with the project security convention).
- Retention: created_at / expires_at persisted in UTC; expires = created + 48h.
  Cleanup runs opportunistically (on access, throttled to >=1/h) AND on
  explicit sweeps - expiry never depends on in-memory timers alone (AB4-AB6).
- Regeneration creates a NEW report id + new 48h window (AB7) and replaces the
  single active report per immutable thermodynamics result (AB8).
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

from services.reaction_workflow_service import HARTREE_KJ_MOL, utcnow_iso

RETENTION_HOURS = 48


class ReportNotFound(Exception):
    pass


class ReportExpired(Exception):
    pass


class ReportForbidden(Exception):
    pass


def _resolve_unicode_font():
    candidates = [
        os.environ.get("CHEMISTRY_LAB_PDF_FONT", ""),
        os.path.join(os.path.dirname(__file__), "..", "static", "fonts", "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return os.path.abspath(path)
    return None


def _fmt(v, unit="", digits=6):
    if v is None:
        return "n/a"
    return ("%.*f %s" % (digits, v, unit)).strip()


def generate_thermo_report_pdf(thermo: dict, reaction: dict, now_provider=None) -> tuple:
    """Builds the academic PDF report from VALIDATED stored results only."""
    from fpdf import FPDF

    now = (now_provider or (lambda: datetime.now(timezone.utc)))()
    font_path = _resolve_unicode_font()

    def _ascii(s):
        s = str(s)
        s = s.replace("\u0394", "Delta ").replace("\u00d7", "x").replace("\u2192", "->")
        s = s.replace("\u2212", "-").replace("\u2264", "<=").replace("\u2265", ">=")
        return s.encode("latin-1", "replace").decode("latin-1")

    class ReportPDF(FPDF):
        def footer(self):
            self.set_y(-15)
            self.set_font("helvetica", "I", 8)
            self.cell(0, 10, "Chemistry Lab - Reaction Thermodynamics Report - page %d" % self.page_no(),
                      align="C")

    pdf = ReportPDF(orientation="P", unit="mm", format="A4")
    pdf.set_compression(False)
    pdf.set_auto_page_break(auto=True, margin=18)
    base_font = "helvetica"  # ASCII scientific minimum; TTF/Unicode is a
    # documented future enhancement (font resolver retained above)
    pdf.set_text_shaping(False)
    pdf.add_page()

    def head(txt, size=13):
        pdf.set_font(base_font, "B", size)
        pdf.cell(0, 8, _ascii(txt), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    def row(label, value):
        pdf.set_font(base_font, "", 10)
        pdf.cell(70, 6, _ascii(label), border=0)
        pdf.multi_cell(0, 6, _ascii(value), new_x="LMARGIN", new_y="NEXT")

    def section(txt):
        pdf.ln(2)
        head(txt, 11)
        pdf.ln(0.5)

    head("Chemistry Lab")
    head("Reaction Thermodynamics Report")
    pdf.set_font(base_font, "I", 9)
    pdf.cell(0, 6, "Generated: %s (UTC)" % now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    section("Reaction")
    row("Equation:", thermo.get("equation", reaction.get("equation", "")))
    row("Reaction ID:", reaction.get("reaction_id", ""))
    bal = "BALANCED" if reaction.get("balance_valid") else "NOT BALANCED (see warnings)"
    row("Balance:", bal)

    section("Conditions")
    row("Temperature:", _fmt(thermo.get("temperature_k"), "K", 2))
    row("Standard state:", "ORCA / gas default (no silent correction applied)")
    solvents = sorted({str(p.get("solvent") or "gas phase") for p in thermo.get("species_provenance", [])})
    row("Solvent(s):", ", ".join(solvents))

    section("Species")
    cols = [("Species", 34), ("Role", 18), ("nu", 12), ("Method", 34), ("Basis", 30),
            ("Geometry source", 40), ("E_elec (Ha)", 22), ("H (Ha)", 22), ("G (Ha)", 22), ("Imag", 12)]
    widths = []
    max_w = 190.0
    scale = min(1.0, max_w / sum(c[1] for c in cols))
    for name, w in cols:
        widths.append(w * scale)
    pdf.set_font(base_font, "B", 8)
    for (name, _w), w in zip(cols, widths):
        pdf.cell(w, 6, _ascii(name), border=1)
    pdf.ln()
    pdf.set_font(base_font, "", 8)
    for prov in thermo.get("species_provenance", []):
        fr_map = {sp["display_name"]: sp for sp in reaction.get("species", [])}
        sp = fr_map.get(prov.get("display_name"), {})
        fr = sp.get("final_result") or {}
        vals = [prov.get("display_name"), prov.get("nu") and ("+" if prov["nu"] > 0 else "") + str(abs(prov["nu"])),
                sp.get("charge", ""), sp.get("final_result", {}).get("method") or prov.get("method") or "",
                prov.get("basis_set") or "", prov.get("geometry_source") or "",
                _fmt(fr.get("E_final"), "", 6), _fmt(fr.get("H_final"), "", 6), _fmt(fr.get("G_final"), "", 6),
                str(sp.get("final_result", {}).get("imaginary_count", 0))]
        for v, w in zip(vals, widths):
            pdf.cell(w, 6, _ascii(str(v))[:int(w / 1.6)], border=1)
        pdf.ln()

    section("Reaction Thermodynamics")
    row("\u0394E_elec:", "%s Ha   =   %s kJ/mol" % (_fmt(thermo.get("dE_elec_hartree"), "", 6),
                                                   _fmt(thermo.get("dE_elec_kj_mol"), "", 3)))
    row("\u0394ZPE:", "%s Ha   =   %s kJ/mol" % (_fmt(thermo.get("dZPE_hartree"), "", 6),
                                                 _fmt(thermo.get("dZPE_kj_mol"), "", 3)))
    row("\u0394H_rxn:", "%s Ha   =   %s kJ/mol" % (_fmt(thermo.get("dH_hartree"), "", 6),
                                                   _fmt(thermo.get("dH_kj_mol"), "", 3)))
    row("\u0394S_rxn:", _fmt(thermo.get("dS_j_mol_k"), "J mol^-1 K^-1", 3))
    row("\u0394G_rxn:", "%s Ha   =   %s kJ/mol" % (_fmt(thermo.get("dG_hartree"), "", 6),
                                                   _fmt(thermo.get("dG_kj_mol"), "", 3)))
    ln_k = thermo.get("ln_k")
    log10_k = thermo.get("log10_k")
    row("ln K:", _fmt(ln_k, "", 4))
    row("log10 K:", _fmt(log10_k, "", 4))
    k = thermo.get("k")
    if k is None:
        row("K:", "n/a")
    elif k == float("inf"):
        row("K:", "K > 1e300 (see log10 K)")
    elif k == 0.0:
        row("K:", "K < 1e-300 (see log10 K)")
    else:
        row("K:", _fmt(k, "", 6))
    row("K formula:", "ln K = -\u0394G / (R T),  \u0394G converted to J/mol; R = 8.314462618 J mol^-1 K^-1")

    section("Composite Method (if applicable)")
    composites = sorted({p.get("equation_G") for p in thermo.get("species_provenance", [])
                         if p.get("equation_G") and "(direct)" not in str(p.get("equation_G"))})
    if composites:
        for eqn in composites:
            row("Equation:", eqn)
        row("H formula:", "H_final = E_SP + (H_freq - E_freq)")
    else:
        row("Composite:", "Not applicable - direct thermodynamics from the frequency stage.")

    section("Validation / Warnings")
    warnings = list(thermo.get("warnings") or [])
    warnings += list(reaction.get("balance_warnings") or [])
    if warnings:
        for w in warnings:
            pdf.set_font(base_font, "", 9)
            pdf.multi_cell(0, 5, "- " + _ascii(w), new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font(base_font, "", 9)
        pdf.cell(0, 5, "No scientific warnings.", new_x="LMARGIN", new_y="NEXT")

    section("Provenance")
    row("Result ID:", thermo.get("result_id", ""))
    row("Computed at:", thermo.get("computed_at", ""))
    for prov in thermo.get("species_provenance", []):
        row(prov.get("display_name", ""),
            "geom: %s | elec: %s | thermal: %s | G: %s"
            % (prov.get("geometry_source"), prov.get("electronic_source"),
               prov.get("thermal_source"), prov.get("equation_G")))
    row("Chemistry Lab version:", "1.0.3")

    pdf_bytes = bytes(pdf.output())
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    return pdf_bytes, sha


def create_report(store, owner: str, reaction: dict, thermo: dict, now_provider=None) -> dict:
    now = (now_provider or store.now_provider)()
    created = now.astimezone(timezone.utc)
    expires = created + timedelta(hours=RETENTION_HOURS)
    pdf_bytes, sha = generate_thermo_report_pdf(thermo, reaction, now_provider=now_provider)
    report_id = uuid.uuid4().hex
    stored_id = "report_%s.pdf" % uuid.uuid4().hex
    path = os.path.join(store.reports_dir, stored_id)
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    meta = {
        "report_id": report_id,
        "result_id": thermo.get("result_id"),
        "reaction_id": reaction.get("reaction_id"),
        "owner": owner,
        "stored_report_id": stored_id,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "sha256": sha,
        "size": len(pdf_bytes),
    }
    store.replace_active_report(meta)
    return meta


def get_downloadable_report(store, owner: str, report_id: str, now_provider=None):
    now = (now_provider or store.now_provider)()
    store.cleanup_if_due(now)
    meta = store.get_report(report_id)
    if meta is None or (owner is not None and meta.get("owner") != owner):
        raise ReportNotFound("report not found")
    expires = datetime.fromisoformat(meta["expires_at"])
    if now >= expires:
        raise ReportExpired("REPORT_EXPIRED")
    path = os.path.join(store.reports_dir, meta["stored_report_id"])
    if not os.path.exists(path):
        raise ReportNotFound("report file missing")
    with open(path, "rb") as f:
        return meta, f.read()
