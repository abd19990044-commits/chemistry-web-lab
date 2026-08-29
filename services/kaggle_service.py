# -*- coding: utf-8 -*-
"""Framework-independent Kaggle job services.

Shared by the Flask routes (app.py) and the FastAPI v1 layer (api/) so both
frameworks call the SAME implementation - no duplicate business logic.

Functions here return plain ``(payload, status_code)`` tuples instead of
framework responses; the transport layers wrap them.
"""
import base64
import io
import os
import re
import traceback
import zipfile

from orca_orchestrator.logging_ext import configure  # noqa: F401  (keeps parity with app boot)

import kaggle_runner
from orca_orchestrator.errors import ValidationError as _OrchValidationError
from kaggle_runner import cli_health


class _OwnerDenied(Exception):
    """Raised when the orchestrator refuses a fetch for another user's job."""


log = kaggle_runner.log if hasattr(kaggle_runner, "log") else None

# ---------------------------------------------------------------------------
# Submit idempotency (moved verbatim from app.py - single implementation).
# ---------------------------------------------------------------------------
SUBMIT_DEDUP = {}
SUBMIT_DEDUP_TTL = 1800  # 30 minutes


def _submit_dedup_purge():
    import time
    now = time.time()
    for key in [k for k, (at, _v) in SUBMIT_DEDUP.items() if now - at > SUBMIT_DEDUP_TTL]:
        SUBMIT_DEDUP.pop(key, None)


def submit_dedup_lookup(idem_key):
    if not idem_key:
        return None
    _submit_dedup_purge()
    entry = SUBMIT_DEDUP.get(idem_key)
    return entry[1] if entry else None


def submit_dedup_store(idem_key, response):
    if not idem_key:
        return
    import time
    _submit_dedup_purge()
    SUBMIT_DEDUP[idem_key] = (time.time(), response)


# ---------------------------------------------------------------------------
# Credential resolution (moved from app.py._resolve_kaggle_credentials)
# ---------------------------------------------------------------------------
def resolve_credentials(kaggle_username, kaggle_key):
    """Mirrors app._resolve_kaggle_credentials exactly (clean + vault fallback)."""
    kaggle_username = (kaggle_username or "").strip()
    kaggle_key = (kaggle_key or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)
    if not kaggle_key and kaggle_username:
        try:
            from orca_orchestrator.credential_vault import get_vault_manager
            loaded = get_vault_manager().load_credentials(kaggle_username)
            if loaded:
                kaggle_key = loaded.key or loaded.api_token or ""
        except Exception:
            pass
    return kaggle_username, kaggle_key


def save_credentials(kaggle_username, kaggle_key):
    try:
        from orca_orchestrator.credential_vault import get_vault_manager
        from orca_orchestrator.credentials import parse as parse_credentials
        creds = parse_credentials(kaggle_username, kaggle_key)
        get_vault_manager().save_credentials(creds.username, creds)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def check_status(kaggle_username, kaggle_key, job_id):
    """Returns (payload, status_code)."""
    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key)
    job_id = (job_id or "").strip()
    if not kaggle_username or not kaggle_key or not job_id:
        return {"ok": False, "error": "Missing username, API key, or job id."}, 400
    if not kaggle_runner.is_valid_job_id(job_id):
        return {"ok": False, "error": "That job id doesn't look like one of this site's jobs."}, 400
    try:
        result = kaggle_runner.check_job_status(kaggle_username, kaggle_key, job_id)
        if result.get("next_job_id"):
            pass
        elif result.get("status") in ("error", "cancelled"):
            pass
        return {"ok": True, **result}, 200
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        return {"ok": False, "error": str(exc)}, 503
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "Failed to check job status: %s" % exc}, 502


# ---------------------------------------------------------------------------
# Submission (legacy-path core: validation + build + push + vault save).
# Transport layers (Flask form/files, FastAPI JSON) call this.
# ---------------------------------------------------------------------------
def submit_job(*, kaggle_username, kaggle_key, dataset_sources_raw, orca_link,
               input_filename, input_content, job_name, idem_key=None, store=None,
               extra_files=None, maxdisk_mb=None):
    """Authoritative submission used by BOTH Flask and FastAPI.

    Idempotency is enforced by the ORCHESTRATOR STORE (SQLite, shared across
    workers/processes): begin_idempotent claims the key, complete_idempotent
    persists the response, abandon_idempotent releases it only when the push
    provably did not land. The in-memory Idempotency-Key cache in the Flask
    route is a compatibility layer only - never the sole guarantee.
    """
    import hashlib
    import json as _json

    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key)

    if not kaggle_username or not kaggle_key:
        return {"ok": False, "error": "Please enter your Kaggle username and API key/token."}, 400
    if not dataset_sources_raw and not orca_link:
        return {"ok": False,
                "error": "Provide an ORCA source: either a Kaggle Dataset identifier that holds your own "
                         "licensed ORCA package (example: username/orca-6-1-0), or a Google Drive / direct "
                         "download link."}, 400
    if not input_content.strip():
        return {"ok": False,
                "error": "There is no .inp content to submit. Build one with the Input Generator, "
                         "paste it directly, or upload a ready-made .inp file."}, 400

    import chem_core as core

    # Defensive service-level guard (P2-2): non-Pydantic callers (the legacy
    # Flask route omits the field; future internal callers) must never turn an
    # invalid explicit budget into an invalid ORCA directive. Omitted stays
    # untouched; the FastAPI boundary additionally rejects with 422.
    if maxdisk_mb is not None:
        if isinstance(maxdisk_mb, bool):
            return {"ok": False,
                    "error": "maxdisk_mb must be a positive integer (MB)."}, 400
        try:
            value = float(maxdisk_mb)
            non_integral = value != int(value)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False,
                    "error": "maxdisk_mb must be a positive integer (MB)."}, 400
        if non_integral or int(value) < 1:
            return {"ok": False,
                    "error": "maxdisk_mb must be a positive integer (MB)."}, 400
        maxdisk_mb = int(value)

    dataset_sources = kaggle_runner.clean_dataset_sources(dataset_sources_raw)
    input_filename = core.safe_filename(os.path.splitext(input_filename)[0]) + ".inp"

    # Explicit per-job MaxDisk override (P2-2): force the caller's budget into
    # the canonical input text BEFORE the idempotency identity is computed, so
    # content_sha covers it too. Both execution runners PRESERVE a valid
    # directive, so the value survives every window, successor and checkpoint
    # without coupling MaxDisk to one backend. Omitted -> runners keep their
    # configured default (20000 MB).
    if maxdisk_mb is not None:
        from orca_orchestrator import orca_artifacts as _art
        input_content, _forced_maxdisk_mb, _maxdisk_action = _art.set_maxdisk(
            input_content, int(maxdisk_mb), force=True)

    files_payload = {input_filename: base64.b64encode(input_content.encode("utf-8")).decode("utf-8")}
    for _aux_name, _aux_b64 in (extra_files or {}).items():
        files_payload[_aux_name] = _aux_b64

    title_source = job_name or os.path.splitext(os.path.basename(input_filename))[0]
    job_base_id = kaggle_runner.make_job_base_id(title_source, input_filename)
    job_title = kaggle_runner.kaggle_safe_title(title_source, fallback=job_base_id)

    if store is None:
        from orca_orchestrator.service import get_service
        store = get_service().store

    request_payload = {
        "owner": kaggle_username, "filename": input_filename,
        "content_sha": hashlib.sha256(input_content.encode("utf-8")).hexdigest(),
        "aux": [], "datasets": sorted(dataset_sources or []),
        "name": job_name, "orca_link": orca_link or "",
        "maxdisk_mb": int(maxdisk_mb) if maxdisk_mb is not None else None,
    }
    payload_hash = hashlib.sha256(
        _json.dumps(request_payload, sort_keys=True).encode("utf-8")).hexdigest()
    key = idem_key or ("kaggle-submit:" + payload_hash)

    replay, stored = store.begin_idempotent(key, request_payload)
    if replay:
        if stored is None:
            return {"ok": False,
                    "error": "an identical submission is already in progress; it will appear in "
                             "your job list shortly"}, 409
        return stored, 200

    job_dir = None
    job_id = job_base_id
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
        response_data = {
            "ok": True,
            "kaggle_url": pushed["url"],
            "job_id": pushed["job_id"],
            "kaggle_owner": pushed["owner"],
            "job_title": job_title,
            "message": "Job submitted to Kaggle successfully. Track progress and results below.",
        }
        store.complete_idempotent(key, response_data)
        save_credentials(kaggle_username, kaggle_key)
        return response_data, 200
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        store.abandon_idempotent(key)
        return {"ok": False, "error": str(exc)}, 503
    except Exception as exc:  # noqa: BLE001
        # The push may have LANDED even though the call raised. Probe Kaggle
        # before releasing the store-backed claim: a landed push is persisted
        # so a retry REPLAYS it instead of pushing a second kernel; an
        # unanswerable probe KEEPS the claim (the store TTL bounds the wait).
        landed = None
        try:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.kaggle_api import KaggleClient
            status = KaggleClient(parse_credentials(kaggle_username, kaggle_key)).kernel_exists(job_id)
            landed = status is not None
        except Exception:  # noqa: BLE001
            landed = None
        if landed:
            response_data = {"ok": True, "kaggle_url": "https://www.kaggle.com/code/%s/%s"
                             % (kaggle_username, job_id),
                             "job_id": job_id, "kaggle_owner": kaggle_username,
                             "job_title": job_title,
                             "message": "the push landed despite the transport error"}
            store.complete_idempotent(key, response_data)
            return response_data, 200
        store.abandon_idempotent(key)
        return {"ok": False, "error": "Failed to submit the job to Kaggle: %s" % exc}, 502


# ---------------------------------------------------------------------------
# Optimized-coordinate extraction with the scientific OPT gate
# ---------------------------------------------------------------------------
def fetch_archive(kaggle_username, kaggle_key, job_id):
    """Fetches a job's results archive and returns (zip_path, cleanup_dir).

    The caller owns cleanup: after reading, remove cleanup_dir."""
    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key)
    job_id = (job_id or "").strip()
    if not kaggle_username or not kaggle_key or not job_id:
        return None, None, {"ok": False, "error": "Missing username, API key, or job id."}, 400
    if not kaggle_runner.is_valid_job_id(job_id):
        return None, None, {"ok": False, "error": "That job id doesn't look like one of this site's jobs."}, 400
    try:
        try:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.service import get_service
            creds = parse_credentials(kaggle_username, kaggle_key)
            zip_path, cleanup_dir = get_service().fetch_results(creds, job_id)
        except _OrchValidationError as exc:
            # Owner denial (and any other deterministic validation) must NOT
            # fall into the legacy fetch fallback: it would be misreported.
            if 'does not belong' in str(exc):
                raise _OwnerDenied(str(exc))
            raise
        except Exception:
            zip_path, cleanup_dir = kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)
        if not zip_path or not os.path.exists(zip_path):
            return None, None, {"ok": False, "error": "Could not find output results archive on Kaggle."}, 404
        return zip_path, cleanup_dir, {"ok": True}, 200
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        return None, None, {"ok": False, "error": str(exc)}, 503
    except _OwnerDenied as exc:
        return None, None, {"ok": False,
                            "error": {"code": "FORBIDDEN", "message": str(exc)}}, 403
    except Exception as exc:  # noqa: BLE001
        return None, None, {"ok": False, "error": "Failed to fetch the archive: %s" % exc}, 502


def extract_opt_coords(kaggle_username, kaggle_key, job_id):
    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key)
    job_id = (job_id or "").strip()
    if not kaggle_username or not kaggle_key or not job_id:
        return {"ok": False, "error": "Missing username, API key, or job id."}, 400
    if not kaggle_runner.is_valid_job_id(job_id):
        return {"ok": False, "error": "That job id doesn't look like one of this site's jobs."}, 400

    cleanup_dir = None
    try:
        try:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.service import get_service
            creds = parse_credentials(kaggle_username, kaggle_key)
            zip_path, cleanup_dir = get_service().fetch_results(creds, job_id)
        except _OrchValidationError as exc:
            # Owner denial (and any other deterministic validation) must NOT
            # fall into the legacy fetch fallback: it would be misreported.
            if 'does not belong' in str(exc):
                raise _OwnerDenied(str(exc))
            raise
        except Exception:
            zip_path, cleanup_dir = kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)

        if not zip_path or not os.path.exists(zip_path):
            return {"ok": False, "error": "Could not find output results archive on Kaggle."}, 404

        out_content = None
        out_name = None
        xyz_content = None
        inp_content = None
        with zipfile.ZipFile(zip_path, "r") as zf:
            for item in zf.infolist():
                fname_lower = item.filename.lower()
                if not out_content and fname_lower.endswith((".out", ".log", ".property.txt")):
                    out_name = item.filename
                    out_content = zf.read(item).decode("utf-8", errors="replace")
                elif fname_lower.endswith(".xyz") and not fname_lower.endswith(("_trj.xyz", "trajectory.xyz")):
                    if not any(fname_lower.startswith(p) for p in ("original", "input", "initial", "start")) and not any(fname_lower.endswith(s) for s in ("_input.xyz", "_initial.xyz", "_start.xyz")):
                        if not xyz_content or fname_lower.endswith((".opt.xyz", "opt.xyz", "final.xyz")):
                            xyz_content = zf.read(item).decode("utf-8", errors="replace")
                elif fname_lower.endswith(".inp") and not inp_content:
                    inp_content = zf.read(item).decode("utf-8", errors="replace")

        # ── Scientific eligibility gate ────────────────────────────────────
        try:
            from orca_orchestrator import orca_artifacts as _art
        except ImportError:
            _art = None
        if _art is None:
            return {"ok": False, "error": "Coordinate extraction requires the orchestration module."}, 503
        job_kind = _art.detect_job_kind(inp_content or "")
        if job_kind not in ("opt", "opt_ts"):
            return {"ok": False,
                    "error": "This calculation is a %s, not a geometry optimization - coordinates "
                             "can only be imported from completed Opt/OptTS runs." % (job_kind or "unknown"),
                    "code": "not_optimization", "job_kind": job_kind}, 422
        outcome = _art.classify_outcome(out_content or "", job_kind=job_kind)
        if not outcome.is_complete:
            return {"ok": False,
                    "error": "This optimization did not converge (%s), so it has no final optimized "
                             "geometry to import." % outcome.kind,
                    "code": "optimization_not_complete", "job_kind": job_kind,
                    "outcome": outcome.kind}, 422

        clean_xyz = ""
        total_energy = None
        formula = ""

        if xyz_content:
            raw_blocks = re.split(r"\n(?=\s*\d+\s*\n)", xyz_content.strip())
            target_block = raw_blocks[-1].strip() if raw_blocks else xyz_content.strip()
            raw_lines = target_block.splitlines()
            if len(raw_lines) > 2 and raw_lines[0].strip().isdigit():
                atom_lines = raw_lines[2:]
            else:
                atom_lines = raw_lines
            extracted_lines = []
            for line in atom_lines:
                parts = line.strip().split()
                if len(parts) >= 4 and re.match(r"^[A-Za-z]{1,2}:?$", parts[0]):
                    try:
                        float(parts[1]), float(parts[2]), float(parts[3])
                        extracted_lines.append(f"{parts[0]:<3} {parts[1]} {parts[2]} {parts[3]}")
                    except ValueError:
                        pass
            if extracted_lines:
                clean_xyz = "\n".join(extracted_lines)

        if not clean_xyz and out_content:
            blocks = re.findall(r"CARTESIAN COORDINATES \(ANGSTROEM\)\s*\n[-=\s]+\n(.*?)(?:\n\s*\n|\n-+\n|\n\*\*\*|\Z)",
                                out_content, re.DOTALL | re.IGNORECASE)
            if blocks:
                last_block = blocks[-1].strip()
                extracted_lines = []
                for line in last_block.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 4 and re.match(r"^[A-Za-z]{1,2}:?$", parts[0]):
                        try:
                            float(parts[1]), float(parts[2]), float(parts[3])
                            extracted_lines.append(f"{parts[0]:<3} {parts[1]} {parts[2]} {parts[3]}")
                        except ValueError:
                            pass
                if extracted_lines:
                    clean_xyz = "\n".join(extracted_lines)

        # NOTE: deliberately NO fallback to the INPUT geometry - a coordinate
        # import must be the FINAL optimized geometry of a completed run.

        if out_content:
            try:
                from orca_engine.parser import OrcaParser  # optional engine dependency
            except ImportError:
                OrcaParser = None
            if OrcaParser:
                try:
                    parsed_jobs = OrcaParser(io.StringIO(out_content), source_name=out_name or job_id).parse()
                    if parsed_jobs:
                        last_job = parsed_jobs[-1]

                        def _plain(v):
                            if callable(v):
                                try:
                                    v = v()
                                except Exception:  # noqa: BLE001
                                    return None
                            return v

                        total_energy = (_plain(getattr(last_job, "e_elec_eh", None))
                                        or _plain(getattr(last_job, "electronic_zpe_eh", None))
                                        or _plain(getattr(last_job, "gibbs_free_energy_eh", None)))
                        _formula = _plain(getattr(last_job, "chemical_formula", "")) or ""
                        formula = str(_formula)
                        if not clean_xyz and getattr(last_job, "elements", None) and getattr(last_job, "coords", None):
                            lines = []
                            for el, (x, y, z) in zip(last_job.elements, last_job.coords, strict=False):
                                lines.append(f"{el:<3} {x:14.8f} {y:14.8f} {z:14.8f}".rstrip())
                            clean_xyz = "\n".join(lines)
                except Exception:  # noqa: BLE001
                    pass

        if not clean_xyz:
            return {"ok": False, "error": "No 3D coordinates found in calculation results or input file."}, 404

        geo_lines = [ln for ln in clean_xyz.splitlines() if ln.strip()]
        if not geo_lines or any(
                not re.match(r"^[A-Za-z]{1,2}:?\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+$", ln.strip())
                for ln in geo_lines):
            return {"ok": False,
                    "error": "The extracted geometry failed validation (empty, malformed, or non-numeric coordinates)."}, 422
        if len(geo_lines) > 1 and all(
                all(float(tok) == 0.0 for tok in ln.strip().split()[1:4]) for ln in geo_lines):
            return {"ok": False,
                    "error": "The extracted geometry is degenerate (every atom at the origin); it cannot be "
                             "an optimized structure."}, 422

        return {"ok": True, "job_id": job_id, "coords": clean_xyz, "converged": True,
                "job_kind": job_kind, "chemical_formula": formula or "",
                "total_energy_eh": total_energy, "e_elec_eh": total_energy,
                "atom_count": len(geo_lines)}, 200
    except _OwnerDenied as exc:
        return {"ok": False,
                "error": {"code": "FORBIDDEN", "message": str(exc)}}, 403
    finally:
        if cleanup_dir:
            import shutil
            shutil.rmtree(cleanup_dir, ignore_errors=True)
