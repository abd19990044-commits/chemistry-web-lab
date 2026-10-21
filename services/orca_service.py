# -*- coding: utf-8 -*-
"""Framework-independent ORCA input generation service.

Extracted verbatim from app.py's /api/orca/generate route so the Flask route
and the FastAPI v1 route share ONE implementation.
"""
import base64
import math

import chem_core as core


def error(code_message):
    return {"ok": False, "error": code_message}


def generate_inputs(data: dict):
    """Validates the generator payload and builds the ORCA 6 input.

    Returns (result, status_code). On success:
    {"ok": True, "input_text", "filename", "file_base64"}.
    """
    if not isinstance(data, dict):
        return error("The request body must be a JSON object."), 400

    # Flask callers do not pass through the versioned API's Pydantic schema.
    # Validate at the shared boundary so malformed input never becomes a 500
    # (or an invalid ORCA directive) on either transport.
    string_defaults = {
        "coords": "", "custom_line": "", "calc_type": "sp", "theory": "",
        "basis": "", "disp": "none", "ri_type": "none", "scf_conv": "none",
        "solv_model": "none", "solvent": "Water", "name": "",
    }
    data = dict(data)
    for field, default in string_defaults.items():
        value = data.get(field, default)
        if value is None:
            value = default
        if not isinstance(value, str):
            return error(f"{field} must be a string."), 400
        data[field] = value
    data["calc_type"] = data["calc_type"].strip().lower().replace("_", " ") or "sp"

    def integer(value):
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError("Expected an integer")
        return int(value)

    try:
        cores = integer(data.get("cores", 4))
        ram = integer(data.get("ram", 6000))
        charge = integer(data.get("charge", 0))
        mult = integer(data.get("mult", 1))
    except (TypeError, ValueError, OverflowError):
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

    try:
        nroots = integer(10 if data.get("nroots") is None else data["nroots"]) if data["calc_type"] == "tddft" else None
        maxdisk = integer(data["maxdisk"]) if data.get("maxdisk") not in (None, "") else None
        temperature = float(data.get("temp", 298.15))
        pressure = float(data.get("pressure", 1.0))
    except (TypeError, ValueError, OverflowError):
        return error("Invalid roots, MaxDisk, temperature, or pressure value."), 400
    if nroots is not None and not 1 <= nroots <= 999:
        return error("TD-DFT roots must be between 1 and 999."), 400
    if maxdisk is not None and maxdisk < 1:
        return error("MaxDisk must be a positive integer in MB."), 400
    if not all(math.isfinite(value) and value > 0 for value in (temperature, pressure)):
        return error("Temperature and pressure must be positive finite numbers."), 400

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
        "nroots": nroots,
        "cores": cores,
        "ram": ram,
        # MaxDisk is a CONFIGURABLE caller input: an explicit valid value is
        # preserved verbatim into the generated input; when omitted the
        # execution runner applies its configured budget (default 20000 MB)
        # through art.set_maxdisk. See FINAL_SYSTEM_REPAIR_PLAN.md R-1.
        "maxdisk": maxdisk,
        "largeprint": bool(data.get("largeprint")),
        "temp": temperature,
        "pressure": pressure,
        "coords": coords,
    }
    if payload["custom_line"] and not payload["custom_line"].startswith("!"):
        return error("The custom command line must start with '!'."), 400

    try:
        inp_text = core.generate_orca_6_input(payload)
    except ValueError as exc:
        return error(str(exc)), 400

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
