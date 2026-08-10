# -*- coding: utf-8 -*-
"""
Chemistry Lab — Flask backend.

Two workspaces, each combining tools from the original source bots:
  - Draw Chemistry  : Molecule Explorer + Reaction Drawing
  - ORCA Program     : Input Generator + Kaggle Launcher (+ job status polling)

Runs as a single always-on Flask process, which is exactly what a Hugging
Face Space (Docker SDK) keeps alive.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import shutil
import traceback

from flask import (Flask, abort, after_this_request, jsonify, render_template, request,
                   send_file, send_from_directory, session)

import chem_core as core
import kaggle_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chemistry_tools")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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


app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB uploads cap

#: An RXN file repeats a structure once per unit of its coefficient, so this
#: bounds the file size and keeps the drawing legible.
MAX_COEFFICIENT = 50


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
    from orca_orchestrator.logging_ext import configure as configure_logging

    configure_logging()
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
    return render_template(
        "index.html",
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
        google_client_id=GOOGLE_CLIENT_ID,
    )


@app.errorhandler(413)
def _too_large(_exc):
    """Flask's default 413 is an HTML page; the browser does resp.json() on it
    and shows a JSON parse error instead of "your file is too big"."""
    return jsonify({
        "ok": False,
        "error": ("That upload is larger than the %d MB this site accepts. Put large "
                  "files in a Kaggle Dataset and attach it to the job instead."
                  % (app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))),
    }), 413


@app.route("/health")
def health():
    """Liveness plus a real readiness picture.

    Hugging Face restarts a Space for reasons nobody is watching, so this
    endpoint reports what the orchestrator recovered on boot and what the
    watchdog last did -- enough to tell "up" from "up and actually driving
    jobs" without opening a shell."""
    # The `kaggle` CLI is what every /api/kaggle/* route shells out to, and it
    # is the path the browser actually uses. Reporting only on the orchestrator
    # meant /health could answer {"ok": true} while the CLI was missing from the
    # image and every job submission was returning 502.
    # The CLI is RUN, not merely located: a failed image build leaves the
    # wrapper script on PATH with the package behind it missing, and a file
    # test calls that healthy while every request raises.
    cli = kaggle_runner.cli_health()
    payload = {"ok": cli["ok"], "status": "running",
               "runner": "kaggle_runner (legacy routes) — the path the UI submits through",
               "kaggle_cli": cli.get("version") or cli.get("path") or "MISSING",
               "kaggle_cli_ok": cli["ok"],
               "orchestrator": ORCHESTRATOR_AVAILABLE}
    if not cli["ok"]:
        payload["error"] = (
            "the kaggle command-line tool is not usable (%s), so no job can be submitted, "
            "polled or downloaded. Check that requirements.txt installed cleanly — a pin "
            "that does not exist on PyPI fails the whole image build silently."
            % cli.get("detail", "unknown"))
    if ORCHESTRATOR_AVAILABLE:
        try:
            from orca_orchestrator import get_service

            payload.update(get_service().health())
        except Exception as exc:  # noqa: BLE001
            payload["orchestrator_error"] = str(exc)
            payload["ok"] = False
    return jsonify(payload)


# ─────────────────────────────────────────────────────────────
# Google Sign-In (identity display only — no server-side user database)
# ─────────────────────────────────────────────────────────────
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

        props = None
        wiki = None
        solubility: list[str] = []
        if parsed.kind is core.InputKind.NAME:
            props = core.fetch_pubchem_properties(query)
            if props:
                wiki = core.fetch_wikipedia_summary(props.get("IUPACName") or query) or \
                    core.fetch_wikipedia_summary(query)
                cid = props.get("CID")
                if cid:
                    solubility = core.fetch_solubility(cid)

        payload = {
            "ok": True,
            "input_kind": parsed.kind.name,
            "smiles": smiles,
            "image_png_base64": b64(image_bytes),
            "mol_file_base64": b64(mol_bytes),
            "filename": core.safe_filename(query),
            "formula": (props or {}).get("MolecularFormula"),
            "weight": (props or {}).get("MolecularWeight"),
            "iupac_name": (props or {}).get("IUPACName"),
            "title": (props or {}).get("Title"),
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
    validation is what the "opens in ChemDraw" badge is based on — see
    chem_core.validate_rxn_block for why it is phrased as a format check rather
    than a claim about a program that is not installed here.
    """
    data = request.get_json(force=True, silent=True) or {}
    reactants_str = (data.get("reactants") or "").strip()
    products_str = (data.get("products") or "").strip()
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
        for term in reactant_terms + product_terms:
            if term.name in resolved:
                continue
            smiles, note = core.resolve_species(term.name)
            if smiles:
                resolved[term.name] = smiles
                if note:
                    interpretations.append(note)
            else:
                unresolved.append(term.name)
        if unresolved:
            if not core.pubchem_reachable():
                return error_response(
                    "The PubChem lookup service could not be reached from this server, so "
                    f"{', '.join(unresolved)} could not be resolved. This is not a problem "
                    "with your equation — SMILES strings are resolved locally and still work.",
                    503)
            return error_response("Could not recognize: " + ", ".join(unresolved), 404)

        # An MDL RXN cannot express a fraction, so 1/2 O2 is scaled away — and
        # said out loud, because it changes the numbers the person typed.
        factor, (rxn_reactants, rxn_products) = core.scale_terms_to_integers(
            reactant_terms, product_terms)

        as_pairs = lambda terms: [(t.coefficient, resolved[t.name]) for t in terms]  # noqa: E731
        balance = core.check_reaction_balance(as_pairs(reactant_terms), as_pairs(product_terms))

        # Default on: writing O2 rather than drawing it is what a chemist does.
        small_as_formula = data.get("small_as_formula", True) is not False
        image_bytes = core.render_reaction_png(as_pairs(reactant_terms), as_pairs(product_terms),
                                               small_as_formula=small_as_formula)
        if not image_bytes:
            return error_response("Could not draw the reaction scheme — check the formulas.", 422)

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
                "In the file a coefficient is represented the way MDL defines it — the "
                "structure appears that many times. ChemDraw will show two water molecules "
                "rather than the text '2 H2O'; the stoichiometry is there, just not as a "
                "numeral.")

        return jsonify({
            "ok": True,
            "image_png_base64": b64(image_bytes),
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
            "formula": (props or {}).get("MolecularFormula"),
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


@app.route("/api/orca/generate", methods=["POST"])
def api_orca_generate():
    data = request.get_json(force=True, silent=True) or {}
    try:
        cores = int(data.get("cores", 4))
        ram = int(data.get("ram", 6000))
        charge = int(data.get("charge", 0))
        mult = int(data.get("mult", 1))
    except (TypeError, ValueError):
        return error_response("Non-numeric value in cores/RAM/charge/multiplicity.")

    if not (1 <= cores <= 128):
        return error_response("Cores must be between 1 and 128.")
    if not (100 <= ram <= 64000):
        return error_response("RAM per core must be between 100 and 64000 MB.")
    if not (-10 <= charge <= 10):
        return error_response("Charge must be between -10 and 10.")
    if not (1 <= mult <= 20):
        return error_response("Multiplicity must be between 1 and 20.")

    coords = (data.get("coords") or "").strip()
    if not coords:
        return error_response("Please provide atomic coordinates (XYZ).")

    payload = {
        "custom_line": (data.get("custom_line") or "").strip() or None,
        "calc_type": data.get("calc_type", "sp"),
        "family": data.get("family"),
        "theory": data.get("theory", ""),
        "basis": data.get("basis", ""),
        "disp": data.get("disp", "none"),
        "ri_type": data.get("ri_type", "none"),
        "solv_model": data.get("solv_model", "none"),
        "solvent": data.get("solvent", "Water"),
        "x2c": bool(data.get("x2c")),
        "charge": charge,
        "mult": mult,
        "nroots": int(data.get("nroots", 10)) if data.get("calc_type") == "tddft" else None,
        "cores": cores,
        "ram": ram,
        "coords": coords,
    }
    if payload["custom_line"] and not payload["custom_line"].startswith("!"):
        return error_response("The custom command line must start with '!'.")

    inp_text = core.generate_orca_6_input(payload)
    filename = f"{core.safe_filename(data.get('name') or 'molecule')}_6.inp"
    return jsonify({
        "ok": True,
        "input_text": inp_text,
        "filename": filename,
        "file_base64": b64(inp_text.encode("utf-8")),
    })


# ─────────────────────────────────────────────────────────────
# Kaggle Launcher
# ─────────────────────────────────────────────────────────────
@app.route("/api/kaggle/login", methods=["POST"])
def api_kaggle_login():
    """Verifies the person's Kaggle username + API key/token and, in the
    same call, pulls back the list of jobs this site has previously
    submitted under that account straight from Kaggle. This is the
    recovery path when a browser's local data (and with it the locally
    remembered job list) has been cleared or a different browser/device
    is used — logging back in with the same Kaggle credentials rebuilds
    the job list from Kaggle itself rather than from anything stored on
    this server."""
    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)

    if not kaggle_username or not kaggle_key:
        return error_response("Please enter your Kaggle username and API key/token.")

    try:
        jobs = kaggle_runner.list_jobs(kaggle_username, kaggle_key)
        log.info("kaggle sign-in", extra={"event": "signed_in", "jobs": len(jobs)})
        return jsonify({"ok": True, "jobs": jobs})
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        # 503, not 401. A 401 tells the person their credentials are wrong and
        # sends them to regenerate their token — which cannot help, and which
        # kills every job already running, since each running kernel carries the
        # old token to push its own successor.
        log.error("kaggle CLI unavailable:\n%s", traceback.format_exc())
        return error_response(str(exc), 503)
    except Exception as exc:  # noqa: BLE001
        log.error("api_kaggle_login failed:\n%s", traceback.format_exc())
        return error_response(f"Could not sign in to Kaggle: {exc}", 401)


@app.route("/api/kaggle/submit", methods=["POST"])
def api_kaggle_submit():
    kaggle_username = (request.form.get("kaggle_username") or "").strip()
    kaggle_key = (request.form.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)
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
            "download link to the ORCA archive."
        )

    # A ready-made .inp file upload takes precedence over the textarea content.
    input_file = request.files.get("input_file")
    if input_file and input_file.filename:
        input_filename = input_file.filename
        input_content = input_file.read().decode("utf-8", errors="replace")

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

    # NOTE: intentionally no username segment here — see the long comment on
    # kaggle_runner.list_jobs() for why embedding it used to silently break
    # "sign in from a different browser" for any Kaggle username containing
    # a character (most commonly a hyphen) that core.safe_filename() mangles.
    # The slug doubles as the kernel's Kaggle title on purpose; see
    # kaggle_runner.make_job_base_id().
    title_source = job_name or os.path.splitext(os.path.basename(input_filename))[0]
    job_base_id = kaggle_runner.make_job_base_id(title_source, input_filename)
    # The job's display name, shown in "My Jobs" — the person's own wording,
    # kept by the browser, so it survives even though Kaggle has to show the
    # slug-safe title on its side.
    job_title = kaggle_runner.kaggle_safe_title(title_source, fallback=job_base_id)
    job_dir = None
    try:
        job_dir = kaggle_runner.build_job_dir(
            kaggle_username=kaggle_username,
            kaggle_key=kaggle_key,
            job_base_id=job_base_id,
            input_filename=input_filename,
            files_payload=files_payload,
            dataset_sources=dataset_sources,
            orca_link=orca_link or None,
            job_title=job_title,
        )
        pushed = kaggle_runner.push_job(job_dir, kaggle_username, kaggle_key)
        # The live path emitted no success logging at all, so an operator could
        # not answer "did this user's job get pushed, and when". The redacting
        # formatter is already installed; it just had nothing to log.
        log.info("kaggle job submitted", extra={"event": "job_submitted",
                                                "job_id": pushed["job_id"],
                                                "kaggle_owner": pushed["owner"],
                                                "input_file": input_filename})
        return jsonify({
            "ok": True,
            # Both of these come from the URL Kaggle itself reported for the
            # push, so the link the person clicks and the id the site polls
            # always describe the notebook that actually exists.
            "kaggle_url": pushed["url"],
            "job_id": pushed["job_id"],
            "job_title": job_title,
            "message": "Job submitted to Kaggle successfully. Track progress and results below.",
        })
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        log.error("kaggle CLI unavailable:\n%s", traceback.format_exc())
        return error_response(str(exc), 503)
    except Exception as exc:  # noqa: BLE001
        log.error("api_kaggle_submit failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to submit the job to Kaggle: {exc}", 502)
    finally:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)


@app.route("/api/kaggle/status", methods=["POST"])
def api_kaggle_status():
    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)
    job_id = (data.get("job_id") or "").strip()

    if not kaggle_username or not kaggle_key or not job_id:
        return error_response("Missing username, API key, or job id.")
    if not kaggle_runner.is_valid_job_id(job_id):
        return error_response("That job id doesn't look like one of this site's jobs.")

    try:
        result = kaggle_runner.check_job_status(kaggle_username, kaggle_key, job_id)
        if result.get("next_job_id"):
            log.info("chain advanced", extra={"event": "chain_advanced", "job_id": job_id,
                                              "next_job_id": result["next_job_id"]})
        elif result.get("status") in ("error", "cancelled"):
            log.info("job ended", extra={"event": "job_ended", "job_id": job_id,
                                         "status": result["status"]})
        return jsonify({"ok": True, **result})
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        log.error("kaggle CLI unavailable:\n%s", traceback.format_exc())
        return error_response(str(exc), 503)
    except Exception as exc:  # noqa: BLE001
        log.error("api_kaggle_status failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to check job status: {exc}", 502)


@app.route("/api/kaggle/download", methods=["POST"])
def api_kaggle_download():
    """Fetches a completed job's output directly from Kaggle's own kernel
    storage and streams it to the browser. No third-party upload host is
    involved (that used to be file.io, which occasionally returned
    non-JSON/empty responses and broke the download)."""
    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)
    job_id = (data.get("job_id") or "").strip()

    if not kaggle_username or not kaggle_key or not job_id:
        return error_response("Missing username, API key, or job id.")
    if not kaggle_runner.is_valid_job_id(job_id):
        return error_response("That job id doesn't look like one of this site's jobs.")

    try:
        zip_path, cleanup_dir = kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        log.error("kaggle CLI unavailable:\n%s", traceback.format_exc())
        return error_response(str(exc), 503)
    except Exception as exc:  # noqa: BLE001
        log.error("api_kaggle_download failed:\n%s", traceback.format_exc())
        return error_response(f"Failed to fetch results from Kaggle: {exc}", 502)

    if not zip_path:
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        return error_response(
            "No output files were found for this job yet on Kaggle. "
            "It may still be finishing up — try again in a minute, or check the "
            "notebook directly on kaggle.com.",
            404,
        )

    @after_this_request
    def _cleanup(response):
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        return response

    return send_file(zip_path, as_attachment=True, download_name=f"{job_id}_results.zip")


@app.route("/api/kaggle/delete", methods=["POST"])
def api_kaggle_delete():
    """Permanently deletes a job's kernel from the person's own Kaggle
    account. This is what makes deleting a job in "My Jobs" stick — without
    an actual delete on Kaggle's side, list_jobs() would simply find the
    same kernel again and re-add it the next time this account signs in."""
    data = request.get_json(force=True, silent=True) or {}
    kaggle_username = (data.get("kaggle_username") or "").strip()
    kaggle_key = (data.get("kaggle_key") or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
