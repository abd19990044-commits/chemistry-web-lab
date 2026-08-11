# -*- coding: utf-8 -*-
"""Scientific policy layer for the guided ORCA input wizard.

The original generator is intentionally kept as the low-level renderer. This
module is the policy gate in front of it: it rejects combinations that ORCA 6.1
cannot support or that silently mean something different from what the UI says,
and it makes an explicit ``No RI`` choice become the real ORCA ``NORI`` keyword.

Custom command-line inputs are not rewritten here because the user explicitly
opted out of the guided wizard.
"""
from __future__ import annotations

from typing import Callable


# ORCA 6.1: these functionals have dispersion treatment built into the
# functional definition. Adding D3/D3BJ/D4 would describe a different model.
_NL_FUNCTIONALS = {"WB97M-V"}
_BUILTIN_DISPERSION_FUNCTIONALS = {"WB97X-D4"}
_HYBRID_DFT = {"B3LYP", "CAM-B3LYP", "PBE0", "M062X", "WB97M-V", "WB97X-D4"}
_NONHYBRID_DFT = {"PBE", "BP86"}
_COMPOSITE = {"R2SCAN-3C", "B97-3C", "PBEH-3C"}
_DLPNO = {"DLPNO-MP2", "DLPNO-CCSD", "DLPNO-CCSD(T)"}
_CORRELATION_RI = {"RI-MP2"}


class OrcaPolicyError(ValueError):
    """A guided-wizard choice is scientifically or syntactically invalid."""


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def validate_payload(payload: dict) -> None:
    """Validate a guided ORCA wizard payload before it reaches the renderer."""
    if payload.get("custom_line"):
        return

    calc = str(payload.get("calc_type") or "sp").strip().lower()
    family = str(payload.get("family") or "").strip().lower()
    method = _norm(payload.get("theory"))
    basis = str(payload.get("basis") or "").strip()
    disp = _norm(payload.get("disp") or "none")
    ri = _norm(payload.get("ri_type") or "none")

    if family == "f_comp":
        if method not in _COMPOSITE:
            raise OrcaPolicyError("Unknown composite method. Choose a supported ORCA 6 composite method.")
        if disp not in {"NONE", ""}:
            raise OrcaPolicyError(
                f"{method} already contains its parameterized dispersion/BSSE treatment; "
                "do not add D3, D3BJ or D4 separately."
            )
        if ri not in {"NONE", ""}:
            raise OrcaPolicyError(
                f"{method} is a composite 3c method with its own parameterization; "
                "the guided wizard does not add an independent RI switch to it."
            )
        if payload.get("x2c"):
            raise OrcaPolicyError(
                f"X2C is disabled for {method} in the guided wizard. The 3c method has "
                "a parameterized basis/correction model; choose an explicit functional "
                "with a compatible relativistic basis for an X2C calculation."
            )
        return

    if method in _NL_FUNCTIONALS:
        if calc == "tddft":
            raise OrcaPolicyError(
                "TD-DFT is not available for the VV10/non-local wB97M-V functional in ORCA 6.1. "
                "Choose a TD-DFT-compatible functional such as CAM-B3LYP, PBE0 or wB97X-D4."
            )
        if disp not in {"NONE", ""}:
            raise OrcaPolicyError(
                "wB97M-V already includes its fitted VV10 non-local dispersion treatment. "
                "Do not add D3, D3BJ or D4."
            )

    if method in _BUILTIN_DISPERSION_FUNCTIONALS and disp not in {"NONE", ""}:
        raise OrcaPolicyError(
            "wB97X-D4 already denotes the D4-parameterized functional. "
            "Choose None for the separate dispersion selector."
        )

    if family == "f_dft":
        if method in _HYBRID_DFT and ri == "RI":
            raise OrcaPolicyError(
                f"RI is not a valid standalone exchange treatment for hybrid {method}. "
                "Use RIJCOSX, RIJK, or No RI instead."
            )
        if method in _NONHYBRID_DFT and ri == "RIJCOSX":
            raise OrcaPolicyError(
                f"{method} is a non-hybrid DFT functional. Use Standard RI or No RI; "
                "RIJCOSX is not offered for these methods by the guided policy."
            )

    if method in _CORRELATION_RI and ri == "NONE":
        raise OrcaPolicyError(
            f"{method} requires an RI/correlation-fitting treatment. The wizard must "
            "not generate a nominal 'No RI' input for this method."
        )

    # DLPNO methods obligatorily use RI internally. The wizard deliberately
    # skips a separate RI screen for them and the low-level renderer adds
    # AutoAux, so ``ri_type=none`` means "no extra RI selector" rather than
    # "turn RI off".
    if method in _DLPNO and ri not in {"NONE", "RIJCOSX", "RIJK", "RI"}:
        raise OrcaPolicyError(f"Unsupported RI selection for {method}.")

    # The renderer substitutes x2c-* bases for the supported def2 choices.
    if payload.get("x2c") and basis and basis not in {
        "def2-SVP", "def2-TZVP", "def2-TZVPP", "def2-QZVP", "def2-TZVPD", "ma-def2-TZVP"
    }:
        raise OrcaPolicyError(
            f"X2C with {basis} is not supported by the guided wizard. Choose a supported "
            "def2 basis so the matching relativistically recontracted x2c-* basis can be used."
        )


def install_policy() -> None:
    """Patch the shared renderer once, after ``chem_core`` has been imported."""
    import chem_core

    if getattr(chem_core, "_ORCA_POLICY_INSTALLED", False):
        return

    original: Callable[[dict], str] = chem_core.generate_orca_6_input

    def guarded(payload: dict) -> str:
        validate_payload(payload)
        text = original(payload)
        if not payload.get("custom_line") and _norm(payload.get("ri_type") or "none") == "NONE":
            # ORCA 6.1 uses RI by default for DFT. NORI is the explicit opt-out.
            # Canonical MP2 and other methods that do not have the same DFT RI
            # default do not need an artificial NORI token.
            method = _norm(payload.get("theory"))
            family = str(payload.get("family") or "").strip().lower()
            if family in {"f_dft", "f_hf"} and method not in _DLPNO:
                lines = text.splitlines()
                if len(lines) > 1:
                    lines[1] = lines[1].rstrip() + " NORI"
                    text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        return text

    chem_core.generate_orca_6_input = guarded
    chem_core._ORCA_POLICY_INSTALLED = True
