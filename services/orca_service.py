# -*- coding: utf-8 -*-
"""Framework-independent ORCA input generation service.

Extracted verbatim from app.py's /api/orca/generate route so the Flask route
and the FastAPI v1 route share ONE implementation.
"""
import base64

import chem_core as core


def error(code_message):
    return {"ok": False, "error": code_message}


def generate_inputs(data: dict):
    """Validates the generator payload and builds the ORCA 6 input.

    Returns (result, status_code). On success:
    {"ok": True, "input_text", "filename", "file_base64"}.
    """
    try:
        cores = int(data.get("cores", 4))
        ram = int(data.get("ram", 6000))
        charge = int(data.get("charge", 0))
        mult = int(data.get("mult", 1))
    except (TypeError, ValueError):
        return error("Non-numeric value in cores/RAM/charge/multiplicity."), 400

    if not (1 <= cores <= 128):
        return error("Cores must be between 1 and 128."), 400
    if not (100 <= ram <= 64000):
        return error("RAM per core must be between 100 and 64000 MB."), 400
    if not (-10 <= charge <= 10):
        return error("Charge must be between -10 and 10."), 400
    if not (1 <= mult <= 20):
        return error("Multiplicity must be between 1 and 20."), 400

    coords = (data.get("coords") or "").strip()
    if not coords:
        return error("Please provide atomic coordinates (XYZ)."), 400

    payload = {
        "custom_line": (data.get("custom_line") or "").strip() or None,
        "calc_type": data.get("calc_type", "sp"),
        "family": data.get("family"),
        "theory": data.get("theory", ""),
        "basis": data.get("basis", ""),
        "disp": data.get("disp", "none"),
        "ri_type": data.get("ri_type", "none"),
        "scf_conv": data.get("scf_conv", "none"),
        "solv_model": data.get("solv_model", "none"),
        "solvent": data.get("solvent", "Water"),
        "x2c": bool(data.get("x2c")),
        "charge": charge,
        "mult": mult,
        "nroots": int(data.get("nroots", 10)) if data.get("calc_type") == "tddft" else None,
        "cores": cores,
        "ram": ram,
        # MaxDisk is a CONFIGURABLE caller input: an explicit valid value is
        # preserved verbatim into the generated input; when omitted the
        # execution runner applies its configured budget (default 20000 MB)
        # through art.set_maxdisk. See FINAL_SYSTEM_REPAIR_PLAN.md R-1.
        "maxdisk": int(data["maxdisk"]) if data.get("maxdisk") else None,
        "largeprint": bool(data.get("largeprint")),
        "temp": float(data.get("temp", 298.15)),
        "pressure": float(data.get("pressure", 1.0)),
        "coords": coords,
    }
    if payload["custom_line"] and not payload["custom_line"].startswith("!"):
        return error("The custom command line must start with '!'."), 400

    inp_text = core.generate_orca_6_input(payload)

    # Honor the caller's explicit MaxDisk budget in the generated input (the
    # documented contract of this field). Omitted -> the input stays
    # backend-neutral and the execution runner applies its configured default
    # (20000 MB). force=True: an explicit API value overrides anything a
    # pre-existing directive may say; %maxcore is never touched.
    if payload["maxdisk"] is not None:
        from orca_orchestrator import orca_artifacts as _art
        inp_text, _effective_maxdisk, _maxdisk_action = _art.set_maxdisk(
            inp_text, int(payload["maxdisk"]), force=True)

    filename = f"{core.safe_filename(data.get('name') or 'molecule')}_6.inp"
    result = {
        "ok": True,
        "input_text": inp_text,
        "filename": filename,
        "file_base64": base64.b64encode(inp_text.encode("utf-8")).decode("utf-8"),
    }
    return result, 200
