# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures

import base64
import logging
import os
import io
import json
import re
import secrets
import shutil
import sys
import traceback
import zipfile
import tarfile
import threading
import time
import tempfile
try:
    import rarfile
    RAR_AVAILABLE = True
except ImportError:
    RAR_AVAILABLE = False

from flask import (Flask, abort, after_this_request, jsonify, render_template, request,
                   send_file, send_from_directory, session)

import chem_core as core
import reaction_conditions
import kaggle_runner
from services import kaggle_service, orca_service, health_service

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ORCA_ENGINE_SRC = os.path.join(BASE_DIR, "orca_engine", "src")
if os.path.isdir(_ORCA_ENGINE_SRC) and _ORCA_ENGINE_SRC not in sys.path:
    sys.path.insert(0, _ORCA_ENGINE_SRC)

try:
    from orca_engine.parser import OrcaParser
    from orca_engine.models import JobData, MoleculeData, EnergyKind
    from orca_engine.constants import PhysConst
    from orca_engine.thermochemistry import ThermochemistryEngine, Reaction, ReactionTerm, ReactionParseError
    from orca_engine.reporting import job_to_dict, reaction_result_to_dict
    from orca_engine.adapters.web_adapter import (
        job_to_web_json,
        molecule_to_web_json,
        convolute_uvvis_spectrum,
        generate_xyz_string,
        parse_experimental_text,
        parse_experimental_excel,
        parse_experimental_multi_series_excel,
        parse_experimental_multi_series_text,
        inspect_experimental_file,
        build_multi_spectrum_overlay,
        convolute_theoretical_spectrum,
        parse_nmr_output,
        ExperimentalSpectrum,
        ExperimentalSpectrumError,
    )
    from orca_engine.nmr import (
        NMRNucleus,
        NMRReference,
        NMRAtomRecord,
        NMRPeak,
        NMRSpectrum,
        NMRResult,
        OrcaNMRParser,
        build_nmr_spectrum,
        export_nmr_csv,
        export_nmr_json,
        NMR_REFERENCE_CATALOG,
    )
    ORCA_ENGINE_AVAILABLE = True
except Exception:  # noqa: BLE001
    OrcaParser = None  # type: ignore[assignment,misc]
    ORCA_ENGINE_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chemistry_tools")

# The project is documented with templates/index.html + static/{css,js}, but the
# same files are often uploaded side by side in one flat folder (which is all a
# Hugging Face Space needs). Rather than failing with a bare TemplateNotFound
# when that happens, both layouts are supported: whichever one is present wins.
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
if not os.path.isfile(os.path.join(TEMPLATE_DIR, "index.html")):
    TEMPLATE_DIR = BASE_DIR
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.isdir(STATIC_DIR):
    STATIC_DIR = BASE_DIR

# static_folder=None so the custom handler below owns the /static/ endpoint.
app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=None)


@app.route("/static/<path:filename>", endpoint="static")
def static_files(filename: str):
    """Serves css/js from either layout: `static/css/style.css` when the
    documented folders exist, or a flat `style.css` next to app.py when
    everything was uploaded into a single directory."""
    for candidate in (filename, os.path.basename(filename)):
        if os.path.isfile(os.path.join(STATIC_DIR, candidate)):
            return send_from_directory(STATIC_DIR, candidate, max_age=3600)
    abort(404)


app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB uploads cap for calculation archives

#: An RXN file repeats a structure once per unit of its coefficient, so this
#: bounds the file size and keeps the drawing legible.
MAX_COEFFICIENT = 50

# Idempotency guard for /api/kaggle/submit: a browser retry or double click
# that replays the same Idempotency-Key gets the original response instead of
# pushing a duplicate kernel. Entries expire so the map cannot grow forever.
SUBMIT_DEDUP: dict[str, tuple[float, dict]] = {}
SUBMIT_DEDUP_TTL_SECONDS = 30 * 60
_submit_dedup_lock = threading.Lock()


def _submit_dedup_lookup(idem_key: str | None) -> tuple | None:
    if not idem_key:
        return None
    now = time.time()
    with _submit_dedup_lock:
        hit = SUBMIT_DEDUP.get(idem_key)
        if hit and now - hit[0] <= SUBMIT_DEDUP_TTL_SECONDS:
            return hit[1]
        SUBMIT_DEDUP.pop(idem_key, None)
        expired = [k for k, (ts, _) in SUBMIT_DEDUP.items() if now - ts > SUBMIT_DEDUP_TTL_SECONDS]
        for k in expired:
            SUBMIT_DEDUP.pop(k, None)
    return None


def _submit_dedup_store(idem_key: str | None, response: dict) -> None:
    if not idem_key:
        return
    with _submit_dedup_lock:
        SUBMIT_DEDUP[idem_key] = (time.time(), response)


def _resolve_secret_key() -> str:
    """Returns a signing key that is the SAME in every gunicorn worker.

    The previous `os.environ.get("SECRET_KEY") or secrets.token_hex(32)` had a
    bug that is invisible in development and constant in production. Gunicorn
    runs without `--preload`, so each worker imports this module separately and
    each one generated a *different* random key. A session cookie signed by
    worker 1 then failed validation on worker 2, so with two workers a signed-in
    user was silently signed out on roughly half of their requests, at random.

    Resolution order:
      1. `SECRET_KEY` from the environment -- the correct production answer.
      2. A key persisted in the orchestrator's state directory, generated once
         and shared by every worker on this host. Survives a worker restart;
         lost on a Space rebuild, which only means users sign in again.
      3. A process-local random key, with a loud warning.
    """
    from_env = os.environ.get("SECRET_KEY")
    if from_env:
        return from_env

    try:
        from orca_orchestrator.config import CONFIG

        key_path = os.path.join(CONFIG.store.state_dir, "flask_secret_key")
        if os.path.isfile(key_path):
            with open(key_path, "r", encoding="ascii") as fh:
                existing = fh.read().strip()
            if len(existing) >= 32:
                return existing
        generated = secrets.token_hex(32)
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, generated.encode("ascii"))
        finally:
            os.close(fd)
        return generated
    except FileExistsError:
        # Another worker created it between our check and our write.
        with open(key_path, "r", encoding="ascii") as fh:
            return fh.read().strip()
    except Exception:  # noqa: BLE001
        log.warning(
            "SECRET_KEY is not set and no shared key could be persisted. Sessions will "
            "not survive a restart and may be inconsistent across gunicorn workers. Set "
            "SECRET_KEY in the Space's environment to fix this."
        )
        return secrets.token_hex(32)


app.secret_key = _resolve_secret_key()

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────
# The fault-tolerant checkpoint/restart subsystem lives in orca_orchestrator
# and is mounted at /api/orca/*. The older /api/kaggle/* routes below are kept
# so that chains submitted before this deploy keep working -- an in-flight ORCA
# calculation can be days old, and cutting it off at a redeploy would destroy
# real work. New submissions should use /api/orca/submit.
try:
    from orca_orchestrator.api import register as register_orchestrator
    from orca_orchestrator.legacy_compat import install_legacy_route_adapter
    from orca_orchestrator.logging_ext import configure as configure_logging

    configure_logging()
    install_legacy_route_adapter(app)
    register_orchestrator(app)
    ORCHESTRATOR_AVAILABLE = True
except Exception:  # noqa: BLE001
    # Reaching this now means a genuine import-time failure -- a missing file, a
    # syntax error, an incompatible Python. It deliberately no longer catches a
    # *runtime* initialisation failure.
    #
    # It used to. `register_orchestrator` warmed the service before registering
    # the routes, so when two gunicorn workers raced to create the same fresh
    # SQLite file at boot, the loser landed here and served the whole site with
    # the orchestrator switched off for the life of that process. With two
    # workers that meant roughly half of all /api/orca/* requests returned 404,
    # intermittently -- which to a user looks like the site randomly losing
    # their jobs. `register()` now attaches the routes first and retries the
    # service lazily, so a boot race is self-healing.
    #
    # The fallback remains because the molecule and reaction tools have nothing
    # to do with Kaggle and should stay up regardless.
    log.error("orchestrator failed to import; falling back to the legacy Kaggle "
              "routes only:\n%s", traceback.format_exc())
    ORCHESTRATOR_AVAILABLE = False


def b64(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode("ascii") if data else None


def error_response(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# ─────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return _render_lab_index("home")


@app.route("/lab")
def lab_view():
    """Primary section 1 - structure/reaction drawing and ORCA setup."""
    return _render_lab_index("draw")


@app.route("/calculations")
def calculations_view():
    """Primary section 2 - Kaggle sign-in, submission and job management."""
    return _render_lab_index("orca")


@app.route("/analysis")
def analysis_view():
    """Primary section 3 - quantum results, spectra, thermochemistry."""
    return _render_lab_index("quantum")


def _render_lab_index(initial_view: str):
    """Renders the single-page app with one studio pre-activated.

    Each primary section (/lab, /calculations, /analysis) is a REAL Flask
    route, so deep links, refreshes and back/forward navigation all work
    without a client-side router; the template activates the requested view
    through the existing showView() switcher."""
    return render_template(
        "index.html",
        initial_view=initial_view,
        calc_types=core.CALC_TYPES,
        composite_methods=core.COMPOSITE_METHODS,
        dft_functionals=core.DFT_FUNCTIONALS,
        mp2_variants=core.MP2_VARIANTS,
        ccsd_variants=core.CCSD_VARIANTS,
        ri_options=core.RI_OPTIONS,
        basis_map=core.BASIS_MAP,
        dispersion_models=core.DISPERSION_MODELS,
        solvation_models=core.SOLVATION_MODELS,
        solvents=core.SOLVENTS,
        scf_options=core.SCF_CONVERGENCE,
        google_client_id=GOOGLE_CLIENT_ID,
    )



@app.errorhandler(413)
def _too_large(_exc):
    """Flask's default 413 is an HTML page; the browser does resp.json() on it
    and shows a JSON parse error instead of "your file is too big"."""
    return jsonify({
        "ok": False,
        "error": ("That upload is larger than the %d MB maximum size. Please ensure "
                  "your archive or calculation output file is under 500 MB."
                  % (app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))),
    }), 413


@app.route("/download/manual")
def download_academic_manual():
    """Serves the Academic User Manual & Technical Reference Guide (.docx)."""
    manual_path = os.path.join(STATIC_DIR, "docs", "ChemistryLab_User_Manual.docx")
    if not os.path.isfile(manual_path):
        import sys
        sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
        try:
            from generate_academic_manual import create_manual
            os.makedirs(os.path.dirname(manual_path), exist_ok=True)
            create_manual(manual_path)
        except Exception:
            pass
    if os.path.isfile(manual_path):
        return send_file(
            manual_path,
            as_attachment=True,
            download_name="ORCA_Web_Lab_Academic_User_Manual.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
@app.route("/api/license")
def api_license():
    """Returns license metadata and permissions summary for Chemistry Lab."""
    return jsonify({
        "ok": True,
        "product_name": "Chemistry Lab",
        "application": "chemistry-lab",
        "version": "1.0.3",
        "license_name": "ORCA Web Lab Academic and Non-Commercial License v1.1",
        "license_type": "Source-Available Academic & Non-Commercial",
        "copyright": "Copyright (c) 2026 Abdulsalam S. Hasan. All rights reserved.",
        "author": "Abdulsalam S. Hasan",
        "permitted_uses": [
            "academic_research",
            "classroom_teaching",
            "student_theses",
            "scientific_benchmarks",
            "peer_review_reproducibility",
            "local_non_commercial_modification",
        ],
        "non_commercial_standard": "Nature and purpose of the activity, regardless of organizational entity type.",
        "commercial_use": "Prohibited under academic license; requires separate Commercial License.",
        "full_ip_assignment": "Requires separate written agreement executed with the author.",
        "contact": {
            "name": "Abdulsalam S. Hasan",
            "email": "abd.19990044@gmail.com",
            "whatsapp": "+9647715541279",
            "repository": "https://github.com/abd19990044-commits/chemistry-web-lab",
        },
        "third_party_notice": "Third-party components and ORCA are governed by their respective licenses. See /api/third-party-licenses.",
    })


@app.route("/api/third-party-licenses")
def api_third_party_licenses():
    """Returns third-party component license summary."""
    return jsonify({
        "ok": True,
        "orca_boundary": {
            "program": "ORCA Quantum Chemistry Software",
            "authors": "Frank Neese et al. (Max Planck Institute für Kohlenforschung / FACCTs GmbH)",
            "bundled": False,
            "license_type": "Proprietary Academic EULA / Commercial via FACCTs GmbH",
            "notice": "ORCA is NOT bundled or distributed with Chemistry Lab. Users must obtain their own license directly from the official ORCA forum.",
        },
        "python_dependencies": [
            {"name": "Flask", "license": "BSD-3-Clause", "url": "https://github.com/pallets/flask"},
            {"name": "gunicorn", "license": "MIT", "url": "https://github.com/benoitc/gunicorn"},
            {"name": "requests", "license": "Apache-2.0", "url": "https://github.com/psf/requests"},
            {"name": "rdkit", "license": "BSD-3-Clause", "url": "https://github.com/rdkit/rdkit"},
            {"name": "kaggle", "license": "Apache-2.0", "url": "https://github.com/Kaggle/kaggle-api"},
            {"name": "google-auth", "license": "Apache-2.0", "url": "https://github.com/googleapis/google-auth-library-python"},
            {"name": "pillow", "license": "HPND", "url": "https://github.com/python-pillow/Pillow"},
            {"name": "rarfile", "license": "ISC", "url": "https://github.com/markokr/rarfile"},
            {"name": "openpyxl", "license": "MIT", "url": "https://foss.heptapod.net/openpyxl/openpyxl"},
            {"name": "jsonschema", "license": "MIT", "url": "https://github.com/python-jsonschema/jsonschema"},
            {"name": "cryptography", "license": "Apache-2.0 / BSD", "url": "https://github.com/pyca/cryptography"},
            {"name": "numpy", "license": "BSD-3-Clause", "url": "https://github.com/numpy/numpy"},
        ],
        "frontend_libraries": [
            {"name": "3Dmol.js", "license": "BSD-3-Clause", "authors": "Nicholas Rego & David Koes", "url": "https://github.com/3dmol/3Dmol.js"},
            {"name": "Space Grotesk Font", "license": "OFL-1.1", "authors": "Florian Karsten"},
            {"name": "Inter Font", "license": "OFL-1.1", "authors": "Rasmus Andersson"},
            {"name": "JetBrains Mono Font", "license": "OFL-1.1", "authors": "JetBrains s.r.o."},
        ],
        "licensing_contact": {
            "email": "abd.19990044@gmail.com",
            "whatsapp": "+9647715541279",
        },
    })


@app.route("/health")
def health():

    """Liveness plus a real readiness picture (shared health_service)."""
    payload = health_service.build_health(
        orchestrator_available=ORCHESTRATOR_AVAILABLE,
        orca_engine_available=ORCA_ENGINE_AVAILABLE)
    return jsonify(payload)


@app.route("/api/auth/google", methods=["POST"])
def api_auth_google():
    if not GOOGLE_CLIENT_ID:
        return error_response("Google Sign-In is not configured on this server.", 501)

    data = request.get_json(force=True, silent=True) or {}
    id_token = data.get("credential")
    if not id_token:
        return error_response("Missing Google credential token.")

    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests

        info = google_id_token.verify_oauth2_token(
            id_token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        user = {
            "sub": info.get("sub"),
            "name": info.get("name"),
            "email": info.get("email"),
            "picture": info.get("picture"),
        }
        session["user"] = user
        return jsonify({"ok": True, "user": user})
    except Exception as exc:  # noqa: BLE001
        log.warning("Google token verification failed: %s", exc)
        return error_response("Could not verify Google sign-in.", 401)


@app.route("/api/auth/me")
def api_auth_me():
    return jsonify({"ok": True, "user": session.get("user")})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.pop("user", None)
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────
# Molecule Explorer
# ─────────────────────────────────────────────────────────────
@app.route("/api/compound", methods=["POST"])
def api_compound():
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return error_response("Please enter a compound name, SMILES, or InChI.")

    try:
        parsed = core.parse_compound(query)
        smiles = core.resolve_compound_to_smiles(query)
        if not smiles:
            return error_response(f"Could not recognize \u201c{query}\u201d. Check the spelling, or try a SMILES/InChI string instead.", 404)

        image_bytes = core.render_molecule_png(smiles)
        if not image_bytes:
            return error_response("Could not draw a structure for this input.", 422)
        mol_bytes = core.generate_mol_file_bytes(smiles)
        svg_bytes = core.render_molecule_svg(smiles)

        props = None
        wiki = None
        solubility: list[str] = []
        if parsed.kind is core.InputKind.NAME:
            props = core.fetch_pubchem_properties(query)
        elif smiles:
            props = core.fetch_pubchem_properties(smiles)

        if props:
            title_candidate = props.get("Title")
            wiki = core.fetch_wikipedia_summary(query) or \
                (core.fetch_wikipedia_summary(title_candidate) if title_candidate and title_candidate.lower() != query.lower() else None)
            cid = props.get("CID")
            if cid:
                solubility = core.fetch_solubility(cid)

        iupac_name = (props or {}).get("IUPACName")
        if not iupac_name:
            iupac_name = core.fetch_iupac_name(query, smiles) or (query if core.is_iupac_name(query) else "")

        payload = {
            "ok": True,
            "input_kind": parsed.kind.name,
            "smiles": smiles,
            "image_png_base64": b64(image_bytes),
            "image_svg_base64": b64(svg_bytes),
            "mol_file_base64": b64(mol_bytes),
            "filename": core.safe_filename(query),
            "formula": (props or {}).get("MolecularFormula"),
            "weight": (props or {}).get("MolecularWeight"),
            "iupac_name": iupac_name,
            "title": (props or {}).get("Title") or query.capitalize(),
            "query_name": query,
            "solubility": solubility,
            "wikipedia_summary": wiki,
        }
        return jsonify(payload)
    except Exception:  # noqa: BLE001
        log.error("api_compound failed:\n%s", traceback.format_exc())
        return error_response("An unexpected technical error occurred.", 500)


# ─────────────────────────────────────────────────────────────
# Reaction Drawing
# ─────────────────────────────────────────────────────────────
@app.route("/api/reaction", methods=["POST"])
def api_reaction():
    """Draws a chemical equation, with stoichiometric coefficients.

    Three things come back besides the picture: whether the equation balances,
    an MDL RXN file, and the result of structurally validating that file. The
    validation is what the "opens in ChemDraw" badge is based on - see
    chem_core.validate_rxn_block for why it is phrased as a format check rather
    than a claim about a program that is not installed here.
    """
    data = request.get_json(force=True, silent=True) or {}
    reactants_str = (data.get("reactants") or "").strip()
    products_str = (data.get("products") or "").strip()
    arrow_top = (data.get("arrow_top") or "").strip()[:240]
    arrow_bottom = (data.get("arrow_bottom") or "").strip()[:240]
    if not reactants_str or not products_str:
        return error_response("Please enter both reactants and products.")

    try:
        reactant_terms = core.split_compound_terms(reactants_str)
        product_terms = core.split_compound_terms(products_str)
        if not reactant_terms or not product_terms:
            return error_response("Could not parse the compound list.")

        for term in reactant_terms + product_terms:
            if term.coefficient <= 0:
                return error_response(
                    f"'{term.name}' has a coefficient of {term.pretty_coefficient or 1}. "
                    "A stoichiometric coefficient has to be greater than zero.")
            if term.coefficient > MAX_COEFFICIENT:
                return error_response(
                    f"The coefficient on '{term.name}' is {term.coefficient}. "
                    f"The largest this tool will draw is {MAX_COEFFICIENT}: an MDL RXN file "
                    "represents a coefficient by repeating the structure, so a very large "
                    "one produces a file no editor can display usefully.")

        resolved: dict[str, str] = {}
        interpretations: list[str] = []
        unresolved = []
        unique_names = list({t.name for t in reactant_terms + product_terms})
        
        def _resolve_worker(name: str):
            return name, core.resolve_species(name)

        if len(unique_names) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(unique_names), 8)) as pool:
                res_list = list(pool.map(_resolve_worker, unique_names))
        else:
            res_list = [_resolve_worker(unique_names[0])] if unique_names else []

        for name, (smiles, note) in res_list:
            if smiles:
                resolved[name] = smiles
                if note:
                    interpretations.append(note)
            else:
                unresolved.append(name)
        if unresolved:
            if not core.pubchem_reachable():
                return error_response(
                    "The PubChem lookup service could not be reached from this server, so "
                    f"{', '.join(unresolved)} could not be resolved. This is not a problem "
                    "with your equation - SMILES strings are resolved locally and still work.",
                    503)
            return error_response("Could not recognize: " + ", ".join(unresolved), 404)

        # An MDL RXN cannot express a fraction, so 1/2 O2 is scaled away - and
        # said out loud, because it changes the numbers the person typed.
        factor, (rxn_reactants, rxn_products) = core.scale_terms_to_integers(
            reactant_terms, product_terms)

        as_pairs = lambda terms: [(t.coefficient, resolved[t.name]) for t in terms]  # noqa: E731
        balance = core.check_reaction_balance(as_pairs(reactant_terms), as_pairs(product_terms))

        # Default on: writing O2 rather than drawing it is what a chemist does.
        small_as_formula = data.get("small_as_formula", True) is not False
        image_bytes = core.render_reaction_png(
            as_pairs(reactant_terms), as_pairs(product_terms),
            small_as_formula=small_as_formula,
            arrow_top=arrow_top, arrow_bottom=arrow_bottom)
        image_bytes = reaction_conditions.overlay_png(image_bytes, arrow_top, arrow_bottom)
        svg_bytes = core.render_reaction_svg(
            as_pairs(reactant_terms), as_pairs(product_terms),
            small_as_formula=small_as_formula,
            arrow_top=arrow_top, arrow_bottom=arrow_bottom)
        svg_bytes = reaction_conditions.overlay_svg(svg_bytes, arrow_top, arrow_bottom)
        if not image_bytes:
            return error_response("Could not draw the reaction scheme - check the formulas.", 422)

        equation = core.format_equation(reactant_terms, product_terms)
        rxn_bytes = core.generate_rxn_file_bytes(as_pairs(rxn_reactants), as_pairs(rxn_products),
                                                 title=equation)
        if not rxn_bytes:
            return error_response("The reaction file could not be written.", 422)
        rxn_report = core.validate_rxn_block(rxn_bytes.decode("utf-8", errors="replace"))

        notes = list(interpretations)
        if factor != 1:
            notes.append(
                f"The MDL RXN format has no field for a fractional coefficient, so every "
                f"coefficient was multiplied by {factor} in the file "
                f"({core.format_equation(rxn_reactants, rxn_products)}). The ratios, and "
                "therefore the chemistry, are unchanged. The drawing above still shows the "
                "equation as you wrote it.")
        if any(t.coefficient != 1 for t in rxn_reactants + rxn_products):
            notes.append(
                "In the file a coefficient is represented the way MDL defines it - the "
                "structure appears that many times. ChemDraw will show two water molecules "
                "rather than the text '2 H2O'; the stoichiometry is there, just not as a "
                "numeral.")

        return jsonify({
            "ok": True,
            "image_png_base64": b64(image_bytes),
            "image_svg_base64": b64(svg_bytes),
            "rxn_file_base64": b64(rxn_bytes),
            "reaction_smiles": "%s>>%s" % (
                ".".join(resolved[t.name] for t in reactant_terms),
                ".".join(resolved[t.name] for t in product_terms)),
            "equation": equation,
            "balance": balance,
            "file_report": rxn_report,
            "notes": notes,
        })
    except Exception:  # noqa: BLE001
        log.error("api_reaction failed:\n%s", traceback.format_exc())
        return error_response("An unexpected technical error occurred.", 500)


# ─────────────────────────────────────────────────────────────
# ORCA Input Generator
# ─────────────────────────────────────────────────────────────
@app.route("/api/orca/coords", methods=["POST"])
def api_orca_coords():
    """Fetch 3D coordinates for a compound name to prefill the wizard."""
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return error_response("Please enter a compound name.")
    try:
        props = core.fetch_pubchem_properties(query)
        smiles = core.resolve_compound_to_smiles(query)
        cid = (props or {}).get("CID")

        xyz = None
        if cid:
            sdf = core.fetch_pubchem_sdf(cid)
            if sdf:
                xyz = core.mol_block_to_xyz(sdf)
        if not xyz and smiles:
            xyz = core.xyz_from_smiles(smiles)
        if not xyz:
            # A compound PubChem has never heard of and a PubChem that cannot be
            # reached both end up here, and telling someone their molecule does
            # not exist when the truth is "this deployment has no outbound
            # network" sends them looking in the wrong place entirely.
            if props is None and smiles is None and not core.pubchem_reachable():
                return error_response(
                    "The PubChem lookup service could not be reached from this server, so "
                    "the name could not be resolved. This is not a problem with your "
                    "compound. Paste the coordinates directly, or upload an .xyz file.", 503)
            return error_response(
                "PubChem has no 3D structure for that name. Check the spelling, try the "
                "IUPAC name or a SMILES string, or paste the coordinates directly.", 404)

        return jsonify({
            "ok": True,
            "coords": xyz,
            "name": core.safe_filename(query),
            "query_name": query,
            "formula": (props or {}).get("MolecularFormula"),
            "iupac_name": (props or {}).get("IUPACName") or (query if core.is_iupac_name(query) else ""),
            "title": (props or {}).get("Title") or query.capitalize(),
        })
    except Exception:  # noqa: BLE001
        log.error("api_orca_coords failed:\n%s", traceback.format_exc())
        return error_response("An unexpected technical error occurred while fetching coordinates.", 500)


@app.route("/api/orca/coords/file", methods=["POST"])
def api_orca_coords_file():
    """Extract coordinates from an uploaded .xyz/.sdf/.mol file."""
    if "file" not in request.files:
        return error_response("No file was attached.")
    f = request.files["file"]
    ext = os.path.splitext(f.filename or "")[1].lower()
    raw = f.read()
    xyz, err = core.xyz_from_uploaded_file(raw, ext)
    if err:
        return error_response(err, 422)
    return jsonify({"ok": True, "coords": xyz, "name": core.safe_filename(os.path.splitext(f.filename)[0])})


@app.route("/api/orca/builder/clean", methods=["POST"])
def api_orca_builder_clean():
    """Optimize/relax 3D coordinates using Universal Force Field (UFF) with graceful fallback."""
    data = request.get_json(force=True, silent=True) or {}
    coords = (data.get("coords") or "").strip()
    try:
        charge = int(data.get("charge", 0)) if data.get("charge") is not None else 0
    except (ValueError, TypeError):
        charge = 0
    if not coords:
        return error_response("No 3D coordinates were provided.")
    opt_coords, err_msg = core.clean_3d_coordinates_uff(coords, charge=charge)
    if not opt_coords:
        return jsonify({
            "ok": False,
            "optimized": False,
            "converged": False,
            "error": err_msg or "Force field relaxation could not converge for this structure. Original geometry preserved.",
            "coords": coords
        }), 200
    return jsonify({
        "ok": True,
        "optimized": True,
        "converged": True,
        "coords": opt_coords
    })



@app.route("/api/orca/generate", methods=["POST"])
def api_orca_generate():
    data = request.get_json(force=True, silent=True) or {}
    result, status = orca_service.generate_inputs(data)
    return jsonify(result), status

@app.route("/api/kaggle/login", methods=["POST"])
def api_kaggle_login():
    """Credential verification and job listing with optional encrypted persistence."""
    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = _resolve_kaggle_credentials(kaggle_username, kaggle_key)
    if not kaggle_username or not kaggle_key:
        return error_response("Please enter your Kaggle username and API key/token.")
    try:
        if ORCHESTRATOR_AVAILABLE:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.service import get_service
            from orca_orchestrator.legacy_compat import _legacy_job
            from orca_orchestrator.credential_vault import get_vault_manager
            creds = parse_credentials(kaggle_username, kaggle_key)
            service = get_service()
            service.authenticate(creds.username, creds.key or creds.api_token)
            # Persist encrypted credential if master key configured
            try:
                get_vault_manager().save_credentials(creds.username, creds)
            except Exception as save_err:
                log.debug("Auto-save to vault skipped: %s", save_err)
            return jsonify({"ok": True, "username": creds.username, "jobs": [_legacy_job(j) for j in service.list_jobs(creds)], "owner": creds.username})
        auth = kaggle_runner.verify_kaggle_credentials(kaggle_username, kaggle_key)
        jobs = kaggle_runner.list_jobs(kaggle_username, kaggle_key)
        return jsonify({"ok": True, "username": auth["username"], "jobs": jobs})
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        return error_response(str(exc), 503)
    except Exception as exc:
        return error_response(f"Could not sign in to Kaggle: {str(exc).strip() or 'credential verification failed.'}", 401)


@app.route("/api/kaggle/sync", methods=["POST"])
def api_kaggle_sync():
    """Synchronize jobs independently from authentication."""
    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = _resolve_kaggle_credentials(kaggle_username, kaggle_key)
    if not kaggle_username or not kaggle_key:
        return error_response("Missing Kaggle username or API key/token.")
    try:
        if ORCHESTRATOR_AVAILABLE:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.service import get_service
            from orca_orchestrator.legacy_compat import _legacy_job
            from orca_orchestrator.credential_vault import get_vault_manager
            creds = parse_credentials(kaggle_username, kaggle_key)
            service = get_service()
            try:
                get_vault_manager().save_credentials(creds.username, creds)
            except Exception:
                pass
            return jsonify({"ok": True, "jobs": [_legacy_job(j) for j in service.list_jobs(creds)], "owner": creds.username, "username": creds.username})
        return jsonify({"ok": True, "jobs": kaggle_runner.list_jobs(kaggle_username, kaggle_key)})
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        return error_response(str(exc), 503)
    except Exception as exc:
        return error_response(f"Could not synchronize Kaggle jobs: {exc}", 502)


@app.route("/api/kaggle/credentials", methods=["GET", "POST", "DELETE"])
def api_kaggle_credentials():
    """Owner-scoped encrypted credential management endpoint."""
    if not ORCHESTRATOR_AVAILABLE:
        return error_response("Credential vault is unavailable.", 503)

    from orca_orchestrator.credential_vault import (
        CredentialVerificationError,
        get_vault_manager,
    )
    from orca_orchestrator.credentials import parse as parse_credentials

    vault_mgr = get_vault_manager()
    owner = _extract_request_identity()

    if request.method == "GET":
        if not owner:
            return error_response("Authentication required to view credential status.", 401)
        meta = vault_mgr.get_metadata(owner)
        return jsonify({"ok": True, "credential": meta.to_dict()})

    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        username = (data.get("kaggle_username") or request.form.get("kaggle_username") or "").strip()
        key = (data.get("kaggle_key") or request.form.get("kaggle_key") or "").strip()
        if not username or not key:
            return error_response("Both kaggle_username and API key/token are required.")

        target_owner = owner or username.lower()
        try:
            creds = parse_credentials(username, key)
            saved = vault_mgr.save_credentials(target_owner, creds, verify_with_kaggle=True)
            return jsonify({
                "ok": True,
                "saved_to_vault": saved,
                "owner": target_owner,
                "message": "Credentials verified and saved securely in encrypted vault.",
            })
        except CredentialVerificationError as exc:
            return error_response(str(exc), 401)
        except Exception as exc:
            log.error("Failed to save credentials to vault: %s", exc)
            return error_response(f"Failed to persist credentials: {exc}", 500)

    if request.method == "DELETE":
        if not owner:
            return error_response("Authentication required to delete credentials.", 401)
        deleted = vault_mgr.delete_credentials(owner)
        return jsonify({"ok": True, "deleted": deleted, "owner": owner})

    return error_response("Method not allowed", 405)


def _extract_request_identity() -> str | None:
    """Extracts authenticated user identity from session, headers, or parameters."""
    user = session.get("user")
    if user and isinstance(user, dict):
        identity = user.get("sub") or user.get("email") or user.get("name")
        if identity:
            return str(identity).strip()

    for h in ("X-Kaggle-Username", "X-Owner-Id", "X-User-Id"):
        val = request.headers.get(h)
        if val and val.strip():
            return val.strip()

    form_val = request.form.get("kaggle_username") or request.form.get("owner_id") or request.args.get("owner_id")
    if form_val and form_val.strip():
        return form_val.strip()

    return None


@app.route("/api/kaggle/submit", methods=["POST"])
def api_kaggle_submit():
    idem_key = request.headers.get("Idempotency-Key")
    cached = _submit_dedup_lookup(idem_key)
    if cached is not None:
        log.info("Duplicate submit blocked by Idempotency-Key: %s", idem_key)
        return jsonify(cached)

    kaggle_username = (request.form.get("kaggle_username") or "").strip()
    kaggle_key = (request.form.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = _resolve_kaggle_credentials(kaggle_username, kaggle_key)
    dataset_sources_raw = (request.form.get("dataset_sources") or "").strip()
    orca_link = (request.form.get("orca_link") or "").strip()
    input_filename = (request.form.get("input_filename") or "molecule.inp").strip()
    input_content = request.form.get("input_content") or ""
    job_name = (request.form.get("job_name") or "").strip()

    if not kaggle_username or not kaggle_key:
        return error_response("Please enter your Kaggle username and API key/token.")
    if not dataset_sources_raw and not orca_link:
        return error_response(
            "Provide an ORCA source: either a Kaggle Dataset identifier that holds your own "
            "licensed ORCA package (example: username/orca-6-1-0), or a Google Drive / direct "
            "download link."
        )

    # A ready-made .inp file upload takes precedence over the textarea content.
    input_file = request.files.get("input_file")
    if input_file and input_file.filename:
        input_filename = input_file.filename
        raw = input_file.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return jsonify({"ok": False, "error": "the .inp file is larger than the 2 MB upload limit (got > 2 MB)"}), 413
        input_content = raw.decode("utf-8", errors="replace")

    if not input_content.strip():
        return error_response(
            "There is no .inp content to submit. Build one with the Input Generator, "
            "paste it directly, or upload a ready-made .inp file."
        )

    dataset_sources = kaggle_runner.clean_dataset_sources(dataset_sources_raw)
    input_filename = core.safe_filename(os.path.splitext(input_filename)[0]) + ".inp"

    files_payload = {input_filename: base64.b64encode(input_content.encode("utf-8")).decode("utf-8")}

    for f in request.files.getlist("aux_files"):
        if not f.filename:
            continue
        name = os.path.basename(f.filename)
        # ASCII restart artefacts only. A .gbw is refused on purpose: it cannot
        # be verified for truncation, and ORCA AutoStart picking up a corrupt
        # one aborts the run with "GBWFile is corrupt". Continuation here runs
        # on geometries, trajectories and Hessians instead.
        if not name.lower().endswith((".xyz", ".allxyz", ".hess", ".mdrestart")):
            continue
        files_payload[name] = base64.b64encode(f.read()).decode("utf-8")

    payload, status = kaggle_service.submit_job(
        kaggle_username=kaggle_username,
        kaggle_key=kaggle_key,
        dataset_sources_raw=dataset_sources,
        orca_link=orca_link,
        input_filename=input_filename,
        input_content=input_content,
        job_name=job_name,
        idem_key=idem_key,
        extra_files=files_payload,
    )
    if idem_key and payload.get("ok") and status == 200:
        # Compatibility cache layer only: the authoritative replay store is
        # the orchestrator store the service just persisted to.
        kaggle_service.submit_dedup_store(idem_key, payload)
    return jsonify(payload), status

@app.route("/api/kaggle/status", methods=["POST"])
def api_kaggle_status():
    data = request.get_json(force=True, silent=True) or {}
    payload, status = kaggle_service.check_status(
        data.get("kaggle_username") or "", data.get("kaggle_key") or "",
        data.get("job_id") or "")
    return jsonify(payload), status

@app.route("/api/kaggle/download", methods=["GET", "POST"])
def api_kaggle_download():
    """Fetches a completed job's output directly from Kaggle's own kernel
    storage and streams it to the browser. Supports both essential (fast lightweight)
    and full archive modes, with automatic repair of incomplete archives."""
    if request.method == "GET":
        data = request.args.to_dict()
    else:
        data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = _resolve_kaggle_credentials(kaggle_username, kaggle_key)
    job_id = (data.get("job_id") or "").strip()
    mode = (data.get("mode") or "essential").strip().lower()

    if not kaggle_username or not kaggle_key or not job_id:
        return error_response("Missing username, API key, or job id.")
    if not kaggle_runner.is_valid_job_id(job_id):
        return error_response("That job id doesn't look like one of this site's jobs.")

    cleanup_dir = None
    try:
        if ORCHESTRATOR_AVAILABLE:
            try:
                from orca_orchestrator.credentials import parse as parse_credentials
                from orca_orchestrator.service import get_service
                creds = parse_credentials(kaggle_username, kaggle_key)
                zip_path, cleanup_dir = get_service().fetch_results(creds, job_id)
            except Exception:
                zip_path, cleanup_dir = kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)
        else:
            zip_path, cleanup_dir = kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        log.error("kaggle CLI unavailable:\n%s", traceback.format_exc())
        return error_response(str(exc), 503)
    except Exception as exc:  # noqa: BLE001
        log.error("api_kaggle_download failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to fetch results from Kaggle: {exc}", 502)

    if not zip_path or not cleanup_dir:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        return error_response(
            "No output files were found for this job yet on Kaggle. "
            "It may still be finishing up - try again in a minute, or check the "
            "notebook directly on kaggle.com.",
            404,
        )

    # 1. Essential Mode (Default): Create lightweight package for fast transfer (< 1 MB)
    ESSENTIAL_EXTS = (".out", ".log", ".xyz", ".inp", ".property.txt", ".txt", ".png", ".svg", ".json", ".dat", ".csv", ".mol", ".sdf", ".trj")
    EXCLUDED_EXTS = (".gbw", ".tmp", ".densities", ".hess", ".scfp", ".int", ".ges", ".prop")
    SYSTEM_ZIP_NAMES = ("_essential_results.zip", "_repaired_results.zip", "_partial_results.zip", "_fallback_results.zip", "results.zip")

    if mode == "essential":
        essential_zip = os.path.join(cleanup_dir, "_essential_results.zip")
        bundled = False
        added_names = set()
        with zipfile.ZipFile(essential_zip, "w", zipfile.ZIP_DEFLATED) as ezf:
            if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
                with zipfile.ZipFile(zip_path, "r") as src_zf:
                    for item in src_zf.infolist():
                        if item.is_dir():
                            continue
                        fname_lower = item.filename.lower()
                        if any(fname_lower.endswith(ext) for ext in ESSENTIAL_EXTS) and not any(fname_lower.endswith(ext) for ext in EXCLUDED_EXTS):
                            if item.filename not in added_names:
                                ezf.writestr(item.filename, src_zf.read(item))
                                added_names.add(item.filename)
                                bundled = True
            for root, _, files in os.walk(cleanup_dir):
                for f in files:
                    if f in SYSTEM_ZIP_NAMES:
                        continue
                    full_p = os.path.join(root, f)
                    fname_lower = f.lower()
                    if any(fname_lower.endswith(ext) for ext in ESSENTIAL_EXTS) and not any(fname_lower.endswith(ext) for ext in EXCLUDED_EXTS):
                        if not any(f.startswith(sec) for sec in ("__results__", "__script__", "__notebook__", "script.py")):
                            if f not in added_names:
                                ezf.write(full_p, f)
                                added_names.add(f)
                                bundled = True
        if bundled and os.path.exists(essential_zip) and zipfile.is_zipfile(essential_zip) and os.path.getsize(essential_zip) > 30:
            zip_path = essential_zip
        else:
            # If nothing essential was bundled, remove the empty essential zip and keep original zip_path
            if os.path.exists(essential_zip):
                try:
                    os.remove(essential_zip)
                except Exception:
                    pass

    # 2. Verify and repair zip integrity if needed
    if not os.path.exists(zip_path) or os.path.getsize(zip_path) == 0 or not zipfile.is_zipfile(zip_path):
        repaired_zip = os.path.join(cleanup_dir, "_repaired_results.zip")
        bundled = False
        with zipfile.ZipFile(repaired_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(cleanup_dir):
                for f in files:
                    if f in SYSTEM_ZIP_NAMES:
                        continue
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, cleanup_dir)
                    if any(rel_p.startswith(sec) for sec in ("__results__", "__script__", "__notebook__", "script.py")):
                        continue
                    zf.write(full_p, rel_p)
                    bundled = True
        if bundled and zipfile.is_zipfile(repaired_zip) and os.path.getsize(repaired_zip) > 30:
            zip_path = repaired_zip
        elif not os.path.exists(zip_path) or not zipfile.is_zipfile(zip_path) or os.path.getsize(zip_path) <= 30:
            if cleanup_dir:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            return error_response("Output archive could not be verified as a valid zip file.", 502)

    # Safe memory buffering for files under 100 MB to prevent Windows file-locking & premature deletion issues
    file_size = os.path.getsize(zip_path)
    download_name = f"{job_id}_results.zip" if mode == "essential" else f"{job_id}_full_results.zip"
    if file_size <= 100 * 1024 * 1024:
        with open(zip_path, "rb") as fh:
            data_bytes = io.BytesIO(fh.read())
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        return send_file(
            data_bytes,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/zip"
        )
    else:
        response = send_file(
            zip_path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/zip"
        )
        if cleanup_dir:
            response.call_on_close(lambda: shutil.rmtree(cleanup_dir, ignore_errors=True))
        return response


@app.route("/api/kaggle/delete", methods=["POST"])
def api_kaggle_delete():
    """Permanently deletes a job's kernel from the person's own Kaggle
    account. This is what makes deleting a job in "My Jobs" stick - without
    an actual delete on Kaggle's side, list_jobs() would simply find the
    same kernel again and re-add it the next time this account signs in."""
    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = _resolve_kaggle_credentials(kaggle_username, kaggle_key)
    job_id = (data.get("job_id") or "").strip()

    if not kaggle_username or not kaggle_key or not job_id:
        return error_response("Missing username, API key, or job id.")
    if not kaggle_runner.is_valid_job_id(job_id):
        return error_response("That job id doesn't look like one of this site's jobs.")

    try:
        kaggle_runner.delete_job(kaggle_username, kaggle_key, job_id)
        return jsonify({"ok": True})
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        log.error("kaggle CLI unavailable:\n%s", traceback.format_exc())
        return error_response(str(exc), 503)
    except Exception as exc:  # noqa: BLE001
        log.error("api_kaggle_delete failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to delete the job from Kaggle: {exc}", 502)


@app.route("/api/kaggle/extract-opt-coords", methods=["POST"])
def api_kaggle_extract_opt_coords():
    """Delegates to the shared service (comment-safe Opt gate + convergence
    evidence + final-geometry validation live in ONE implementation)."""
    data = request.get_json(force=True, silent=True) or {}
    payload, status = kaggle_service.extract_opt_coords(
        data.get("kaggle_username") or "", data.get("kaggle_key") or "",
        data.get("job_id") or "")
    return jsonify(payload), status

@app.route("/api/orca/engine/status", methods=["GET"])
def api_orca_engine_status():
    """Returns availability and version of the ORCA quantum chemistry engine."""
    return jsonify({
        "ok": True,
        "available": ORCA_ENGINE_AVAILABLE,
        "version": "1.0.3" if ORCA_ENGINE_AVAILABLE else None,
        "features": [
            "streaming_output_parser",
            "3d_structure_geometry",
            "orbital_energies_homo_lumo",
            "conceptual_dft_reactivity",
            "vibrational_frequencies_ts",
            "uvvis_gaussian_convolution",
            "reaction_thermochemistry",
            "bond_dissociation_energies",
        ] if ORCA_ENGINE_AVAILABLE else [],
    })



# ─────────────────────────────────────────────────────────────
# Ephemeral Session & Automatic 30-Minute File Cleanup Manager
# ─────────────────────────────────────────────────────────────
SESSION_TTL_SECONDS = 30 * 60  # 30 minutes
ACTIVE_SESSIONS: dict[str, dict] = {}
_session_lock = threading.Lock()
UPLOADS_BASE_DIR = os.path.realpath(os.path.join(tempfile.gettempdir(), "orca_weblab_ephemeral"))
os.makedirs(UPLOADS_BASE_DIR, exist_ok=True)

SAFE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
MAX_ARCHIVE_FILE_COUNT = 500
MAX_ARCHIVE_TOTAL_SIZE = 500 * 1024 * 1024  # 500 MB max uncompressed


def sanitize_session_id(session_id: str | None) -> str:
    """Validate and sanitize session_id to prevent path traversal and directory injection."""
    if not session_id or not isinstance(session_id, str):
        return f"sess_{int(time.time()*1000)}_{os.urandom(6).hex()}"
    clean = session_id.strip()
    if not SAFE_SESSION_ID_RE.match(clean):
        return f"sess_{int(time.time()*1000)}_{os.urandom(6).hex()}"
    return clean


def get_safe_session_dir(session_id: str) -> str:
    """Return canonical session directory strictly contained within UPLOADS_BASE_DIR."""
    clean_id = sanitize_session_id(session_id)
    base_real = os.path.realpath(UPLOADS_BASE_DIR)
    target_real = os.path.realpath(os.path.join(base_real, clean_id))
    if not target_real.startswith(base_real + os.sep) or target_real == base_real:
        raise ValueError(f"Session path escape attempt: {session_id!r}")
    return target_real


def track_session_activity(session_id: str | None = None, temp_dir: str | None = None) -> str:
    """Register or refresh session activity timestamp and track temporary directories."""
    clean_id = sanitize_session_id(session_id)
    with _session_lock:
        if clean_id not in ACTIVE_SESSIONS:
            sess_dir = get_safe_session_dir(clean_id)
            os.makedirs(sess_dir, exist_ok=True)
            ACTIVE_SESSIONS[clean_id] = {
                "last_active": time.time(),
                "temp_dirs": [sess_dir],
                "closed": False,
            }
        s = ACTIVE_SESSIONS[clean_id]
        s["last_active"] = time.time()
        if temp_dir:
            base_real = os.path.realpath(UPLOADS_BASE_DIR)
            tdir_real = os.path.realpath(temp_dir)
            if tdir_real.startswith(base_real + os.sep) and tdir_real != base_real and tdir_real not in s["temp_dirs"]:
                s["temp_dirs"].append(tdir_real)
    return clean_id


def close_session(session_id: str):
    """Mark session as closed when user leaves or closes window."""
    clean_id = sanitize_session_id(session_id)
    with _session_lock:
        if clean_id in ACTIVE_SESSIONS:
            ACTIVE_SESSIONS[clean_id]["closed"] = True
            ACTIVE_SESSIONS[clean_id]["last_active"] = time.time()


def _janitor_sweep_expired_sessions():
    """Background daemon thread sweeping expired session files every 60 seconds."""
    base_real = os.path.realpath(UPLOADS_BASE_DIR)
    while True:
        try:
            time.sleep(60)
            now = time.time()
            expired_sessions = []
            with _session_lock:
                for sid, data in list(ACTIVE_SESSIONS.items()):
                    if now - data["last_active"] > SESSION_TTL_SECONDS:
                        expired_sessions.append(sid)

            for sid in expired_sessions:
                with _session_lock:
                    data = ACTIVE_SESSIONS.pop(sid, None)
                if data:
                    for tdir in data.get("temp_dirs", []):
                        try:
                            tdir_real = os.path.realpath(tdir)
                            # Hard containment invariant: strictly inside UPLOADS_BASE_DIR and not root base
                            if tdir_real.startswith(base_real + os.sep) and tdir_real != base_real and os.path.exists(tdir_real):
                                shutil.rmtree(tdir_real, ignore_errors=True)
                                log.info("Janitor cleaned up 30-min expired session directory: %s", tdir_real)
                        except Exception as e:
                            log.warning("Janitor error removing %s: %s", tdir, e)
        except Exception as e:
            log.warning("Janitor sweeper error: %s", e)


_janitor_thread = threading.Thread(target=_janitor_sweep_expired_sessions, daemon=True)
_janitor_thread.start()


def extract_calculation_files_from_archive(raw_bytes: bytes, filename: str, target_dir: str) -> list[dict]:
    """Extract ORCA output files (.out, .log, .property.txt, .xyz, .molden) from ZIP, RAR, or TAR archives.
    
    Hardened against Zip Slip, Tar Slip, symlink traversal, and decompression bomb attacks.
    """
    extracted_files = []
    ext = os.path.splitext(filename)[1].lower()
    fn_lower = filename.lower()
    target_real = os.path.realpath(target_dir)
    os.makedirs(target_real, exist_ok=True)
    
    analyzable_extensions = (".out", ".log", ".property.txt", ".xyz", ".molden", ".dat")
    ignored_patterns = ("__macosx", "desktop.ini", "thumbs.db", ".tmp", ".bin", ".densities")

    def _is_valid_member(m_name: str) -> bool:
        if not m_name or "\0" in m_name:
            return False
        # Prevent absolute or directory-traversing names
        if m_name.startswith(("/", "\\")) or ".." in m_name.replace("\\", "/").split("/"):
            return False
        nl = m_name.lower()
        if any(ig in nl for ig in ignored_patterns):
            return False
        return nl.endswith(analyzable_extensions) or nl.endswith(".molden.input") or (nl.endswith(".txt") and ("orca" in nl or "calc" in nl or "prop" in nl))

    def _safe_write_member(m_name: str, data: bytes) -> str | None:
        base_name = os.path.basename(m_name)
        if not base_name or ".." in base_name or "\0" in base_name:
            return None
        dest_path = os.path.realpath(os.path.join(target_real, base_name))
        if not dest_path.startswith(target_real + os.sep) or dest_path == target_real:
            return None
        with open(dest_path, "wb") as fh:
            fh.write(data)
        return dest_path

    total_uncompressed_bytes = 0

    # 1. ZIP Archives
    if ext in (".zip", ".jar") or fn_lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
                for item in zf.infolist():
                    if len(extracted_files) >= MAX_ARCHIVE_FILE_COUNT:
                        log.warning("Archive reached maximum file count (%d)", MAX_ARCHIVE_FILE_COUNT)
                        break
                    if item.is_dir():
                        continue
                    # Reject symlinks in zip (external_attr high 16 bits contains POSIX mode 0o120000)
                    if (item.external_attr >> 16) & 0o120000 == 0o120000:
                        continue
                    if _is_valid_member(item.filename):
                        total_uncompressed_bytes += item.file_size
                        if total_uncompressed_bytes > MAX_ARCHIVE_TOTAL_SIZE:
                            log.warning("Archive exceeded maximum uncompressed size (%d bytes)", MAX_ARCHIVE_TOTAL_SIZE)
                            break
                        try:
                            content_bytes = zf.read(item)
                            dest_path = _safe_write_member(item.filename, content_bytes)
                            if not dest_path:
                                continue
                            text = content_bytes.decode("utf-8", errors="replace")
                            extracted_files.append({
                                "filename": item.filename,
                                "basename": os.path.basename(item.filename),
                                "content": text,
                                "size_bytes": len(content_bytes),
                            })
                        except Exception as exc:
                            log.warning("Could not extract %s from zip: %s", item.filename, exc)
        except Exception as exc:
            log.warning("ZIP extraction failed: %s", exc)

    # 2. TAR / TAR.GZ / TGZ Archives
    elif ext in (".tar", ".gz", ".tgz", ".xz", ".bz2") or fn_lower.endswith((".tar.gz", ".tar.xz", ".tar.bz2")):
        try:
            with tarfile.open(fileobj=io.BytesIO(raw_bytes)) as tf:
                for member in tf.getmembers():
                    if len(extracted_files) >= MAX_ARCHIVE_FILE_COUNT:
                        log.warning("Archive reached maximum file count (%d)", MAX_ARCHIVE_FILE_COUNT)
                        break
                    # Strict validation: must be regular file, not symlink or hardlink
                    if not member.isfile() or member.issym() or member.islnk():
                        continue
                    if _is_valid_member(member.name):
                        total_uncompressed_bytes += member.size
                        if total_uncompressed_bytes > MAX_ARCHIVE_TOTAL_SIZE:
                            log.warning("Archive exceeded maximum uncompressed size (%d bytes)", MAX_ARCHIVE_TOTAL_SIZE)
                            break
                        try:
                            f_obj = tf.extractfile(member)
                            if f_obj:
                                content_bytes = f_obj.read()
                                dest_path = _safe_write_member(member.name, content_bytes)
                                if not dest_path:
                                    continue
                                text = content_bytes.decode("utf-8", errors="replace")
                                extracted_files.append({
                                    "filename": member.name,
                                    "basename": os.path.basename(member.name),
                                    "content": text,
                                    "size_bytes": len(content_bytes),
                                })
                        except Exception as exc:
                            log.warning("Could not extract %s from tar: %s", member.name, exc)
        except Exception as exc:
            log.warning("Tar extraction error: %s", exc)

    # 3. RAR Archives
    elif ext == ".rar" or fn_lower.endswith(".rar"):
        if RAR_AVAILABLE:
            try:
                with rarfile.RarFile(io.BytesIO(raw_bytes)) as rf:
                    for item in rf.infolist():
                        if len(extracted_files) >= MAX_ARCHIVE_FILE_COUNT:
                            break
                        if item.is_dir() or getattr(item, "is_symlink", lambda: False)():
                            continue
                        if _is_valid_member(item.filename):
                            total_uncompressed_bytes += getattr(item, "file_size", 0)
                            if total_uncompressed_bytes > MAX_ARCHIVE_TOTAL_SIZE:
                                break
                            try:
                                content_bytes = rf.read(item)
                                dest_path = _safe_write_member(item.filename, content_bytes)
                                if not dest_path:
                                    continue
                                text = content_bytes.decode("utf-8", errors="replace")
                                extracted_files.append({
                                    "filename": item.filename,
                                    "basename": os.path.basename(item.filename),
                                    "content": text,
                                    "size_bytes": len(content_bytes),
                                })
                            except Exception as exc:
                                log.warning("Could not extract %s from rar: %s", item.filename, exc)
            except Exception as exc:
                log.info("rarfile extraction failed: %s", exc)

    # 4. Universal fallback: if single file was sent or raw text contains ORCA calculations
    if not extracted_files and raw_bytes and len(raw_bytes) <= MAX_ARCHIVE_TOTAL_SIZE:
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
            if "ORCA" in text or "FINAL SINGLE POINT ENERGY" in text or "Program Version" in text:
                extracted_files.append({
                    "filename": filename,
                    "basename": os.path.basename(filename),
                    "content": text,
                    "size_bytes": len(raw_bytes),
                })
        except Exception:
            pass

    return extracted_files


@app.route("/api/session/heartbeat", methods=["POST"])
def api_session_heartbeat():
    """Client heartbeat to refresh 30-minute session TTL."""
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    sid = track_session_activity(session_id)
    return jsonify({"ok": True, "session_id": sid, "ttl_seconds": SESSION_TTL_SECONDS})


@app.route("/api/session/leave", methods=["POST"])
def api_session_leave():
    """Client beacon on page unload to begin 30-minute countdown for auto-cleanup."""
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    if session_id:
        close_session(session_id)
    return jsonify({"ok": True})


@app.route("/api/orca/engine/parse", methods=["POST"])
def api_orca_engine_parse():
    """Parse raw ORCA output text, uploaded .out/.log file, or compressed archive (.zip, .rar, .tar.gz)."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not installed or available on this instance.", 503)

    session_id = request.headers.get("X-Session-ID") or request.form.get("session_id") or ""
    session_id = track_session_activity(session_id)
    session_dir = os.path.join(UPLOADS_BASE_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    raw_text = ""
    name = "molecule"
    archive_entries = []
    is_archive = False

    if "file" in request.files:
        f = request.files["file"]
        if f.filename:
            raw_bytes = f.read()
            ext = os.path.splitext(f.filename)[1].lower()
            name = core.safe_filename(os.path.splitext(f.filename)[0])

            if ext in (".zip", ".rar", ".tar", ".gz", ".tgz", ".xz", ".bz2") or f.filename.lower().endswith((".tar.gz", ".tar.xz")):
                is_archive = True
                extracted = extract_calculation_files_from_archive(raw_bytes, f.filename, session_dir)
                if not extracted:
                    return error_response(
                        f"Archive '{f.filename}' was uploaded, but no recognized calculation output files (.out, .log, .property.txt) were found inside.",
                        422
                    )

                # Parse all valid calculations inside the archive
                for item in extracted:
                    try:
                        parsed_jobs = OrcaParser(io.StringIO(item["content"]), source_name=item["basename"]).parse()
                        if parsed_jobs:
                            entry_mol_name = core.safe_filename(os.path.splitext(item["basename"])[0])
                            entry_mol = MoleculeData(name=entry_mol_name, jobs=parsed_jobs, sources=[item["filename"]])
                            archive_entries.append({
                                "filename": item["filename"],
                                "basename": item["basename"],
                                "raw_text": item["content"],
                                "molecule": molecule_to_web_json(entry_mol),
                                "jobs_count": len(parsed_jobs),
                                "latest_job": job_to_web_json(parsed_jobs[-1], entry_mol_name, len(parsed_jobs)),
                            })
                    except Exception as e:
                        log.info("Skipping non-calculation file %s: %s", item["filename"], e)

                if not archive_entries:
                    return error_response(
                        f"Calculation files were found in '{f.filename}', but none could be parsed as valid ORCA outputs.",
                        422
                    )

                # Target file index or default to first valid calculation
                target_file = request.form.get("target_file")
                selected_entry = next((e for e in archive_entries if e["filename"] == target_file or e["basename"] == target_file), archive_entries[0])

                return jsonify({
                    "ok": True,
                    "session_id": session_id,
                    "is_archive": True,
                    "archive_filename": f.filename,
                    "archive_entries": [
                        {"filename": e["filename"], "basename": e["basename"], "molecule_name": e["molecule"]["name"], "jobs_count": e["jobs_count"]}
                        for e in archive_entries
                    ],
                    "selected_file": selected_entry["filename"],
                    "raw_text": selected_entry["raw_text"],
                    "name": selected_entry["molecule"]["name"],
                    "molecule": selected_entry["molecule"],
                    "jobs_count": selected_entry["jobs_count"],
                    "latest_job": selected_entry["latest_job"],
                    "cleanup_note": "Uploaded archive files will be automatically wiped from this server 30 minutes after your session ends.",
                })
            else:
                raw_text = raw_bytes.decode("utf-8", errors="replace")
    else:
        data = request.get_json(force=True, silent=True) or {}
        raw_text = (data.get("content") or "").strip()
        name = core.safe_filename(data.get("name") or "molecule")
        if data.get("session_id"):
            session_id = track_session_activity(data.get("session_id"))

    if not raw_text:
        return error_response("No ORCA output text or file content provided.")

    try:
        if "[atoms]" in raw_text.lower() or name.lower().endswith((".molden", ".input")):
            from orca_engine.io import parse_molden_text
            mol = parse_molden_text(raw_text, name=name)
            jobs = mol.jobs if mol else []
        else:
            jobs = OrcaParser(io.StringIO(raw_text), source_name=name).parse()

        if not jobs:
            return error_response(
                "Could not find any recognized ORCA calculation or Molden data in this file. "
                "Ensure this is a valid ORCA output file (.out, .log) or Molden file (.molden).",
                422
            )


        molecule = MoleculeData(name=name, jobs=jobs, sources=[name])
        web_bundle = molecule_to_web_json(molecule)

        return jsonify({
            "ok": True,
            "session_id": session_id,
            "is_archive": False,
            "raw_text": raw_text,
            "name": name,
            "molecule": web_bundle,
            "jobs_count": len(jobs),
            "jobs": [job_to_web_json(j, name, idx) for idx, j in enumerate(jobs, 1)],
            "latest_job": job_to_web_json(jobs[-1], name, len(jobs)),
            "cleanup_note": "Uploaded files are held in ephemeral storage and automatically wiped 30 minutes after your session ends.",
        })
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_parse failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to parse ORCA calculation: {exc}", 500)


@app.route("/api/orca/engine/convolute", methods=["POST"])
def api_orca_engine_convolute():
    """Calculate Gaussian spectral convolution with customizable broadening and shift."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    data = request.get_json(force=True, silent=True) or {}
    tddft_cm = data.get("tddft_cm") or []
    tddft_fosc = data.get("tddft_fosc") or []
    transitions = data.get("transitions") or []

    if (not tddft_cm or not tddft_fosc) and transitions:
        tddft_cm = []
        tddft_fosc = []
        for t in transitions:
            cm = t.get("energy_cm") or (1.0e7 / t.get("wavelength_nm") if t.get("wavelength_nm") else None)
            fosc = t.get("oscillator_strength", 0.0)
            if cm and float(cm) > 0:
                tddft_cm.append(float(cm))
                tddft_fosc.append(float(fosc))

    try:
        sigma_nm = float(data.get("sigma_nm", 20.0))
        shift_nm = float(data.get("shift_nm") if data.get("shift_nm") is not None else data.get("wavelength_shift_nm", 0.0))
        start_nm = float(data.get("start_nm", 180.0))
        end_nm = float(data.get("end_nm", 800.0))
        step_nm = float(data.get("step_nm", 1.0))
    except (TypeError, ValueError):
        return error_response("Invalid numeric parameters for spectral convolution.")

    if not tddft_cm or not tddft_fosc:
        return error_response("No TD-DFT transition states provided.")

    try:
        spectrum = convolute_uvvis_spectrum(
            tddft_cm=tddft_cm,
            tddft_fosc=tddft_fosc,
            wavelength_shift_nm=shift_nm,
            sigma_nm=sigma_nm,
            start_nm=start_nm,
            end_nm=end_nm,
            step_nm=step_nm,
        )
        return jsonify({
            "ok": True,
            "spectrum": spectrum,
            "points_count": len(spectrum),
            "parameters": {
                "sigma_nm": sigma_nm,
                "wavelength_shift_nm": shift_nm,
                "shift_nm": shift_nm,
                "start_nm": start_nm,
                "end_nm": end_nm,
                "step_nm": step_nm,
            },
        })
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_convolute failed:\n%s", traceback.format_exc())
        return error_response(f"Convolution calculation failed: {exc}", 500)


@app.route("/api/orca/engine/experimental-spectrum/inspect", methods=["POST"])
def api_orca_engine_experimental_inspect():
    """Inspect structure, sheets, and preview headers of an experimental spectrum file."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return error_response("No file uploaded for inspection.", 400)

    try:
        content = uploaded.read()
        res = inspect_experimental_file(content, uploaded.filename)
        return jsonify({"ok": True, **res})
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_experimental_inspect failed:\n%s", traceback.format_exc())
        return error_response(f"Inspection failed: {exc}", 500)


@app.route("/api/orca/engine/experimental-spectrum/parse", methods=["POST"])
@app.route("/api/orca/engine/ir-spectrum/parse-experimental", methods=["POST"])
def api_orca_engine_experimental_parse():

    """Parse an experimental UV-Vis absorption spectrum (.txt, .csv, .xlsx).
    
    Preserves raw measurement points and computes cryptographic SHA-256 hash
    for immutable reference tracking.
    """
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    is_form = bool(request.files or request.form)
    if is_form:
        uploaded = request.files.get("file")
        raw_text = request.form.get("raw_text")
        file_name = request.form.get("file_name") or (uploaded.filename if uploaded else "experimental_spectrum.txt")
        sheet_name = request.form.get("sheet_name") or None
        wavelength_col = request.form.get("wavelength_col") or None
        absorbance_col = request.form.get("absorbance_col") or None
        absorbance_cols = request.form.getlist("absorbance_cols") or request.form.get("absorbance_cols") or None
        delimiter = request.form.get("delimiter") or None
    else:
        data = request.get_json(force=True, silent=True) or {}
        uploaded = None
        raw_text = data.get("raw_text")
        file_name = data.get("file_name", "experimental_spectrum.txt")
        sheet_name = data.get("sheet_name")
        wavelength_col = data.get("wavelength_col")
        absorbance_col = data.get("absorbance_col")
        absorbance_cols = data.get("absorbance_cols")
        delimiter = data.get("delimiter")

    try:
        ext = os.path.splitext(file_name.lower())[1]
        spectra_objs = []
        target_y_cols = absorbance_cols or absorbance_col

        if ext in (".xlsx", ".xlsm", ".xltx") and uploaded:
            file_bytes = uploaded.read()
            spectra_objs = parse_experimental_multi_series_excel(
                file_bytes=file_bytes,
                file_name=file_name,
                sheet_name=sheet_name,
                wavelength_col=wavelength_col,
                absorbance_cols=target_y_cols,
            )
        elif raw_text:
            spectra_objs = parse_experimental_multi_series_text(
                content=raw_text,
                file_name=file_name,
                wavelength_col=wavelength_col,
                absorbance_cols=target_y_cols,
                delimiter=delimiter,
            )
        elif uploaded:
            file_bytes = uploaded.read()
            if ext in (".xlsx", ".xlsm", ".xltx"):
                spectra_objs = parse_experimental_multi_series_excel(
                    file_bytes=file_bytes,
                    file_name=file_name,
                    sheet_name=sheet_name,
                    wavelength_col=wavelength_col,
                    absorbance_cols=target_y_cols,
                )
            else:
                text_content = file_bytes.decode("utf-8", errors="replace")
                spectra_objs = parse_experimental_multi_series_text(
                    content=text_content,
                    file_name=file_name,
                    wavelength_col=wavelength_col,
                    absorbance_cols=target_y_cols,
                    delimiter=delimiter,
                )
        else:
            return error_response("No experimental spectrum file or data text provided.", 400)

        if not spectra_objs:
            return error_response("No valid numeric data series found in file.", 400)

        primary_spectrum = spectra_objs[0]
        total_points = sum(s.points_count for s in spectra_objs)

        return jsonify({
            "ok": True,
            "success": True,
            "spectrum": primary_spectrum.to_dict(),
            "spectra": [s.to_dict() for s in spectra_objs],
            "series_count": len(spectra_objs),
            "points_count": total_points,
            "message": f"Successfully parsed {len(spectra_objs)} series ({total_points} data points) from '{file_name}'.",
        })

    except ExperimentalSpectrumError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_experimental_parse failed:\n%s", traceback.format_exc())
        return error_response(f"Experimental spectrum parsing failed: {exc}", 500)


# ---------------------------------------------------------------------------
# Unified ORCA output import (multi-file) for the IR / UV-Vis overlay studios.
# Reuses the SAME authoritative OrcaParser as the current-calculation path -
# there is no second scientific parser. Uploaded content is treated strictly
# as text/data: it is never executed. Per-file status keeps one invalid file
# from rejecting the whole batch.
ORCA_IMPORT_MAX_FILES = 20
ORCA_IMPORT_MAX_FILE_BYTES = 25 * 1024 * 1024


@app.route("/api/orca/engine/import-orca-output", methods=["POST"])
def api_orca_engine_import_orca_output():
    """Imports one or more ORCA outputs and reports per-file FREQ/IR and
    TD-DFT/UV capabilities with the raw scientific data needed for overlay."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        return error_response("No ORCA output files provided.", 400)
    if len(uploads) > ORCA_IMPORT_MAX_FILES:
        return error_response(
            "Too many files in one import (maximum %d)." % ORCA_IMPORT_MAX_FILES, 413)

    import hashlib

    results = []
    seen_hashes = {}
    for storage in uploads:
        raw_name = os.path.basename((storage.filename or "orca_output.txt").strip()) or "orca_output.txt"
        entry = {"file_name": raw_name}
        content = storage.read(ORCA_IMPORT_MAX_FILE_BYTES + 1)
        if len(content) > ORCA_IMPORT_MAX_FILE_BYTES:
            entry.update({"status": "rejected",
                          "reason": "file exceeds the %d MB per-file import limit"
                                    % (ORCA_IMPORT_MAX_FILE_BYTES // (1024 * 1024))})
            results.append(entry)
            continue
        if not content.strip():
            entry.update({"status": "rejected", "reason": "file is empty"})
            results.append(entry)
            continue
        sha = hashlib.sha256(content).hexdigest()
        entry["raw_hash"] = sha
        entry["size"] = len(content)
        if sha in seen_hashes:
            entry.update({"status": "duplicate",
                          "reason": "content identical to an already-imported file",
                          "duplicate_of": seen_hashes[sha]})
            results.append(entry)
            continue
        seen_hashes[sha] = raw_name

        display_name = os.path.splitext(core.safe_filename(os.path.splitext(raw_name)[0]))[0] or "orca_output"
        entry["display_name"] = display_name
        text = content.decode("utf-8", errors="replace")
        try:
            parsed_jobs = OrcaParser(io.StringIO(text), source_name=display_name).parse() or []
        except Exception as exc:  # noqa: BLE001 - malformed input must not 500
            entry.update({"status": "rejected",
                          "reason": "malformed ORCA output (parser: %s)" % str(exc)[:120]})
            results.append(entry)
            continue

        ir_job = next((j for j in parsed_jobs
                       if getattr(j, "ir_frequencies_cm", None)
                       and getattr(j, "ir_intensities_km_mol", None)), None)
        uv_job = next((j for j in parsed_jobs
                       if getattr(j, "tddft_cm", None) and getattr(j, "tddft_fosc", None)), None)
        capabilities = {"ir": ir_job is not None, "uv": uv_job is not None}
        entry["capabilities"] = capabilities
        if not any(capabilities.values()):
            entry.update({"status": "rejected",
                          "reason": "no usable FREQ/IR or TD-DFT/UV results found "
                                    "(SP-only, Opt-only, or unsupported output)"})
            results.append(entry)
            continue

        entry["status"] = "loaded"
        if ir_job:
            modes = []
            for i, (fc, t2) in enumerate(zip(ir_job.ir_frequencies_cm,
                                             ir_job.ir_intensities_km_mol)):
                try:
                    modes.append({"mode": i + 1, "frequency_cm": float(fc),
                                  "intensity_km_mol": float(t2)})
                except (TypeError, ValueError):
                    continue
            imaginary = sum(1 for m in modes if m["frequency_cm"] < 0)
            entry["ir"] = {
                "modes": modes,
                "imaginary_frequency_count": imaginary or int(getattr(ir_job, "imaginary_frequencies_count", 0) or 0),
                "method": getattr(ir_job, "method", None) or "ORCA DFT",
                "basis_set": getattr(ir_job, "basis_set", None) or "",
                "charge": getattr(ir_job, "charge", None),
                "multiplicity": getattr(ir_job, "multiplicity", None),
                "mode_count": len(modes),
            }
        if uv_job:
            transitions = []
            for i, (cm, fosc) in enumerate(zip(uv_job.tddft_cm, uv_job.tddft_fosc)):
                try:
                    cm_v, fosc_v = float(cm), float(fosc)
                except (TypeError, ValueError):
                    continue
                if cm_v <= 0 or fosc_v < 0:
                    continue
                transitions.append({
                    "state_index": i + 1,
                    "energy_cm": cm_v,
                    "excitation_energy_ev": cm_v * 1.2398419843320026e-4,
                    "wavelength_nm": (1.0e7 / cm_v) if cm_v > 0 else None,
                    "oscillator_strength": fosc_v,
                })
            entry["uv"] = {
                "transitions": transitions,
                "method": getattr(uv_job, "method", None) or "TD-DFT",
                "basis_set": getattr(uv_job, "basis_set", None) or "",
                "charge": getattr(uv_job, "charge", None),
                "multiplicity": getattr(uv_job, "multiplicity", None),
                "transition_count": len(transitions),
            }
        results.append(entry)

    loaded = sum(1 for r in results if r.get("status") == "loaded")
    return jsonify({"ok": True, "results": results, "loaded": loaded,
                    "rejected": sum(1 for r in results if r.get("status") == "rejected"),
                    "duplicates": sum(1 for r in results if r.get("status") == "duplicate")})


@app.route("/api/orca/engine/multi-spectrum/overlay", methods=["POST"])
def api_orca_engine_multi_spectrum_overlay():
    """Generate multi-spectrum overlay comparing immutable experimental references with theoretical models."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    data = request.get_json(force=True, silent=True) or {}
    exp_list_raw = data.get("experimental_spectra") or []
    theo_list_raw = data.get("theoretical_spectra") or []
    normalize_mode = data.get("normalize_mode", "none")
    common_range_only = bool(data.get("common_range_only", False))

    exp_objects: list[ExperimentalSpectrum] = []
    for item in exp_list_raw:
        if isinstance(item, dict):
            pts_raw = item.get("points") or item.get("raw_data") or []
            pts: list[tuple[float, float]] = []
            for p in pts_raw:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append((float(p[0]), float(p[1])))
                elif isinstance(p, dict):
                    wl = p.get("wavelength_nm", p.get("x", 0.0))
                    abs_val = p.get("absorbance", p.get("y", p.get("intensity", 0.0)))
                    pts.append((float(wl), float(abs_val)))
            
            raw_hash = item.get("raw_hash") or compute_raw_data_hash(pts)
            exp_objects.append(ExperimentalSpectrum(
                file_name=item.get("file_name", "experimental_spectrum.txt"),
                sheet_name=item.get("sheet_name"),
                column_wavelength=item.get("column_wavelength", "Wavelength"),
                column_absorbance=item.get("column_absorbance", "Absorbance"),
                units_wavelength=item.get("units_wavelength", "nm"),
                units_y=item.get("units_y", "AU"),
                y_quantity=item.get("y_quantity", "absorbance"),
                raw_data=tuple(pts),
                raw_hash=raw_hash,
                detected_peaks=tuple(item.get("detected_peaks", ())),
            ))

    try:
        overlay = build_multi_spectrum_overlay(
            experimental_spectra=exp_objects,
            theoretical_spectra=theo_list_raw,
            normalize_mode=normalize_mode,
            common_range_only=common_range_only,
        )
        return jsonify(overlay)
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_multi_spectrum_overlay failed:\n%s", traceback.format_exc())
        return error_response(f"Multi-spectrum overlay generation failed: {exc}", 500)


# ===========================================================================
# NMR Spectrum Analyzer Endpoints
# ===========================================================================

@app.route("/api/orca/engine/nmr/parse", methods=["POST"])
def api_orca_engine_nmr_parse():
    """Parse NMR chemical shielding calculation output and return structured spectrum data."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    is_form = bool(request.files or request.form)
    if is_form:
        uploaded = request.files.get("file")
        content = uploaded.read().decode("utf-8", errors="replace") if uploaded else (request.form.get("content") or "")
        ref_1h = float(request.form.get("reference_1h")) if request.form.get("reference_1h") not in (None, "") else None
        ref_13c = float(request.form.get("reference_13c")) if request.form.get("reference_13c") not in (None, "") else None
        group_peaks = request.form.get("group_peaks", "false").lower() in ("1", "true", "yes")
        group_tol = float(request.form.get("group_tolerance_ppm", 0.02))
    else:
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content") or data.get("output_text") or ""
        ref_1h = float(data.get("reference_1h")) if data.get("reference_1h") is not None else None
        ref_13c = float(data.get("reference_13c")) if data.get("reference_13c") is not None else None
        group_peaks = bool(data.get("group_peaks", False))
        group_tol = float(data.get("group_tolerance_ppm", 0.02))

    if not content.strip():
        return error_response("No ORCA output text or file provided for NMR analysis.", 400)

    if len(content) > 30 * 1024 * 1024:
        return error_response("NMR payload exceeds maximum allowed size of 30 MB.", 413)

    try:
        nmr_dict = parse_nmr_output(
            content,
            reference_1h=ref_1h,
            reference_13c=ref_13c,
            group_close_peaks=group_peaks,
            group_tolerance_ppm=group_tol,
        )
        return jsonify({
            "ok": True,
            "nmr": nmr_dict,
            "reference_catalog": NMR_REFERENCE_CATALOG,
        })
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_nmr_parse failed:\n%s", traceback.format_exc())
        return error_response(f"NMR calculation parsing failed: {exc}", 500)


@app.route("/api/orca/engine/nmr/spectrum", methods=["POST"])
def api_orca_engine_nmr_spectrum():
    """Recalculate NMR spectrum with new reference shielding or peak coalescence options."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    data = request.get_json(force=True, silent=True) or {}
    raw_atoms = data.get("atoms") or []
    nucleus = str(data.get("nucleus", "1H"))
    ref_val = data.get("reference_shielding")
    ref_method = str(data.get("reference_method", "Custom"))
    ref_basis = data.get("reference_basis")
    group_peaks = bool(data.get("group_peaks", False))
    group_tol = float(data.get("group_tolerance_ppm", 0.02))

    atoms = [NMRAtomRecord.from_dict(a) for a in raw_atoms]
    nuc_enum = NMRNucleus(nucleus) if nucleus in ("1H", "13C") else NMRNucleus.H1

    reference = None
    if ref_val is not None and str(ref_val).strip() != "":
        try:
            reference = NMRReference(
                nucleus=nuc_enum,
                reference_shielding=float(ref_val),
                reference_method=ref_method,
                reference_basis=ref_basis,
                reference_source="User Configured",
            )
        except (ValueError, TypeError):
            reference = None

    try:
        spectrum = build_nmr_spectrum(
            atoms,
            nucleus=nuc_enum,
            reference=reference,
            group_close_peaks=group_peaks,
            group_tolerance_ppm=group_tol,
            method=str(data.get("method", "Unknown")),
            basis_set=str(data.get("basis_set", "Unknown")),
            solvent=str(data.get("solvent", "None")),
            orca_version=str(data.get("orca_version", "Unknown")),
        )
        return jsonify({
            "ok": True,
            "spectrum": spectrum.to_dict(),
        })
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_nmr_spectrum failed:\n%s", traceback.format_exc())
        return error_response(f"NMR spectrum generation failed: {exc}", 500)


@app.route("/api/orca/engine/nmr/export/csv", methods=["POST"])
def api_orca_engine_nmr_export_csv():
    """Export calculated NMR atom shielding and chemical shift data to CSV format."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    data = request.get_json(force=True, silent=True) or {}
    raw_atoms = data.get("atoms") or []
    if not raw_atoms and "spectrum" in data:
        raw_atoms = (data.get("spectrum") or {}).get("atoms") or []

    atoms = [NMRAtomRecord.from_dict(a) for a in raw_atoms]
    csv_str = export_nmr_csv(atoms)

    response = app.response_class(csv_str, mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=calculated_nmr_data.csv"
    return response


@app.route("/api/orca/engine/nmr/export/json", methods=["POST"])
def api_orca_engine_nmr_export_json():
    """Export complete calculated NMR data payload to JSON format."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    data = request.get_json(force=True, silent=True) or {}
    json_str = json.dumps(data, indent=2)

    response = app.response_class(json_str, mimetype="application/json")
    response.headers["Content-Disposition"] = "attachment; filename=calculated_nmr_data.json"
    return response


@app.route("/api/orca/engine/thermochemistry", methods=["POST"])
def api_orca_engine_thermochemistry():
    """Evaluate reaction Delta H, Delta G, Delta S, and K_eq from parsed ORCA outputs."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    # Check if multipart FormData or JSON
    is_form = bool(request.files or request.form)
    if is_form:
        equation = (request.form.get("equation") or "").strip()
        reactants_json = request.form.get("reactants_json")
        products_json = request.form.get("products_json")
        archive_file = request.files.get("archive")
        session_id = request.headers.get("X-Session-ID") or request.form.get("session_id") or ""
        session_id = track_session_activity(session_id)
        session_dir = os.path.join(UPLOADS_BASE_DIR, session_id)
        os.makedirs(session_dir, exist_ok=True)
    else:
        data = request.get_json(force=True, silent=True) or {}
        equation = (data.get("equation") or "").strip()
        reactants_json = data.get("reactants")
        products_json = data.get("products")
        molecules_raw = data.get("molecules") or {}
        archive_file = None

    parsed_molecules: dict[str, MoleculeData] = {}

    # If archive was supplied
    if archive_file and archive_file.filename:
        raw_b = archive_file.read()
        extracted = extract_calculation_files_from_archive(raw_b, archive_file.filename, session_dir)
        for item in extracted:
            clean_name = core.safe_filename(os.path.splitext(item["basename"])[0])
            try:
                jobs = OrcaParser(io.StringIO(item["content"]), source_name=clean_name).parse()
                if jobs:
                    parsed_molecules[clean_name] = MoleculeData(name=clean_name, jobs=jobs, sources=[item["filename"]])
                    parsed_molecules[item["basename"]] = parsed_molecules[clean_name]
            except Exception:
                pass

    # If structured reactants/products arrays were sent
    r_terms: list[ReactionTerm] = []
    p_terms: list[ReactionTerm] = []
    if reactants_json or products_json:
        import json
        r_list = json.loads(reactants_json) if isinstance(reactants_json, str) else (reactants_json or [])
        p_list = json.loads(products_json) if isinstance(products_json, str) else (products_json or [])

        # Build equation if not given
        if not equation:
            r_str = " + ".join(f"{item.get('coefficient', 1)} {item.get('name', 'R')}".strip() for item in r_list)
            p_str = " + ".join(f"{item.get('coefficient', 1)} {item.get('name', 'P')}".strip() for item in p_list)
            equation = f"{r_str} -> {p_str}"

        for item in r_list:
            sp_name = (item.get("name") or "").strip()
            coeff = float(item.get("coefficient", 1) or 1)
            content = (item.get("content") or "").strip()
            sp_content = (item.get("sp_content") or "").strip()
            if sp_name and content:
                clean_name = core.safe_filename(sp_name)
                jobs = OrcaParser(io.StringIO(content), source_name=clean_name).parse()
                if sp_content:
                    sp_jobs = OrcaParser(io.StringIO(sp_content), source_name=f"{clean_name}_sp").parse()
                    jobs.extend(sp_jobs)
                if jobs:
                    mol_obj = MoleculeData(name=clean_name, jobs=jobs, sources=[sp_name])
                    parsed_molecules[sp_name] = mol_obj
                    parsed_molecules[clean_name] = mol_obj
                    parsed_molecules[sp_name.lower()] = mol_obj
            if sp_name:
                r_terms.append(ReactionTerm(coefficient=coeff, molecule_name=sp_name, raw_text=f"{coeff} {sp_name}"))

        for item in p_list:
            sp_name = (item.get("name") or "").strip()
            coeff = float(item.get("coefficient", 1) or 1)
            content = (item.get("content") or "").strip()
            sp_content = (item.get("sp_content") or "").strip()
            if sp_name and content:
                clean_name = core.safe_filename(sp_name)
                jobs = OrcaParser(io.StringIO(content), source_name=clean_name).parse()
                if sp_content:
                    sp_jobs = OrcaParser(io.StringIO(sp_content), source_name=f"{clean_name}_sp").parse()
                    jobs.extend(sp_jobs)
                if jobs:
                    mol_obj = MoleculeData(name=clean_name, jobs=jobs, sources=[sp_name])
                    parsed_molecules[sp_name] = mol_obj
                    parsed_molecules[clean_name] = mol_obj
                    parsed_molecules[sp_name.lower()] = mol_obj
            if sp_name:
                p_terms.append(ReactionTerm(coefficient=coeff, molecule_name=sp_name, raw_text=f"{coeff} {sp_name}"))

    # If molecules dictionary was sent directly
    if not is_form and isinstance(data.get("molecules"), dict):
        for sp_name, content in data.get("molecules", {}).items():
            clean_name = core.safe_filename(sp_name)
            if isinstance(content, str):
                jobs = OrcaParser(io.StringIO(content), source_name=clean_name).parse()
                if jobs:
                    mol_obj = MoleculeData(name=clean_name, jobs=jobs, sources=[sp_name])
                    parsed_molecules[sp_name] = mol_obj
                    parsed_molecules[clean_name] = mol_obj
                    parsed_molecules[sp_name.lower()] = mol_obj

    if not equation and not (r_terms and p_terms):
        return error_response("Please provide a reaction equation (e.g. '2 H2 + O2 -> 2 H2O').")

    if not parsed_molecules:
        return error_response("No valid ORCA calculation output was provided for the participating species.")

    try:
        # Check if custom gas-phase conditions were requested
        enable_custom = False
        custom_temp_k = None
        custom_press_atm = None
        if not is_form:
            enable_custom = bool(data.get("enable_custom_conditions"))
            if enable_custom and data.get("custom_temperature_k") is not None:
                try:
                    custom_temp_k = float(data.get("custom_temperature_k"))
                    custom_press_atm = float(data.get("custom_pressure_atm") or 1.0)
                except (ValueError, TypeError):
                    pass
        else:
            enable_custom = request.form.get("enable_custom_conditions") in ("true", "1", "on")
            if enable_custom and request.form.get("custom_temperature_k"):
                try:
                    custom_temp_k = float(request.form.get("custom_temperature_k"))
                    custom_press_atm = float(request.form.get("custom_pressure_atm") or 1.0)
                except (ValueError, TypeError):
                    pass

        engine = ThermochemistryEngine(parsed_molecules)
        
        # If explicit reaction terms exist, use Reaction object directly
        if r_terms and p_terms:
            rxn_input = Reaction(equation=equation, reactants=r_terms, products=p_terms)
        else:
            rxn_input = equation

        result = engine.evaluate(
            rxn_input,
            custom_temperature_k=custom_temp_k if enable_custom else None,
            custom_pressure_atm=custom_press_atm if enable_custom else None,
        )

        # Build individual species energetics breakdown for the UI table
        species_breakdown = {}
        for sp_name, mol in list(parsed_molecules.items()):
            best_e = engine._get_best_electronic(sp_name, mol.jobs)
            freq_job = engine._get_best_freq_job(sp_name, mol.jobs) or next(
                (j for j in reversed(mol.jobs) if j.gibbs_free_energy_eh is not None or j.total_enthalpy_eh is not None or j.zpe_eh is not None),
                None
            )
            h_val = engine._get_best_energy(sp_name, EnergyKind.ENTHALPY)
            g_val = engine._get_best_energy(sp_name, EnergyKind.GIBBS)
            zpe_val = freq_job.zpe_eh if freq_job else None
            entropy_val = None
            if freq_job and freq_job.entropy_term_eh is not None and freq_job.metadata.temperature_k and freq_job.metadata.temperature_k > 0:
                entropy_val = (freq_job.entropy_term_eh * PhysConst.HARTREE_TO_KCAL * 1000.0) / freq_job.metadata.temperature_k
            elif freq_job and freq_job.entropy_correction_eh is not None and freq_job.metadata.temperature_k and freq_job.metadata.temperature_k > 0:
                entropy_val = (-freq_job.entropy_correction_eh * PhysConst.HARTREE_TO_KCAL * 1000.0) / freq_job.metadata.temperature_k
            elif freq_job and freq_job.total_entropy_cal_mol_k is not None:
                entropy_val = freq_job.total_entropy_cal_mol_k
            elif freq_job and freq_job.vibrational_entropy_cal_mol_k is not None:
                entropy_val = (
                    (freq_job.vibrational_entropy_cal_mol_k or 0.0) +
                    (freq_job.rotational_entropy_cal_mol_k or 0.0) +
                    (freq_job.translational_entropy_cal_mol_k or 0.0) +
                    (freq_job.electronic_entropy_cal_mol_k or 0.0)
                )

            levels = mol.levels_of_theory()
            is_composite = len(levels) > 1 or (freq_job and best_e is not None and freq_job.e_elec_eh is not None and abs(best_e - freq_job.e_elec_eh) > 1e-6)
            is_electronic_only = bool(best_e is not None and h_val is None and g_val is None)
            
            entry = {
                "e_elec_eh": best_e,
                "zpe_eh": zpe_val,
                "enthalpy_eh": h_val,
                "gibbs_eh": g_val,
                "entropy_cal_mol_k": entropy_val,
                "entropy_j_mol_k": (entropy_val * PhysConst.CAL_TO_JOULE) if entropy_val is not None else None,
                "levels_of_theory": levels,
                "is_composite": is_composite,
                "is_electronic_only": is_electronic_only,
                "temperature_k": freq_job.metadata.temperature_k if freq_job else None,
                "pressure_atm": freq_job.metadata.pressure_atm if freq_job else None,
            }
            # Multi-key index to guarantee matching regardless of naming/spacing differences
            species_breakdown[sp_name] = entry
            species_breakdown[sp_name.strip().lower()] = entry
            species_breakdown[core.safe_filename(sp_name)] = entry
            species_breakdown[re.sub(r"[\s_-]+", "", sp_name.lower())] = entry
            for src in (mol.sources or []):
                species_breakdown[src] = entry
                species_breakdown[os.path.splitext(src)[0]] = entry

        res_dict = reaction_result_to_dict(result)
        res_dict["species_energetics"] = species_breakdown
        res_dict["applied_conditions"] = {
            "temperature_k": result.temperature_k,
            "temperature_source": getattr(result, "temperature_source", "ORCA_OUTPUT"),
            "is_custom": enable_custom,
            "custom_temperature_k": custom_temp_k,
            "custom_pressure_atm": custom_press_atm,
        }

        return jsonify({
            "ok": True,
            "result": res_dict,
            "equation": equation,
        })
    except ReactionParseError as exc:
        return error_response(f"Reaction equation syntax error: {exc}", 400)
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_thermochemistry failed:\n%s", traceback.format_exc())
        return error_response(f"Thermochemistry evaluation failed: {exc}", 500)


@app.route("/api/orca/engine/samples", methods=["GET"])
def api_orca_engine_samples():
    """List and load built-in sample ORCA calculation outputs for interactive demonstration."""
    sample_dir = os.path.join(BASE_DIR, "orca_engine", "data", "orcafile", "orca6.1")
    samples_manifest = [
        {
            "id": "napc9oxcuv",
            "title": "Organic Chromophore (TD-DFT UV-Vis Spectrum)",
            "description": "ORCA 6.1 TD-DFT excited states calculation with UV-Vis absorption spectrum and customizable line broadening.",
            "type": "tddft_uvvis",
            "filename": "napc9oxcuv.out",
        },
        {
            "id": "napo1h",
            "title": "Organic Molecule (Opt & Thermochemistry)",
            "description": "ORCA 6.1 geometry optimization, thermochemistry (ZPE, Enthalpy, Gibbs), and CDFT reactivity indices.",
            "type": "opt_freq",
            "filename": "napo1h.out",
        },
        {
            "id": "nap",
            "title": "Aromatic System (Orbitals & HOMO-LUMO Gap)",
            "description": "ORCA 6.1 electronic structure with frontier molecular orbitals (HOMO/LUMO) and gap evaluation.",
            "type": "sp_orbitals",
            "filename": "nap.out",
        },
    ]

    sample_id = request.args.get("id")
    if not sample_id:
        return jsonify({"ok": True, "samples": samples_manifest})

    target = next((s for s in samples_manifest if s["id"] == sample_id), None)
    if not target:
        return error_response(f"Sample '{sample_id}' not found.", 404)

    file_path = os.path.join(sample_dir, target["filename"])
    if not os.path.isfile(file_path):
        return error_response("Sample file not available on this server.", 404)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()

        jobs = OrcaParser(io.StringIO(content), source_name=target["id"]).parse()
        molecule = MoleculeData(name=target["id"], jobs=jobs, sources=[target["filename"]])

        return jsonify({
            "ok": True,
            "sample_info": target,
            "raw_text": content,
            "molecule": molecule_to_web_json(molecule),
            "jobs": [job_to_web_json(j, target["id"], idx) for idx, j in enumerate(jobs, 1)],
            "latest_job": job_to_web_json(jobs[-1], target["id"], len(jobs)),
        })
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_samples failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to load sample: {exc}", 500)


@app.route("/api/orca/engine/analyze-job", methods=["POST"])
def api_orca_engine_analyze_job():
    """Fetch and parse output files directly from a completed Kaggle/Orchestrator job."""
    if not ORCA_ENGINE_AVAILABLE:
        return error_response("ORCA Quantum Chemistry Engine is not available.", 503)

    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)
    job_id = (data.get("job_id") or "").strip()

    if not kaggle_username or not kaggle_key or not job_id:
        return error_response("Missing username, API key, or job id.")
    if not kaggle_runner.is_valid_job_id(job_id):
        return error_response("That job id doesn't look like one of this site's jobs.")

    cleanup_dir = None
    try:
        if ORCHESTRATOR_AVAILABLE:
            try:
                from orca_orchestrator.credentials import parse as parse_credentials
                from orca_orchestrator.service import get_service
                creds = parse_credentials(kaggle_username, kaggle_key)
                zip_path, cleanup_dir = get_service().fetch_results(creds, job_id)
            except Exception:
                zip_path, cleanup_dir = kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)
        else:
            zip_path, cleanup_dir = kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)

        if not zip_path or not os.path.isfile(zip_path):
            return error_response("No results bundle found on Kaggle for this job yet.", 404)

        # Extract and find all .out or .log files
        out_content = None
        out_name = None
        available_files = []

        with zipfile.ZipFile(zip_path, "r") as zf:
            for item in zf.infolist():
                available_files.append(item.filename)
                if not out_content and item.filename.lower().endswith((".out", ".log", ".property.txt")):
                    out_name = item.filename
                    out_content = zf.read(item).decode("utf-8", errors="replace")

        if not out_content:
            return error_response(
                f"Results bundle was downloaded, but no .out or .log calculation files were found. "
                f"Available files: {', '.join(available_files) or 'none'}",
                422
            )

        jobs = OrcaParser(io.StringIO(out_content), source_name=out_name or job_id).parse()
        if not jobs:
            return error_response(
                f"Calculation output file '{out_name}' was found but contained no recognized ORCA calculation data.",
                422
            )

        mol_name = core.safe_filename(os.path.splitext(os.path.basename(out_name or job_id))[0])
        molecule = MoleculeData(name=mol_name, jobs=jobs, sources=[out_name or job_id])

        return jsonify({
            "ok": True,
            "job_id": job_id,
            "filename": out_name,
            "raw_text": out_content,
            "available_files": available_files,
            "molecule": molecule_to_web_json(molecule),
            "jobs": [job_to_web_json(j, mol_name, idx) for idx, j in enumerate(jobs, 1)],
            "latest_job": job_to_web_json(jobs[-1], mol_name, len(jobs)),
        })
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        log.error("kaggle CLI unavailable:\n%s", traceback.format_exc())
        return error_response(str(exc), 503)
    except Exception as exc:  # noqa: BLE001
        log.error("api_orca_engine_analyze_job failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to analyze job results: {exc}", 502)
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)

