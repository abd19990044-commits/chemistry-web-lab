# -*- coding: utf-8 -*-
"""Framework-independent Kaggle job services.

Shared by the Flask routes (app.py) and the FastAPI v1 layer (api/) so both
frameworks call the SAME implementation - no duplicate business logic.

Functions here return plain ``(payload, status_code)`` tuples instead of
framework responses; the transport layers wrap them.
"""
import base64
import binascii
import io
import os
import re
import traceback
import zipfile

from orca_orchestrator.logging_ext import configure  # noqa: F401  (keeps parity with app boot)

import kaggle_runner
from orca_orchestrator.errors import ValidationError as _OrchValidationError
from kaggle_runner import cli_health
from services import artifact_service


MAX_SCIENTIFIC_TEXT_BYTES = int(
    os.environ.get("ORCA_MAX_SCIENTIFIC_TEXT_BYTES", str(256 * 1024 * 1024))
)


class _OwnerDenied(Exception):
    """Raised when the orchestrator refuses a fetch for another user's job."""


def _fetch_results_authoritatively(kaggle_username, kaggle_key, job_id):
    """Use the orchestrator for tracked jobs and legacy only for old jobs.

    A transient failure while fetching a tracked job must not silently switch
    implementations.  Besides producing inconsistent retry/integrity
    behaviour, that fallback could bypass the orchestrator's owner check.
    """
    try:
        from orca_orchestrator.credentials import parse as parse_credentials
        from orca_orchestrator.service import get_service
        service = get_service()
    except Exception:
        # Compatibility for installations that genuinely pre-date the
        # orchestrator.  In the current application this branch is exceptional.
        return kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)

    get_job = getattr(getattr(service, "store", None), "get_job", None)
    tracked = bool(get_job(job_id)) if callable(get_job) else False
    creds = parse_credentials(kaggle_username, kaggle_key)
    try:
        return service.fetch_results(creds, job_id)
    except _OrchValidationError as exc:
        if "does not belong" in str(exc).lower() or "access denied" in str(exc).lower():
            raise _OwnerDenied(str(exc)) from exc
        raise
    except Exception:
        if tracked:
            # Never bypass ownership/integrity/retry semantics for a job known
            # to the durable orchestrator.
            raise
        # Pre-migration jobs have no manifest.  Keep the old adapter only for
        # those untracked jobs (and for lightweight compatibility stubs).
        return kaggle_runner.fetch_job_results(kaggle_username, kaggle_key, job_id)


import logging
log = logging.getLogger("orca.kaggle_service")

# ---------------------------------------------------------------------------
# Submit idempotency (moved verbatim from app.py - single implementation).
# ---------------------------------------------------------------------------
SUBMIT_DEDUP: dict[str, tuple[float, object]] = {}
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
def resolve_credentials(kaggle_username, kaggle_key, owner=None):
    """Mirrors app._resolve_kaggle_credentials exactly (clean + vault fallback)."""
    kaggle_username = (kaggle_username or "").strip()
    kaggle_key = (kaggle_key or "").strip()
    kaggle_username, kaggle_key = kaggle_runner.clean_kaggle_credentials(kaggle_username, kaggle_key)
    if not kaggle_key:
        try:
            from orca_orchestrator.credential_vault import get_vault_manager
            vm = get_vault_manager()
            loaded = None
            if owner:
                loaded = vm.load_credentials(owner)
            if not loaded and kaggle_username:
                loaded = vm.load_credentials(kaggle_username)
            if loaded:
                kaggle_key = loaded.key or loaded.api_token or ""
                if not kaggle_username and loaded.username:
                    kaggle_username = loaded.username
        except Exception:
            pass
    return kaggle_username, kaggle_key


def save_credentials(kaggle_username, kaggle_key, owner=None):
    try:
        from orca_orchestrator.credential_vault import get_vault_manager
        from orca_orchestrator.credentials import parse as parse_credentials
        creds = parse_credentials(kaggle_username, kaggle_key)
        target_owner = (owner or creds.username or "").strip()
        if target_owner:
            get_vault_manager().save_credentials(target_owner, creds)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not persist Kaggle credentials in the owner vault: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def check_status(kaggle_username, kaggle_key, job_id, owner=None):
    """Returns (payload, status_code)."""
    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key, owner=owner)
    job_id = (job_id or "").strip()
    if not kaggle_username or not kaggle_key or not job_id:
        return {"ok": False, "error": "Missing username, API key, or job id."}, 400
    if not kaggle_runner.is_valid_job_id(job_id):
        return {"ok": False, "error": "That job id doesn't look like one of this site's jobs."}, 400
    try:
        # Tracked jobs use the modern orchestrator as their source of truth.
        # The legacy CLI path remains only as an adapter for pre-migration jobs
        # that are not present in the durable manifest store.
        try:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.service import get_service
            modern_service = get_service()
            modern_creds = parse_credentials(kaggle_username, kaggle_key)
            if modern_service.store.get_job(job_id) is not None:
                described = modern_service.status(modern_creds, job_id)
                remote_status = str(described.get("remote_state") or "unknown").lower()
                if remote_status == "unknown":
                    remote_status = str(described.get("state") or "unknown").lower()
                return {"ok": True, **described, "status": remote_status}, 200
        except Exception as modern_exc:
            # A tracked job is not allowed to silently become a legacy job if
            # the modern status observation is temporarily unavailable.  The
            # UI receives an explicit remote-status-unknown response.
            try:
                from orca_orchestrator.service import get_service
                if get_service().store.get_job(job_id) is not None:
                    return {"ok": True, "status": "unknown", "remote_status_unknown": True,
                            "error": type(modern_exc).__name__}, 200
            except Exception:
                pass
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
               extra_files=None, maxdisk_mb=None, owner=None):
    """Authoritative submission used by BOTH Flask and FastAPI.

    Idempotency is enforced by the ORCHESTRATOR STORE (SQLite, shared across
    workers/processes): begin_idempotent claims the key, complete_idempotent
    persists the response, abandon_idempotent releases it only when the push
    provably did not land. The in-memory Idempotency-Key cache in the Flask
    route is a compatibility layer only - never the sole guarantee.
    """
    import hashlib
    import json as _json

    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key, owner=owner)

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

    # Production traffic has one Kaggle source of truth: the modern durable
    # orchestrator.  ``store`` remains an explicit compatibility seam for old
    # migration tests/tools; web routes never pass it.  Keeping the adapter at
    # this boundary avoids a second implementation of submit/status/recovery.
    if store is None:
        try:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.errors import (
                AuthenticationError,
                ConcurrencyError,
                PermanentError,
                SubmissionUnknownError,
                TransientError,
            )
            from orca_orchestrator.service import get_service

            aux_files = {}
            for aux_name, encoded in (extra_files or {}).items():
                safe_name = core.safe_filename(os.path.basename(str(aux_name)))
                if safe_name != os.path.basename(str(aux_name)):
                    return {"ok": False, "error": "Invalid auxiliary filename."}, 400
                if safe_name == input_filename:
                    continue
                try:
                    aux_files[safe_name] = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError, binascii.Error):
                    return {"ok": False, "error": "Invalid base64 auxiliary file payload."}, 400

            result = get_service().submit(
                parse_credentials(kaggle_username, kaggle_key),
                input_filename=input_filename,
                input_content=input_content,
                job_name=job_name or "",
                aux_files=aux_files,
                dataset_sources=dataset_sources,
                orca_link=orca_link or None,
                idempotency_key=idem_key,
                application_owner=owner,
            )
            response_data = {
                "ok": True,
                "kaggle_url": result.url,
                "job_id": result.job_id,
                "kaggle_owner": kaggle_username,
                "job_title": result.title,
                "replayed": result.replayed,
                "message": "Job submitted to Kaggle successfully. Track progress and results below.",
            }
            save_credentials(kaggle_username, kaggle_key, owner=owner)
            return response_data, 200
        except AuthenticationError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}, 401
        except ConcurrencyError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}, 409
        except SubmissionUnknownError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code.upper()}, 503
        except PermanentError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}, 400
        except TransientError as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}, 503
        except Exception as exc:  # noqa: BLE001
            log.exception("Modern Kaggle submission adapter failed")
            return {"ok": False, "error": "Failed to submit the job to Kaggle: %s" % exc,
                    "code": "SUBMIT_FAILED"}, 502

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
        # F-007: Persist manifest in CREATED state BEFORE remote side-effect push
        if store is not None:
            try:
                from orca_orchestrator.models import JobManifest
                from orca_orchestrator.states import JobState
                initial_manifest = JobManifest.create(
                    job_id=job_id,
                    owner=kaggle_username,
                    title=job_title,
                    input_filename=input_filename,
                    original_input_sha256=hashlib.sha256(input_content.encode("utf-8")).hexdigest(),
                    dataset_sources=dataset_sources or [],
                    orca_link_present=bool(orca_link),
                    state=JobState.CREATED,
                    current_url="",
                )
                store.put_job(initial_manifest)
            except Exception as store_err:
                if log is not None:
                    log.warning("Store put_job before push skipped: %s", store_err)

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
        if store is not None:
            try:
                from orca_orchestrator.models import JobManifest
                from orca_orchestrator.states import JobState
                manifest = JobManifest.create(
                    job_id=pushed["job_id"],
                    owner=pushed["owner"],
                    title=job_title,
                    input_filename=input_filename,
                    original_input_sha256=hashlib.sha256(input_content.encode("utf-8")).hexdigest(),
                    dataset_sources=dataset_sources or [],
                    orca_link_present=bool(orca_link),
                    state=JobState.RUNNING,
                    current_url=pushed.get("url", ""),
                )
                store.put_job(manifest)
            except Exception as store_err:
                if log is not None:
                    log.warning("Store put_job after submit skipped: %s", store_err)
        save_credentials(kaggle_username, kaggle_key, owner=owner)
        return response_data, 200
    except Exception as exc:  # noqa: BLE001
        # The push may have LANDED even though the call raised. Probe Kaggle
        # before releasing the store-backed claim: a landed push is persisted
        # so a retry REPLAYS it instead of pushing a second kernel; an
        # unanswerable probe KEEPS the claim (the store TTL bounds the wait).
        landed = None
        probe_answered = False
        try:
            from orca_orchestrator.credentials import parse as parse_credentials
            from orca_orchestrator.kaggle_api import KaggleClient
            status = KaggleClient(parse_credentials(kaggle_username, kaggle_key)).kernel_exists(job_id)
            landed = status is not None
            probe_answered = True
        except Exception:  # noqa: BLE001
            landed = None
            probe_answered = False
        if landed:
            response_data = {"ok": True, "kaggle_url": "https://www.kaggle.com/code/%s/%s"
                             % (kaggle_username, job_id),
                             "job_id": job_id, "kaggle_owner": kaggle_username,
                             "job_title": job_title,
                             "message": "the push landed despite the transport error"}
            store.complete_idempotent(key, response_data)
            if store is not None:
                try:
                    from orca_orchestrator.models import JobManifest
                    from orca_orchestrator.states import JobState
                    manifest = JobManifest.create(
                        job_id=job_id,
                        owner=kaggle_username,
                        title=job_title,
                        input_filename=input_filename,
                        original_input_sha256=hashlib.sha256(input_content.encode("utf-8")).hexdigest(),
                        dataset_sources=dataset_sources or [],
                        orca_link_present=bool(orca_link),
                        state=JobState.RUNNING,
                        current_url=response_data["kaggle_url"],
                    )
                    store.put_job(manifest)
                except Exception as store_err:
                    if log is not None:
                        log.warning("Store put_job after landed push skipped: %s", store_err)
            return response_data, 200
        # F-004: An unanswered probe means the remote side is unknown.  Keep the
        # claim so a retry cannot submit a duplicate kernel.  Release it only
        # after Kaggle definitively answered NotFound (probe_answered and not landed).
        if probe_answered and not landed:
            store.abandon_idempotent(key)
            if store is not None:
                try:
                    store.delete_job(job_id)
                except Exception:
                    pass
        status_code = 503 if isinstance(exc, (kaggle_runner.KaggleCliUnavailable,
                                               kaggle_runner.KaggleUnreachable)) else 502
        if not probe_answered:
            err_msg = (
                "SUBMISSION_UNKNOWN: Remote status could not be verified after transport failure (%s). "
                "Idempotency claim is held to prevent duplicate kernel execution." % exc
            )
            return {"ok": False, "error": err_msg, "code": "SUBMISSION_UNKNOWN", "job_id": job_id}, status_code
        return {"ok": False, "error": "Failed to submit the job to Kaggle: %s" % exc, "code": "SUBMIT_FAILED"}, status_code


# ---------------------------------------------------------------------------
# Optimized-coordinate extraction with the scientific OPT gate
# ---------------------------------------------------------------------------
def fetch_archive(kaggle_username, kaggle_key, job_id, owner=None):
    """Fetches a job's results archive and returns (zip_path, cleanup_dir).

    The caller owns cleanup: after reading, remove cleanup_dir."""
    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key, owner=owner)
    job_id = (job_id or "").strip()
    if not kaggle_username or not kaggle_key or not job_id:
        return None, None, {"ok": False, "error": "Missing username, API key, or job id."}, 400
    if not kaggle_runner.is_valid_job_id(job_id):
        return None, None, {"ok": False, "error": "That job id doesn't look like one of this site's jobs."}, 400
    try:
        zip_path, cleanup_dir = _fetch_results_authoritatively(
            kaggle_username, kaggle_key, job_id
        )
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


def extract_opt_coords(kaggle_username, kaggle_key, job_id, owner=None):
    kaggle_username, kaggle_key = resolve_credentials(kaggle_username, kaggle_key, owner=owner)
    job_id = (job_id or "").strip()
    if not kaggle_username or not kaggle_key or not job_id:
        return {"ok": False, "error": "Missing username, API key, or job id."}, 400
    if not kaggle_runner.is_valid_job_id(job_id):
        return {"ok": False, "error": "That job id doesn't look like one of this site's jobs."}, 400

    cleanup_dir = None
    try:
        zip_path, cleanup_dir = _fetch_results_authoritatively(
            kaggle_username, kaggle_key, job_id
        )

        if not zip_path or not os.path.exists(zip_path):
            return {"ok": False, "error": "Could not find output results archive on Kaggle."}, 404

        out_content = None
        out_name = None
        xyz_content = None
        inp_content = None
        with zipfile.ZipFile(zip_path, "r") as zf:
            artifact_service.validate_archive(zf)
            # Determine the input first, then re-select the output using its
            # basename.  This prevents notebook logs and files from another
            # continuation window from becoming the scientific source.
            preliminary_output = artifact_service.select_orca_output_info(zf)
            input_info = artifact_service.select_matching_input_info(zf, preliminary_output)
            input_name = input_info.filename if input_info is not None else ""
            output_info = artifact_service.select_orca_output_info(zf, input_name)
            xyz_info = artifact_service.select_orca_xyz_info(zf, input_name)
            out_name = output_info.filename if output_info is not None else None
            out_content = artifact_service.read_text_member(
                zf, output_info, max_bytes=MAX_SCIENTIFIC_TEXT_BYTES
            )
            inp_content = artifact_service.read_text_member(
                zf, input_info, max_bytes=min(MAX_SCIENTIFIC_TEXT_BYTES, 8 * 1024 * 1024)
            )
            xyz_content = artifact_service.read_text_member(
                zf, xyz_info, max_bytes=min(MAX_SCIENTIFIC_TEXT_BYTES, 64 * 1024 * 1024)
            )

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

        # The converged ORCA output is authoritative.  A standalone XYZ is a
        # fallback only when the selected output genuinely carries no final
        # coordinates; otherwise a stale XYZ could silently feed the next
        # workflow stage the wrong geometry.
        if out_content:
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

        if not clean_xyz and xyz_content:
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
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False,
                "error": {"code": "ARCHIVE_UNREADABLE", "message": str(exc)}}, 502
    except (kaggle_runner.KaggleCliUnavailable, kaggle_runner.KaggleUnreachable) as exc:
        return {"ok": False, "error": str(exc)}, 503
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to extract optimized coordinates for %s", job_id)
        return {"ok": False, "error": "Failed to extract optimized coordinates: %s" % exc}, 502
    finally:
        if cleanup_dir:
            import shutil
            shutil.rmtree(cleanup_dir, ignore_errors=True)
