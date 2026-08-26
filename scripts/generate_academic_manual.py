# -*- coding: utf-8 -*-
"""
Comprehensive Academic User Manual & Technical Reference Guide Generator (.docx)
for Chemistry Lab: Computational Chemistry Platform.
Author: ABDULSALAM S. HASAN (Mosul, Iraq)
Version: 1.0 (Comprehensive Academic Edition, August 2026)
"""

import os
import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn


def set_cell_background(cell, fill_hex):
    """Sets background color of a table cell."""
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    tc_pr.append(shd)


def set_cell_margins(cell, top=100, bottom=100, left=150, right=150):
    """Sets inner padding for a table cell in dxa (1 pt = 20 dxa)."""
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tc_pr.append(tc_mar)


def create_manual(output_path: str):
    doc = docx.Document()

    # ─────────────────────────────────────────────────────────────
    # Page Setup & Margins (1 inch all around)
    # ─────────────────────────────────────────────────────────────
    for section in doc.sections:
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)
        section.header_distance = Inches(0.5)
        section.footer_distance = Inches(0.5)

        # Header
        header = section.header
        hp = header.paragraphs[0]
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        hrun = hp.add_run("Chemistry Lab - Academic User Manual & Technical Reference Guide")
        hrun.font.name = "Calibri"
        hrun.font.size = Pt(8.5)
        hrun.font.color.rgb = RGBColor(110, 110, 110)

        # Footer
        footer = section.footer
        fp = footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        frun = fp.add_run("Chemistry Lab • Developed by ABDULSALAM S. HASAN • Compatible with ORCA 6.1")
        frun.font.name = "Calibri"
        frun.font.size = Pt(8.5)
        frun.font.color.rgb = RGBColor(110, 110, 110)

    # ─────────────────────────────────────────────────────────────
    # Typography & Color Palette
    # ─────────────────────────────────────────────────────────────
    NAVY = RGBColor(11, 44, 82)        # Primary Academic Navy (#0B2C52)
    TEAL = RGBColor(15, 118, 110)      # Scientific Teal (#0F766E)
    INDIGO = RGBColor(67, 56, 202)     # Indigo Subsections (#4338CA)
    DARK = RGBColor(30, 41, 59)        # Slate Text (#1E293B)
    MUTED = RGBColor(100, 116, 139)    # Muted Metadata (#64748B)

    # ─────────────────────────────────────────────────────────────
    # Formatting Helpers
    # ─────────────────────────────────────────────────────────────
    def add_heading_1(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(18)
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.font.name = "Calibri"
        run.font.size = Pt(15.5)
        run.font.bold = True
        run.font.color.rgb = NAVY
        return p

    def add_heading_2(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(14)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.font.name = "Calibri"
        run.font.size = Pt(12.5)
        run.font.bold = True
        run.font.color.rgb = TEAL
        return p

    def add_heading_3(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.font.name = "Calibri"
        run.font.size = Pt(11.0)
        run.font.bold = True
        run.font.color.rgb = INDIGO
        return p

    def add_body(text, bold_prefix=None, space_after=5):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(space_after)
        p.paragraph_format.line_spacing = 1.15
        if bold_prefix:
            br = p.add_run(bold_prefix)
            br.font.name = "Calibri"
            br.font.size = Pt(10.0)
            br.font.bold = True
            br.font.color.rgb = DARK
        r = p.add_run(text)
        r.font.name = "Calibri"
        r.font.size = Pt(10.0)
        r.font.color.rgb = DARK
        return p

    def add_bullet(text, bold_prefix=None):
        p = doc.add_paragraph(style='List Bullet')
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.line_spacing = 1.15
        if bold_prefix:
            br = p.add_run(bold_prefix)
            br.font.name = "Calibri"
            br.font.size = Pt(10.0)
            br.font.bold = True
            br.font.color.rgb = DARK
        r = p.add_run(text)
        r.font.name = "Calibri"
        r.font.size = Pt(10.0)
        r.font.color.rgb = DARK
        return p

    def add_code_box(code_text):
        table = doc.add_table(rows=1, cols=1)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        cell = table.cell(0, 0)
        cell.width = Inches(6.5)
        set_cell_background(cell, "F8FAFC")
        set_cell_margins(cell, 100, 100, 140, 140)
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(code_text)
        run.font.name = "Consolas"
        run.font.size = Pt(8.8)
        run.font.color.rgb = RGBColor(15, 23, 42)
        doc.add_paragraph().paragraph_format.space_after = Pt(3)

    def add_callout(text, title="NOTE / BEST PRACTICE", bg_hex="F0FDF4", border_color="16A34A"):
        table = doc.add_table(rows=1, cols=1)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        cell = table.cell(0, 0)
        cell.width = Inches(6.5)
        set_cell_background(cell, bg_hex)
        set_cell_margins(cell, 100, 100, 140, 140)
        p = cell.paragraphs[0]
        p.paragraph_format.space_after = Pt(2)
        tr = p.add_run(f"📌 {title}: ")
        tr.bold = True
        tr.font.name = "Calibri"
        tr.font.size = Pt(9.5)
        tr.font.color.rgb = NAVY
        br = p.add_run(text)
        br.font.name = "Calibri"
        br.font.size = Pt(9.5)
        br.font.color.rgb = DARK
        doc.add_paragraph().paragraph_format.space_after = Pt(3)

    # ─────────────────────────────────────────────────────────────
    # TITLE & METADATA CARD
    # ─────────────────────────────────────────────────────────────
    title_p = doc.add_paragraph()
    title_p.paragraph_format.space_before = Pt(0)
    title_p.paragraph_format.space_after = Pt(4)
    title_run = title_p.add_run("Chemistry Lab: Computational Chemistry Suite")
    title_run.font.name = "Calibri"
    title_run.font.size = Pt(22)
    title_run.font.bold = True
    title_run.font.color.rgb = NAVY

    subtitle_p = doc.add_paragraph()
    subtitle_p.paragraph_format.space_after = Pt(12)
    sub_run = subtitle_p.add_run("Comprehensive Academic User Manual, Theoretical Foundations & Technical Reference Guide")
    sub_run.font.name = "Calibri"
    sub_run.font.size = Pt(12)
    sub_run.font.italic = True
    sub_run.font.color.rgb = TEAL

    meta_table = doc.add_table(rows=1, cols=1)
    meta_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    meta_cell = meta_table.cell(0, 0)
    meta_cell.width = Inches(6.5)
    set_cell_background(meta_cell, "F1F5F9")
    set_cell_margins(meta_cell, 120, 120, 160, 160)

    mp = meta_cell.paragraphs[0]
    mp.paragraph_format.space_after = Pt(2)
    mp.paragraph_format.line_spacing = 1.2

    runs_data = [
        ("Principal Author & Developer: ", True, NAVY),
        ("ABDULSALAM S. HASAN (Mosul, Iraq)\n", False, DARK),
        ("Academic Target Software: ", True, NAVY),
        ("ORCA 6.1 / 6.0 Program Package (FACCTs GmbH / Max Planck Institute)\n", False, DARK),
        ("Platform Architecture: ", True, NAVY),
        ("Three Unified Studios (Draw Chemistry • ORCA Calculations & Jobs • Quantum Engine & Thermochemistry)\n", False, DARK),
        ("License & Access: ", True, NAVY),
        ("Academic and Non-Commercial Research Suite (ORCA Web Lab License v1.1) • Zero Server Retention Policy\n", False, DARK),
        ("Manual Release: ", True, NAVY),
        ("Version 1.0 (Comprehensive Academic Edition) | Last Updated: August 2026", False, DARK),
    ]
    for text, bold, col in runs_data:
        r = mp.add_run(text)
        r.font.name = "Calibri"
        r.font.size = Pt(9.2)
        r.bold = bold
        r.font.color.rgb = col

    doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # ─────────────────────────────────────────────────────────────
    # SECTION 1: SYSTEM ARCHITECTURE & THE THREE STUDIOS
    # ─────────────────────────────────────────────────────────────
    add_heading_1("1. Platform Overview & The Three Unified Studios")
    add_body(
        "Chemistry Lab is a full-stack, cloud-orchestrated computational chemistry suite developed to bridge the gap between "
        "advanced ab initio quantum chemical software (ORCA 6.1) and intuitive, publication-grade web exploration. The platform "
        "eliminates traditional command-line configuration hurdles while maintaining strict physical rigor, standard spectroscopic "
        "conventions, and reproducible quantum mechanical workflows."
    )
    add_body(
        "The system is organized around Three Unified Studios accessible from the central navigation hub:",
        bold_prefix="The Three Unified Studios Model: "
    )
    add_bullet(
        " Interactive chemical structure sketcher, automated SMILES/InChI/IUPAC compound resolver via PubChem REST APIs, "
        "3D conformer generation via Universal Force Field (UFF/MMFF94) minimization, collision-free multi-fragment offset positioning, "
        "and multi-component reaction balancing with vector SVG / MDL RXN V2000 export.",
        bold_prefix="Studio 1: Draw Chemistry (Chemoinformatics & Reaction Engineering):"
    )
    add_bullet(
        " Sequential input generator for ORCA 6.1 keywords and blocks, multi-step chained calculation workflows "
        "(e.g., Opt -> High-Opt -> Freq -> SP), high-precision coordinate inheritance, and automated cloud HPC execution "
        "via user-owned Kaggle accounts with a 5-job concurrency governor and session watchdog auto-recovery.",
        bold_prefix="Studio 2: ORCA Calculations & Jobs (Generator & Cloud HPC Orchestrator):"
    )
    add_bullet(
        " High-throughput output log parsing, interactive 3D WebGL molecular geometry rendering, Mulliken/Löwdin/Hirshfeld partial charge mapping, "
        "HOMO-LUMO frontier orbital gaps, UV-Vis electronic absorption convolution with experimental overlays, FTIR vibrational spectra with "
        "transmittance/absorbance toggles, Conceptual DFT (CDFT) reactivity descriptors, Grimme's Quasi-RRHO thermochemistry, and Bond Dissociation Energies (BDE).",
        bold_prefix="Studio 3: Quantum Engine & Thermochemistry (Spectroscopy & Scientific Audit):"
    )
    add_callout(
        "Chemistry Lab operates under a strict Zero-Retention Privacy Policy. All Kaggle API credentials, inputs, coordinates, and "
        "computation results remain in transient browser storage (localStorage/sessionStorage). No user data or credentials are "
        "stored or monetized on any server database.",
        title="PRIVACY & EPHEMERAL ARCHITECTURE"
    )

    # ─────────────────────────────────────────────────────────────
    # SECTION 2: STUDIO 1 - CHEMOINFORMATICS & 2D/3D DRAWING
    # ─────────────────────────────────────────────────────────────
    add_heading_1("2. Studio 1: Chemoinformatics, 2D Structure Drawing & Reaction Schemes")
    add_body(
        "Studio 1 provides a seamless environment for preparing chemical structures and reaction equations before submission "
        "to quantum calculations. It is powered by RDKit backend libraries and an interactive vector canvas."
    )

    add_heading_2("2.1 Molecule Explorer & Chemical Identifier Translation")
    add_bullet("Translates IUPAC names, common names, isomeric SMILES, and InChI/InChIKey identifiers into chemical graphs.")
    add_bullet("Fetches live structural and physical property data from PubChem, including Molecular Weight (MW), Exact Mass, XLogP3, Topological Polar Surface Area (TPSA), Formal Charge, Heavy Atom Count, and H-Bond Donors/Acceptors.")
    add_bullet("Instant 2D-to-3D embedding using distance geometry algorithms with MMFF94 force-field relaxation.")

    add_heading_2("2.2 2D Drawing Canvas & Multi-Theme Publication Graphics")
    add_bullet("Draw bonds, rings (benzene, cyclohexane, cyclopentane), heteroatoms, stereochemical wedges, and charges with clean snap-to-grid alignment.")
    add_bullet("Export vector graphics (SVG) with customizable themes: Pure White (print publishing), Dark Slate (presentations), and Transparent (compositing).")
    add_bullet("Direct export to MDL Molfile V2000, SD file (.sdf), and Daylight SMILES compatible with ChemDraw, MarvinSketch, and Reaxys.")

    add_heading_2("2.3 Collision-Free Multi-Fragment Positioning & Force Field Relaxation")
    add_body(
        "When assembling bimolecular complexes, solute-solvent clusters, or catalyst-substrate adducts, the system computes "
        "the Van der Waals bounding envelopes and applies an iterative collision-free offset along the X/Y axes (minimum clearance >= 2.8 Å). "
        "A Universal Force Field (UFF) pre-relaxation step eliminates unphysical steric clashes prior to ab initio optimization."
    )

    add_heading_2("2.4 Multi-Component Reaction Schemes & Stoichiometric Balancing")
    add_bullet("Formulate multi-component chemical reactions (e.g., '2 C6H6 + 15 O2 -> 12 CO2 + 6 H2O').")
    add_bullet("Automated algebraic mass and atom conservation verification ensuring every element and charge balances across reactants and products.")
    add_bullet("Export publication-quality reaction schemes in MDL RXN V2000 format.")

    # ─────────────────────────────────────────────────────────────
    # SECTION 3: STUDIO 2 - ORCA 6.1 INPUT GENERATOR WIZARD
    # ─────────────────────────────────────────────────────────────
    add_heading_1("3. Studio 2: ORCA 6.1 Advanced Input Generation Wizard")
    add_body(
        "The ORCA 6.1 Input Generator follows a validated, multi-step sequential wizard model. It guarantees that generated "
        "ORCA input files (.inp) strictly obey ORCA syntax without syntax errors, unrecognized keywords, or silent parameter regressions."
    )

    add_heading_2("3.1 Theoretical Methods & Functional Taxonomy")
    add_bullet(
        " r2SCAN-3c, B97-3c, PBEh-3c, HF-3c, wB97X-3c. These composite approaches evaluate geometry optimizations and vibrational "
        "frequencies at high speed while correcting for basis set superposition error (gCP) and dispersion interactions (D3/D4).",
        bold_prefix="Composite Density Functional Theory (3c Methods): "
    )
    add_bullet(
        " B3LYP, PBE0, TPSSh, PW6B95, M06-2X (standard hybrids); CAM-B3LYP, wB97X-D4, wB97M-V, wB97X-V (range-separated hybrids); "
        "PBE, BP86, BLYP, r2SCAN, TPSS (pure GGA and meta-GGA).",
        bold_prefix="Standard & Range-Separated Hybrids (DFT): "
    )
    add_bullet(
        " B2PLYP, DSD-PBEP86, DSD-BLYP. Automatically generates the appropriate auxiliary basis sets (RI-JK and AutoAux).",
        bold_prefix="Double-Hybrid Functionals: "
    )
    add_bullet(
        " Canonical MP2, RI-MP2, DLPNO-MP2, CCSD, CCSD(T), and DLPNO-CCSD(T). Enables gold-standard wavefunction benchmark calculations.",
        bold_prefix="Correlated Post-Hartree-Fock Methods: "
    )

    add_heading_2("3.2 Systematic Basis Set Selection")
    add_bullet("Karlsruhe segmented def2 family: def2-SVP, def2-SVPD, def2-TZVP, def2-TZVPD, def2-TZVPP, def2-QZVPP, ma-def2-SVP, ma-def2-TZVP.")
    add_bullet("Jensen Polarization-Consistent family: pcseg-1, pcseg-2, pcseg-3, aug-pcseg-1, aug-pcseg-2 (optimized for DFT energy convergence).")
    add_bullet("Dunning Correlation-Consistent family: cc-pVDZ, aug-cc-pVDZ, cc-pVTZ, aug-cc-pVTZ, cc-pVQZ (for correlated wavefunction extrapolation).")
    add_bullet("Pople 6-31G family: 6-31G(d), 6-31+G(d,p), 6-311G(d,p), 6-311++G(2df,2pd).")
    add_bullet("Relativistic Recontracted Sets: x2c-SVPall, x2c-TZVPall, x2c-TZVPPall, SARC-ZORA-TZVP for heavy transition metals and actinides.")
    add_bullet("Magnetic & Property Sets: IGLO-III, pcJ-2, EPR-II, EPR-III for NMR chemical shielding tensors and EPR hyperfine couplings.")

    add_heading_2("3.3 Relativistic Hamiltonians & Solvation Models")
    add_bullet("Scalar Relativistic Hamiltonians: Exact Two-Component (X2C), Zeroth-Order Regular Approximation (ZORA), and Douglas-Kroll-Hess (DKH).")
    add_bullet("Implicit Solvation Models: Conductor-like Polarizable Continuum Model (CPCM) and Solvation Model based on Density (SMD).")
    add_bullet("19 Parameter-Verified Solvents: Water, Acetonitrile, Methanol, Ethanol, Acetone, DCM, Chloroform, THF, DMSO, DMF, Toluene, Benzene, Hexane, Cyclohexane, DiethylEther, 1,4-Dioxane, CarbonTetrachloride, EthylAcetate, Pyridine.")

    add_heading_2("3.4 Hardware Resource Management & Safety Budgeting")
    add_bullet("Parallel Core Scaling: Configurable `%pal nprocs N end` for multi-threaded execution.")
    add_bullet("MaxCore RAM Clamping: `%maxcore M` dynamically clamped to Kaggle's safe memory threshold (6000 MB per core) to prevent kernel out-of-memory crashes.")
    add_bullet("Scratch Disk Safety: `%maxdisk` safety guardrails preventing disk saturation.")

    # ─────────────────────────────────────────────────────────────
    # SECTION 4: MULTI-STAGE WORKFLOWS & CLOUD ORCHESTRATION
    # ─────────────────────────────────────────────────────────────
    add_heading_1("4. Studio 2: Multi-Stage Chained Workflows & Cloud Orchestration")
    add_body(
        "A flagship capability of Chemistry Lab is the automated execution of chained multi-step computational workflows "
        "across remote Kaggle GPU/CPU instances with full fault tolerance and lossless coordinate inheritance."
    )

    add_heading_2("4.1 Multi-Stage Workflow Automation Pipeline")
    add_body(
        "Typical high-level quantum chemistry protocols require multi-tier calculation sequences. For example:",
        bold_prefix="Standard 4-Stage Protocol: "
    )
    add_bullet(" Stage 1: Low-level rapid structural pre-optimization (e.g., ! B97-3c Opt).")
    add_bullet(" Stage 2: High-level DFT geometry optimization (e.g., ! wB97X-D4 def2-TZVP Opt).")
    add_bullet(" Stage 3: Harmonic vibrational frequency analysis & thermochemistry (e.g., ! wB97X-D4 def2-TZVP Freq).")
    add_bullet(" Stage 4: High-accuracy single-point electronic energy (e.g., ! DLPNO-CCSD(T) def2-QZVPP SP).")

    add_heading_2("4.2 The Latest-OPT Stage Coordinate Inheritance Algorithm")
    add_body(
        "In traditional workflow managers, Stage N blindly requests coordinates from Stage N-1. However, in scientific protocols, "
        "Stage 3 (Freq) or Stage 4 (SP) does not perform geometry optimization and does not yield a new `.opt.xyz` coordinate file. "
        "If Stage 4 attempts to extract coordinates from Stage 3 (Freq), the job chain would fail."
    )
    add_body(
        "Chemistry Lab implements the Latest-OPT Stage Inheritance Algorithm (getWorkflowOptimizedCoords):",
        bold_prefix="Algorithmic Solution: "
    )
    add_bullet("When Stage N is promoted from the waiting dependency queue, the system inspects all predecessor stages (Stage N-1 down to Stage 1).")
    add_bullet("It identifies the most recent predecessor stage that performed geometry optimization (Opt, OptTS) or has cached optimized coordinates.")
    add_bullet("It extracts the optimized Cartesian coordinates from that Opt stage and injects them directly into Stage N's input file.")
    add_bullet("The extracted coordinates are cached in local browser state, allowing instant re-use across all subsequent stages without network overhead.")

    add_heading_2("4.3 Lossless Full Decimal Precision Preservation (10+ Decimals)")
    add_body(
        "Standard web tools round coordinates to 6 decimal places (`12.6f`), causing artificial structural strain and gradient spikes "
        "in subsequent high-level coupled-cluster or tight SCF runs. Chemistry Lab eliminates all coordinate truncation: Cartesian tokens "
        "are extracted verbatim from ORCA logs and `.xyz` files with 10 to 14 decimal places (e.g., `0.117289123456 Å`), preserving "
        "complete quantum geometric fidelity across the entire chain."
    )

    add_heading_2("4.4 Kaggle Cloud HPC Execution, Queue Governor & Auto-Recovery")
    add_bullet("Queue Governor: Limits concurrent active Kaggle jobs to 5 slots, automatically dispatching queued stages as active jobs finish.")
    add_bullet("Watchdog Auto-Recovery: Automatically handles long-running jobs approaching Kaggle's 12-hour session limit, re-launching from the latest converged `.opt.xyz` geometry.")
    add_bullet("Fast Essential Mode vs. Full Mode: Allows researchers on slow or metered internet connections to download essential outputs (.out, .xyz, .property.txt, .inp < 1 MB) instantly, while filtering out multi-gigabyte `.gbw` wavefunctions.")
    add_bullet("Native File System Access: Direct browser file streaming bypassing third-party download interceptors (IDM).")

    # ─────────────────────────────────────────────────────────────
    # SECTION 5: STUDIO 3 - QUANTUM ENGINE, SPECTROSCOPY & THERMOCHEMISTRY
    # ─────────────────────────────────────────────────────────────
    add_heading_1("5. Studio 3: Quantum Engine, Spectroscopy & Thermochemical Audit")
    add_body(
        "Studio 3 delivers a state-of-the-art post-processing environment for analyzing ORCA output files (.out, .log, and .zip archives). "
        "It provides real-time streaming log parsing, 3D visualization, spectral convolution, and multi-component reaction thermodynamics."
    )

    add_heading_2("5.1 3D Molecular Geometry & Orbital Visualizer")
    add_bullet("High-performance WebGL 3D molecular canvas (3Dmol.js) with Ball-and-Stick, Wireframe, and Space-Filling (VDW) modes.")
    add_bullet("Atomic Partial Charges: Instant extraction and color mapping of Mulliken, Löwdin, and CHELPG charges.")
    add_bullet("MultiWfn .crg Integration: Direct upload of Hirshfeld and ADCH charge files (.crg) produced by Multiwfn to render electrostatic potential (ESP) surfaces.")

    add_heading_2("5.2 Frontier Molecular Orbitals & Conceptual DFT (CDFT)")
    add_body(
        "Extracts spatial energy eigenvalues for Highest Occupied (HOMO) and Lowest Unoccupied (LUMO) molecular orbitals, "
        "and computes global reactivity descriptors grounded in conceptual density functional theory:"
    )
    add_bullet(" Energy difference ΔEgap = |ELUMO - EHOMO| in eV and kcal/mol.", bold_prefix="HOMO-LUMO Bandgap: ")
    add_bullet(" Approximate Ionization Potential IP ≈ -EHOMO and Electron Affinity EA ≈ -ELUMO (Koopmans/Janak theorem).", bold_prefix="IP & EA: ")
    add_bullet(" Global Hardness η = (IP - EA)/2 and Global Softness S = 1/(2η).", bold_prefix="Chemical Hardness (η) & Softness (S): ")
    add_bullet(" Electronic Chemical Potential μ = -(IP + EA)/2 and Electronegativity χ = -μ.", bold_prefix="Chemical Potential (μ) & Electronegativity (χ): ")
    add_bullet(" Electrophilicity Index ω = μ² / (2η), measuring electrophilic stabilization capacity.", bold_prefix="Electrophilicity Index (ω): ")
    add_bullet(" Nucleophilicity Index N = EHOMO(Nu) - EHOMO(TCE), referenced against tetracyanoethylene.", bold_prefix="Nucleophilicity Index (N): ")

    add_heading_2("5.3 UV-Vis Electronic Absorption Spectroscopy Studio")
    add_bullet("Theoretical TD-DFT convolution using Gaussian peak broadening with adjustable FWHM (σ, 5 to 50 nm).")
    add_bullet("Solvent Wavelength Calibration Shift (Δλ, -100 to +100 nm) to correct for empirical dielectric and solvent bath shifts.")
    add_bullet("Interactive display showing vertical excitation sticks (oscillator strengths fosc) and molar absorption coefficient (ε in L·mol⁻¹·cm⁻¹).")
    add_bullet("Multi-trace experimental overlay (.xlsx, .csv, .txt) with strict mathematical immutability: experimental traces are never altered by theoretical shift parameters.")
    add_bullet("High-resolution PNG and CSV tabular export.")

    add_heading_2("5.4 Vibrational FTIR Spectroscopic Studio")
    add_bullet("Harmonic vibrational frequency convolution using physical Lorentzian line shapes (FWHM γ) from normal mode transition dipole moments (T² in km/mol).")
    add_bullet("Standard spectroscopic axis: Wavenumber descending from 4000 cm⁻¹ (left) to 400 cm⁻¹ (right).")
    add_bullet("Dual Photometric Mode: Instant toggle between Transmittance (%T, downward bands) and Absorbance (AU, upward peaks).")
    add_bullet("Harmonic Scaling Factor: Slider control (0.800 to 1.100) to account for vibrational anharmonicity and basis set truncation.")
    add_bullet("Imaginary Frequency TS Verification: Flags imaginary vibrational modes (ν < 0 cm⁻¹) indicating a transition state (first-order saddle point) vs. a true local minimum.")

    add_heading_2("5.5 Two-Box Reaction Thermochemistry & Equilibrium Engine")
    add_body(
        "Evaluates chemical reaction thermodynamics by partitioning total energies into electronic and thermal contributions "
        "at arbitrary user-defined temperatures (T) and pressures (P):"
    )
    add_bullet(" Electronic energy change ΔEelec = Σ(n·E_prod) - Σ(m·E_react).", bold_prefix="Electronic Reaction Energy (ΔEelec): ")
    add_bullet(" Zero-point corrected energy ΔE0 = ΔEelec + ΔZPE.", bold_prefix="Zero-Point Corrected Energy (ΔE0): ")
    add_bullet(" Standard Enthalpy of reaction ΔH°(T) = ΔE0 + ΔH_thermal.", bold_prefix="Enthalpy of Reaction (ΔH°): ")
    add_bullet(" Standard Gibbs Free Energy of reaction ΔG°(T) = ΔH°(T) - T·ΔS°(T).", bold_prefix="Gibbs Free Energy (ΔG°): ")
    add_bullet(" Standard Entropy of reaction ΔS°(T) = (ΔH° - ΔG°) / T, precisely converted to cal/(mol·K) and SI units J/(mol·K) (where 1 cal = 4.184 J).", bold_prefix="Entropy of Reaction (ΔS°): ")
    add_bullet(" Thermodynamic Equilibrium Constant Keq = exp(-ΔG° / RT) with numerical underflow/overflow bounds checking.", bold_prefix="Equilibrium Constant (Keq): ")
    add_bullet(" Grimme's Quasi-RRHO (Rigid-Rotor Harmonic-Oscillator) interpolation for low-frequency vibrational entropy corrections.", bold_prefix="Quasi-RRHO Entropy: ")
    add_bullet(" Automated Bond Dissociation Energy (BDE) calculation for homolytic and heterolytic bond cleavage.", bold_prefix="Bond Dissociation Energy (BDE): ")

    add_heading_2("5.6 Scientific Consistency & Quality Audit Engine")
    add_body(
        "To prevent invalid thermodynamic comparisons, the Audit Engine automatically inspects all participating species in a reaction:",
        bold_prefix="Automated Quality Checks: "
    )
    add_bullet("Stoichiometric atom and charge balance check.")
    add_bullet("Level of theory audit: Flags discrepancies in DFT functionals (e.g., mixing B3LYP with PBE0).")
    add_bullet("Basis set consistency audit: Flags mixed basis set qualities (e.g., def2-SVP vs def2-TZVP).")
    add_bullet("Solvation model & dielectric check: Ensures all species share identical solvent parameters.")
    add_bullet("Temperature & pressure synchronization check.")

    # ─────────────────────────────────────────────────────────────
    # SECTION 6: TROUBLESHOOTING & BEST PRACTICES
    # ─────────────────────────────────────────────────────────────
    add_heading_1("6. Troubleshooting, Best Practices & Scientific FAQ")
    
    add_heading_2("6.1 Resolving SCF Convergence Failures")
    add_bullet("Difficult transition metal complexes or open-shell radical systems may exhibit oscillating SCF cycles. Recommended keywords: `! VeryTightSCF SlowConv KDIIS SOSCF`.")
    add_bullet("Ensure initial geometries are reasonable: run an initial force field or B97-3c pre-optimization before submitting high-level hybrid DFT.")

    add_heading_2("6.2 Preventing Kaggle Memory / Disk Allocation Errors")
    add_bullet("Keep `%maxcore` within 5000-6000 MB per core for 4-core jobs. Total memory must not exceed 24,000 MB (Kaggle limit is 30 GB).")
    add_bullet("Use `mode='essential'` when downloading results if bandwidth is limited; this eliminates heavy scratch wavefunctions (`.gbw`).")

    add_heading_2("6.3 Transition State Optimization (OptTS) & Frequency Verification")
    add_bullet("Always verify that an `OptTS` calculation produces exactly one imaginary frequency (e.g., `-342.5 cm⁻¹`).")
    add_bullet("Follow with an Intrinsic Reaction Coordinate (IRC) calculation to confirm the TS connects the correct reactant and product wells.")

    # ─────────────────────────────────────────────────────────────
    # SECTION 7: ACADEMIC CITATION GUIDE & ATTRIBUTION
    # ─────────────────────────────────────────────────────────────
    add_heading_1("7. Academic Citation Guide & Scientific Attribution")
    add_body(
        "When publishing research, articles, or computational datasets obtained using Chemistry Lab, "
        "please include the following academic citations for the platform and the underlying software packages:"
    )

    add_heading_2("7.1 How to Cite Chemistry Lab")
    add_body("BibTeX Entry:", bold_prefix="1. ")
    add_code_box(
        "@software{Hasan2026ChemistryLab,\n"
        "  author       = {Hasan, Abdulsalam S.},\n"
        "  title        = {{Chemistry Lab: A Web-Based Computational Chemistry Platform}},\n"
        "  year         = {2026},\n"
        "  version      = {1.0.0},\n"
        "  url          = {https://github.com/abd19990044-commits/chemistry-web-lab},\n"
        "  note         = {Compatible with ORCA 6.1 program system}\n"
        "}"
    )

    add_body("ACS / Vancouver Chemistry Format:", bold_prefix="2. ")
    add_body(
        "Hasan, A. S. Chemistry Lab, version 1.0.0; Advanced Computational Chemistry & Spectroscopic Analysis Platform, 2026. https://github.com/abd19990044-commits/chemistry-web-lab",
        space_after=8
    )

    add_body("APA 7th Edition Format:", bold_prefix="3. ")
    add_body(
        "Hasan, A. S. (2026). Chemistry Lab: A Web-Based Computational Chemistry Platform (Version 1.0.0) [Computer software]. Retrieved from https://github.com/abd19990044-commits/chemistry-web-lab",
        space_after=8
    )

    add_heading_2("7.2 Underlying Scientific Packages & Theoretical References")
    add_bullet(" Neese, F. (2022). Software update: The ORCA program system - Version 5.0. WIREs Comput. Mol. Sci., 12(5), e1606. / Neese, F. et al. (2026). The ORCA Quantum Chemistry Program System (Version 6.1).", bold_prefix="ORCA Electronic Structure Package: ")
    add_bullet(" Landrum, G. et al. (2026). RDKit: Open-source cheminformatics and machine learning toolkit. http://www.rdkit.org", bold_prefix="RDKit Chemoinformatics Library: ")
    add_bullet(" Rego, N., & Koes, D. (2015). 3Dmol.js: molecular visualization with WebGL. Bioinformatics, 31(8), 1322-1324.", bold_prefix="3Dmol.js WebGL Visualizer: ")
    add_bullet(" Lu, T., & Chen, F. (2012). Multiwfn: A multifunctional wavefunction analyzer. J. Comput. Chem., 33(5), 580-592.", bold_prefix="MultiWfn Wavefunction Analyzer: ")
    add_bullet(" Grimme, S., Antony, J., Ehrlich, S., & Krieg, H. (2010). A consistent and accurate ab initio parametrization of density functional dispersion correction (DFT-D3). J. Chem. Phys., 132(15), 154104.", bold_prefix="Grimme Dispersion Corrections (DFT-D3/D4): ")
    add_bullet(" Grimme, S. (2012). An improved modified rigid-rotor-harmonic-oscillator (quasi-RRHO) model for calculating entropy corrections. Chem. Eur. J., 18(32), 9955-9964.", bold_prefix="Grimme Quasi-RRHO Thermochemistry: ")
    add_bullet(" Grimme, S., Bannwarth, C., & Shushkov, P. (2017). A robust and accurate tight-binding quantum chemical method for structures, vibrational frequencies, and noncovalent interactions. J. Chem. Theory Comput., 13(5), 1989-2009.", bold_prefix="Composite Methods (r2SCAN-3c, B97-3c): ")

    # Ensure output directory exists and save document
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)
    print(f"Comprehensive Academic Manual successfully generated at: {output_path}")


if __name__ == "__main__":
    out_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static", "docs", "ChemistryLab_User_Manual.docx"))
    create_manual(out_file)
