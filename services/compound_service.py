# -*- coding: utf-8 -*-
"""Framework-independent Compound, Structure & Resolution Service."""
from __future__ import annotations

import re
import urllib.request
import urllib.parse
import json
from typing import Any, Dict, List, Optional

try:
    from chem_core import clean_3d_coordinates, smiles_to_xyz, parse_reaction_equation
except ImportError:
    clean_3d_coordinates = None
    smiles_to_xyz = None
    parse_reaction_equation = None


def resolve_pubchem_compound(query: str) -> Dict[str, Any]:
    """Resolves compound name, formula, or SMILES to canonical structure."""
    clean_q = query.strip()
    if not clean_q:
        return {"ok": False, "error": "Empty query", "error_code": "EMPTY_QUERY"}

    # Query PubChem REST API
    encoded = urllib.parse.quote(clean_q)
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{encoded}/JSON"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ChemistryLab/1.0.3"})
        # Fixed HTTPS PubChem authority; only a percent-encoded path varies.
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
            compounds = data.get("PC_Compounds", [])
            if not compounds:
                return {"ok": False, "error": f"No PubChem compound found for '{clean_q}'", "error_code": "NOT_FOUND"}
            c = compounds[0]
            cid = c.get("id", {}).get("id", {}).get("cid")
            return {
                "ok": True,
                "cid": cid,
                "query": clean_q,
                "name": clean_q,
            }
    except Exception as exc:
        return {"ok": False, "error": f"PubChem lookup failed: {exc}", "error_code": "PUBCHEM_ERROR"}
