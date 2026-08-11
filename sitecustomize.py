"""Production startup patch for the Chemistry Lab Space.

This file is intentionally tiny and isolated from the large legacy runner. Python
loads sitecustomize automatically at interpreter startup, so the patch applies
before Gunicorn imports app.py.

Fixes a compatibility problem in some Kaggle CLI versions: `kernels status`
can report COMPLETE, but the subsequent `kernels output --file-pattern ...`
probe can fail. The old checker converted that transport/projection failure into
UNKNOWN, so a genuinely completed job stayed UNKNOWN forever and the download
button never appeared. We trust Kaggle's authoritative kernel status first and
only use the deterministic successor probe to detect an actual continuation.
"""
from __future__ import annotations

try:
    import kaggle_runner as _kr

    def _production_check_job_status(kaggle_username: str, kaggle_key: str, job_id: str) -> dict:
        if not _kr.is_valid_job_id(job_id):
            raise RuntimeError(f"'{job_id}' is not a valid job id.")

        auth = _kr.resolve_kaggle_auth(kaggle_username, kaggle_key)
        with _kr._temp_kaggle_env(kaggle_username, kaggle_key) as env:
            proc = _kr._run_kaggle_cli(
                ["kaggle", "kernels", "status", f"{auth['username']}/{job_id}"],
                env=env, timeout=60,
            )
            text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            status = _kr._classify_kernel_status(text)

            if proc.returncode != 0:
                _kr._raise_if_cli_broken(text)
                _kr._raise_if_unreachable(text)
                note = text or "The Kaggle CLI returned no output."
                low = note.lower()
                if "401" in low or "unauthorized" in low or "authentication" in low:
                    note += "\n\nCheck your Kaggle username and API key/token."
                elif "404" in low or "not found" in low or "denied" in low:
                    note += "\n\nKaggle has no notebook at this address for this account."
                return {"status": "error", "next_job_id": None,
                        "next_kaggle_url": None, "note": note}

            # A successor is deterministic (<base>-r<N+1>). Check it before
            # accepting COMPLETE/ERROR/CANCELLED so a hand-off remains visible
            # even when the predecessor's output listing is unavailable.
            if status in ("complete", "error", "cancelled"):
                next_id, next_url = _kr._probe_successor(env, auth["username"], job_id)
                if next_id:
                    return {
                        "status": "restarting",
                        "next_job_id": next_id,
                        "next_kaggle_url": next_url,
                        "note": "A continuation notebook exists on Kaggle and is being followed.",
                    }

            if status in ("complete", "running", "queued", "cancelled", "error"):
                return {"status": status, "next_job_id": None,
                        "next_kaggle_url": None, "note": text}

            # Only use the old, output-aware checker when Kaggle itself did not
            # provide a recognizable status. This preserves its richer hand-off
            # handling without allowing an output-listing failure to turn a
            # known COMPLETE state into UNKNOWN.
            return _kr._ORIGINAL_CHECK_JOB_STATUS(kaggle_username, kaggle_key, job_id)

    if not hasattr(_kr, "_ORIGINAL_CHECK_JOB_STATUS"):
        _kr._ORIGINAL_CHECK_JOB_STATUS = _kr.check_job_status
        _kr.check_job_status = _production_check_job_status

except Exception:
    # Never prevent the application from starting because this optional patch
    # failed. The normal runner remains available as a fallback.
    pass
