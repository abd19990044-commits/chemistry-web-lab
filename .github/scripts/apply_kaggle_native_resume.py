from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    count = text.count(old)
    assert count == 1, f'{path}: expected one match, found {count}'
    p.write_text(text.replace(old, new, 1), encoding='utf-8')

# ------------------------------------------------------------------
# API: capture the public site URL from the request and provide wake/update
# endpoints for Kaggle. Reconnect no longer touches Cloudflare.
# ------------------------------------------------------------------
replace_once(
    'orca_orchestrator/api.py',
    '''    report = service.cf_controller.reconcile_user_session(creds)\n    return jsonify({\n        "ok": True,\n        "owner": creds.username,\n        "reconciliation": report,\n        "jobs": service.list_jobs(creds),\n        "workflows": service.list_workflows(creds),\n    })\n''',
    '''    jobs = service.list_jobs(creds)\n    return jsonify({\n        "ok": True,\n        "owner": creds.username,\n        "reconciliation": {"source": "kaggle", "cloudflare_used": False},\n        "jobs": jobs,\n        "workflows": service.list_workflows(creds),\n    })\n'''
)
replace_once(
    'orca_orchestrator/api.py',
    '''        dataset_sources=datasets, orca_link=orca_link,\n        idempotency_key=request.headers.get("Idempotency-Key"))\n''',
    '''        dataset_sources=datasets, orca_link=orca_link,\n        idempotency_key=request.headers.get("Idempotency-Key"),\n        callback_base_url=request.url_root.rstrip("/"))\n'''
)
marker = '@bp.route("/status", methods=["POST"])\ndef status():'
p = Path('orca_orchestrator/api.py')
text = p.read_text(encoding='utf-8')
assert text.count(marker) == 1
insert = '''@bp.route("/wake", methods=["GET", "HEAD"])\ndef wake():\n    # Deliberately tiny: Kaggle calls this first to wake a sleeping Space before\n    # posting the heavier state payload. No credentials are required.\n    return jsonify({"ok": True, "service": "orca", "awake": True})\n\n\n@bp.route("/kernel-update", methods=["POST"])\ndef kernel_update():\n    payload = _json()\n    token = request.headers.get("X-ORCA-Callback-Token") or payload.get("callback_token") or ""\n    result = get_service().ingest_kernel_update(payload, token)\n    return jsonify({"ok": True, **result})\n\n\n'''
text = text.replace(marker, insert + marker)
p.write_text(text, encoding='utf-8')

# ------------------------------------------------------------------
# Builder: callback URL/token become part of the self-contained Kaggle header.
# Successor kernels already copy the whole header, so this automatically follows
# every restart window with no server state required.
# ------------------------------------------------------------------
replace_once(
    'orca_orchestrator/runner/builder.py',
    '''def build_header(*, job: JobManifest, epoch: int, creds: KaggleCredentials,\n                 inline_files: dict[str, bytes] | None = None,\n                 checkpoint: CheckpointManifest | None = None,\n                 predecessor_slug: str = "", orca_link: str | None = None) -> dict:\n''',
    '''def build_header(*, job: JobManifest, epoch: int, creds: KaggleCredentials,\n                 inline_files: dict[str, bytes] | None = None,\n                 checkpoint: CheckpointManifest | None = None,\n                 predecessor_slug: str = "", orca_link: str | None = None,\n                 callback_base_url: str | None = None,\n                 callback_token: str | None = None) -> dict:\n'''
)
replace_once(
    'orca_orchestrator/runner/builder.py',
    '''        "orca_link": orca_link,\n        "kaggle_username": creds.username,\n''',
    '''        "orca_link": orca_link,\n        "callback_base_url": callback_base_url or (job._extra.get("callback_base_url") if getattr(job, "_extra", None) else None),\n        "callback_token": callback_token or (job._extra.get("callback_token") if getattr(job, "_extra", None) else None),\n        "kaggle_username": creds.username,\n'''
)
replace_once(
    'orca_orchestrator/runner/builder.py',
    '''    predecessor_slug: str = "",\n) -> str:\n''',
    '''    predecessor_slug: str = "",\n    callback_base_url: str | None = None,\n    callback_token: str | None = None,\n) -> str:\n'''
)
replace_once(
    'orca_orchestrator/runner/builder.py',
    '''        job=job, epoch=epoch, creds=creds, inline_files=inline_files,\n        checkpoint=checkpoint, predecessor_slug=predecessor_slug, orca_link=orca_link,\n    )\n''',
    '''        job=job, epoch=epoch, creds=creds, inline_files=inline_files,\n        checkpoint=checkpoint, predecessor_slug=predecessor_slug, orca_link=orca_link,\n        callback_base_url=callback_base_url, callback_token=callback_token,\n    )\n'''
)

# ------------------------------------------------------------------
# Service: Cloudflare is removed from job submission/status/listing. Kaggle's
# STATE.json is the durable source of truth. A callback update only warms the
# local cache; the next authenticated list/status still verifies against Kaggle.
# ------------------------------------------------------------------
p = Path('orca_orchestrator/service.py')
text = p.read_text(encoding='utf-8')
text = text.replace('import os\n', 'import os\nimport hashlib\nimport secrets\n', 1)
text = text.replace(
    '''        step_name: str = "CALC",\n    ) -> SubmitResult:\n''',
    '''        step_name: str = "CALC",\n        callback_base_url: str | None = None,\n    ) -> SubmitResult:\n''', 1)
text = text.replace(
    '''                        "step_name": step_name,\n                    },\n''',
    '''                        "step_name": step_name,\n                        "callback_base_url": (callback_base_url or "").rstrip("/"),\n                        "callback_token": secrets.token_urlsafe(32),\n                    },\n''', 1)
# remove CF pre-registration
start = text.index('                # Persist durable metadata BEFORE pushing to Kaggle.')
end = text.index('                inline = {input_filename:', start)
text = text[:start] + '                # Kaggle output is the durable job ledger; no Cloudflare registration.\n' + text[end:]
text = text.replace(
    '''                        inline_files=inline, orca_link=orca_link,\n                    )\n''',
    '''                        inline_files=inline, orca_link=orca_link,\n                        callback_base_url=job._extra.get("callback_base_url"),\n                        callback_token=job._extra.get("callback_token"),\n                    )\n''', 1)
# remove CF post-push sync
start = text.index('                # Finalise the durable metadata with the real Kaggle slug/URL.')
end = text.index('                response = SubmitResult', start)
text = text[:start] + '                # No external control plane: Kaggle retains the authoritative ledger.\n' + text[end:]
# status: no CF sync
old = '''        if reconcile:\n            job = self.reconciler.reconcile(job_id, auth_creds, actor="api")\n            try:\n                self.cf_controller.sync_job_state(job)\n            except Exception:\n                pass\n\n        return self.describe(job)\n'''
new = '''        if reconcile:\n            job = self.reconciler.reconcile(job_id, auth_creds, actor="api")\n\n        return self.describe(job)\n'''
assert old in text
text = text.replace(old, new, 1)
# compatibility field no longer references Cloudflare
text = text.replace(
    '''            "cf_sync_status": getattr(job, "cf_sync_status", "SYNCED" if getattr(self.cf_controller.client, "is_durable", False) else "DEGRADED_UNSYNCED"),\n''',
    '''            "cf_sync_status": "KAGGLE_LEDGER",\n''', 1)
# replace list_jobs wholesale with Kaggle-first implementation
start = text.index('    def list_jobs(self, creds: KaggleCredentials) -> list[dict]:')
end = text.index('    # -- workflow execution', start)
new_list = r'''    def list_jobs(self, creds: KaggleCredentials) -> list[dict]:
        """List jobs with Kaggle as the only durable source of truth.

        Kaggle's kernel list discovers chains; each chain is rebuilt/reconciled from
        STATE.json in saved output. The local SQLite store is only a warm cache and is
        never required for recovery after a Space restart.
        """
        self._last_listing_creds = creds
        auth_creds = self.ensure_authenticated(creds)
        client = KaggleClient(auth_creds)
        try:
            remote = ledger_mod.discover_jobs(client)
        except Exception as exc:
            log.warning("Kaggle bulk listing unavailable; serving warm local cache: %s", exc)
            remote = []

        merged, seen = [], set()
        for entry in remote:
            job_id = entry["job_id"]
            seen.add(job_id)
            try:
                # Always rebuild if the cache is absent; otherwise reconcile against the
                # newest saved Kaggle output. No Cloudflare metadata is consulted.
                job = self.store.get_job(job_id)
                if job is None:
                    job = ledger_mod.rebuild_from_kaggle(client, job_id)
                    self.store.put_job(job)
                if not job.is_terminal:
                    job = self.reconciler.reconcile(job_id, auth_creds, actor="list")
                described = self.describe(job)
                described["chain_slugs"] = sorted(
                    set(described.get("chain_slugs", [])) | set(entry.get("chain_slugs", []))
                )
                described["last_run"] = entry.get("last_run")
                described["state_source"] = "kaggle"
                merged.append(described)
            except Exception as exc:
                merged.append({
                    "job_id": job_id, "title": entry.get("title", job_id),
                    "state": "VERIFYING", "phase": "Verifying Kaggle state",
                    "epoch": entry.get("epoch", 0), "window": entry.get("epoch", 0) + 1,
                    "current_slug": entry.get("current_slug"),
                    "kaggle_url": entry.get("kaggle_url"),
                    "chain_slugs": entry.get("chain_slugs", []),
                    "is_terminal": False, "needs_reconcile": True,
                    "last_run": entry.get("last_run"), "state_source": "kaggle",
                    "note": f"Kaggle status check will retry: {type(exc).__name__}",
                })

        # A just-submitted kernel can take a short time to appear in Kaggle's bulk list.
        # Keep it visible from the warm cache, but label the source honestly.
        for job in self.store.list_jobs(auth_creds.username):
            if job.job_id not in seen:
                described = self.describe(job)
                described["not_returned_by_kaggle_listing"] = True
                described["deleted_on_kaggle"] = False
                described["state_source"] = "local_cache_pending_kaggle"
                merged.append(described)

        def stamp(item):
            for key in ("updated_at", "last_run", "created_at"):
                value = item.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            return 0.0
        merged.sort(key=stamp, reverse=True)
        return merged

    def ingest_kernel_update(self, payload: dict[str, Any], callback_token: str) -> dict[str, Any]:
        """Warm the local cache from a signed-by-possession Kaggle callback.

        This endpoint is intentionally not authoritative: user-facing status is still
        reconciled against Kaggle. The per-job callback token is random, lives inside the
        private Kaggle kernel header, and is checked whenever the local job record exists.
        """
        state = payload.get("state") or payload.get("document") or {}
        job_data = state.get("job") if isinstance(state, dict) else None
        if not isinstance(job_data, dict):
            raise ValidationError("kernel update is missing state.job")
        job_id = str(job_data.get("job_id") or "").strip()
        if not is_valid_slug(job_id) or not job_id.startswith(CONFIG.job_id_prefix):
            raise ValidationError("kernel update has an invalid job id")
        if not callback_token or len(callback_token) < 24:
            raise ValidationError("kernel callback token is missing or invalid")

        # Verify STATE.json's own digest before accepting it into the warm cache.
        digest = state.get("_digest")
        if digest:
            body = {k: v for k, v in state.items() if k != "_digest"}
            actual = hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           default=str).encode("utf-8")
            ).hexdigest()
            if actual != digest:
                raise IntegrityError("kernel STATE.json digest mismatch")

        current = self.store.get_job(job_id)
        if current is not None:
            expected = str((current._extra or {}).get("callback_token") or "")
            if expected and not secrets.compare_digest(expected, callback_token):
                raise ValidationError("kernel callback token does not match this job")
            manifest = current
            incoming = JobManifest.from_dict(job_data)
            # Preserve local-only callback/workflow metadata while copying durable fields.
            keep_extra = dict(current._extra or {})
            incoming._extra.update({k: v for k, v in keep_extra.items() if k not in incoming._extra})
            incoming._extra["callback_token"] = callback_token
            manifest = incoming
        else:
            manifest = JobManifest.from_dict(job_data)
            manifest._extra["callback_token"] = callback_token
        self.store.put_job(manifest)
        return {"job_id": job_id, "state": manifest.state.value,
                "epoch": manifest.epoch, "source": "kaggle_callback"}

'''
text = text[:start] + new_list + text[end:]
# need json import for digest canonicalisation
text = text.replace('import hashlib\n', 'import hashlib\nimport json\n', 1)
p.write_text(text, encoding='utf-8')

# ------------------------------------------------------------------
# Kernel runner: wake site, then POST the complete STATE.json document.
# The callback is best-effort and NEVER gates ORCA continuation.
# ------------------------------------------------------------------
p = Path('orca_orchestrator/runner/kernel_runner.py')
text = p.read_text(encoding='utf-8')
text = text.replace('import traceback\n', 'import traceback\nimport urllib.error\nimport urllib.request\n', 1)
text = text.replace(
    '''    header.setdefault("orca_link", None)\n    header.setdefault("kaggle_username", "")\n''',
    '''    header.setdefault("orca_link", None)\n    header.setdefault("callback_base_url", None)\n    header.setdefault("callback_token", None)\n    header.setdefault("kaggle_username", "")\n''', 1)
marker = '# ---------------------------------------------------------------------------\n# Atomic writes\n# ---------------------------------------------------------------------------\n'
assert text.count(marker) == 1
callback_code = r'''# ---------------------------------------------------------------------------
# Site wake-up + state callback
# ---------------------------------------------------------------------------
def _callback_url(path):
    base = str(H.get("callback_base_url") or "").strip().rstrip("/")
    return (base + path) if base else ""


def wake_site():
    """Best-effort wake request before a state POST.

    Sleeping Hugging Face/Render-style services may need one request to start the
    container. Failure is harmless because Kaggle remains the authoritative ledger.
    """
    url = _callback_url("/api/orca/wake")
    if not url:
        return False
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "ORCA-Kaggle-Runner/1"})
            with urllib.request.urlopen(req, timeout=20 + attempt * 10) as resp:
                if 200 <= int(getattr(resp, "status", 200)) < 500:
                    emit("site_wake_ok", "site answered the wake request", attempt=attempt + 1)
                    return True
        except Exception as exc:
            emit("site_wake_retry", "site wake request did not answer yet",
                 attempt=attempt + 1, detail=_scrub(str(exc))[:180])
        time.sleep(3 * (attempt + 1))
    return False


def notify_site_from_state(reason="state_update"):
    url = _callback_url("/api/orca/kernel-update")
    token = str(H.get("callback_token") or "")
    if not url or not token or not os.path.exists(STATE_FILE):
        return False
    # Visit first to wake the service, exactly as requested. The POST remains
    # best-effort: a sleeping/unreachable site must never stop a calculation.
    wake_site()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        payload = json.dumps({
            "schema_version": 1,
            "reason": reason,
            "job_id": JOB_ID,
            "epoch": EPOCH,
            "kernel_slug": os.environ.get("KAGGLE_KERNEL_SLUG", "") or JOB_ID,
            "state": state,
        }, sort_keys=True, default=str).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ORCA-Kaggle-Runner/1",
                "X-ORCA-Callback-Token": token,
            },
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            ok = 200 <= int(getattr(resp, "status", 200)) < 300
        emit("site_update_sent" if ok else "site_update_rejected",
             "sent Kaggle state to the site" if ok else "site rejected the Kaggle state",
             reason=reason)
        return ok
    except Exception as exc:
        emit("site_update_failed", "Kaggle state remains saved even though callback failed",
             reason=reason, detail=_scrub(str(exc))[:240])
        return False


'''
text = text.replace(marker, callback_code + marker, 1)
# write_state: notify only important states, after durable file write + legacy markers.
needle = '''    if next_slug:\n        try:\n            atomic_write_bytes(LEGACY_NEXT_ID, next_slug.encode("utf-8"))\n            atomic_write_bytes(LEGACY_NEXT_URL, (next_url or "").encode("utf-8"))\n        except OSError:\n            pass\n\n\n# ---------------------------------------------------------------------------\n# main\n'''
replacement = '''    if next_slug:\n        try:\n            atomic_write_bytes(LEGACY_NEXT_ID, next_slug.encode("utf-8"))\n            atomic_write_bytes(LEGACY_NEXT_URL, (next_url or "").encode("utf-8"))\n        except OSError:\n            pass\n\n    # Do not phone home for every RUNNING heartbeat. Important durable transitions\n    # are enough to update the UI while keeping callback traffic tiny.\n    if state in ("QUEUED", "CHECKPOINTING", "RESTARTING", "ROLLING_BACK", "FINISHED", "FAILED"):\n        notify_site_from_state(reason=state.lower())\n\n\n# ---------------------------------------------------------------------------\n# main\n'''
assert needle in text
text = text.replace(needle, replacement, 1)
p.write_text(text, encoding='utf-8')

print('Kaggle-native resume/callback patch applied')
