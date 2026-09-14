from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    assert count == 1, f"{path}: expected one match, found {count}"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")

# 1) A transient Kaggle status lookup exception must not be interpreted as NOT_FOUND.
replace_once(
    "orca_orchestrator/cloudflare_controller/controller.py",
    '''            remote_status: KernelStatus | None = None
            try:
                # Query newest window slug or base slug
                slug_to_check = cf_job.chain_slugs[-1] if cf_job.chain_slugs else job_ref
                remote_status = client.kernel_exists(slug_to_check)
            except Exception as exc:
                log.warning("Could not query Kaggle status for job %s: %s", job_ref, exc)

            # Execute deterministic reconciliation decision
            decision: ReconciliationDecision = decide_reconciliation(
                cf_record=cf_job,
                remote_status=remote_status,
                manifest=manifest,
            )
''',
    '''            remote_status: KernelStatus | None = None
            status_query_failed = False
            try:
                # Query newest window slug or base slug
                slug_to_check = cf_job.chain_slugs[-1] if cf_job.chain_slugs else job_ref
                remote_status = client.kernel_exists(slug_to_check)
            except Exception as exc:
                status_query_failed = True
                log.warning("Could not query Kaggle status for job %s: %s", job_ref, exc)

            # A transport/API failure is UNKNOWN, not NOT_FOUND. Preserve the last durable
            # Cloudflare state and retry later. Only a successful exact lookup returning
            # None is allowed to mean that Kaggle no longer has the kernel.
            if status_query_failed:
                decision = ReconciliationDecision(
                    case=ReconciliationCase.UNKNOWN,
                    action="NOOP",
                    local_state=cf_job.local_state,
                    remote_state=cf_job.last_seen_remote_state or RemoteExecutionState.UNKNOWN.value,
                    resume_required=cf_job.resume_required,
                    resume_reason=cf_job.resume_reason,
                    result_state=cf_job.result_state or "REMOTE_ONLY",
                    remote_deleted=cf_job.remote_deleted,
                    note="Kaggle status lookup failed transiently; preserved last durable state.",
                    error_code=cf_job.last_error_code,
                )
            else:
                decision = decide_reconciliation(
                    cf_record=cf_job,
                    remote_status=remote_status,
                    manifest=manifest,
                )
''',
)

# 2) REMOTE_DELETED / REMOTE_STOPPED workflow steps are failed, never accidentally completed.
replace_once(
    "orca_orchestrator/cloudflare_controller/controller.py",
    '''                    elif j_rec.local_state in (LocalWorkflowState.FAILED.value, LocalWorkflowState.REMOTE_FAILED.value):
                        step.status = "FAILED"
                        has_failed = True
''',
    '''                    elif j_rec.local_state in (
                        LocalWorkflowState.FAILED.value,
                        LocalWorkflowState.REMOTE_FAILED.value,
                        LocalWorkflowState.REMOTE_DELETED.value,
                        LocalWorkflowState.REMOTE_STOPPED.value,
                    ):
                        step.status = "FAILED"
                        has_failed = True
''',
)

# 3) Bulk Kaggle listing failures degrade to Cloudflare/local history rather than failing My Jobs.
replace_once(
    "orca_orchestrator/service.py",
    '''        # 2. Discover jobs on Kaggle
        remote = ledger_mod.discover_jobs(client)
        merged = []
''',
    '''        # 2. Discover jobs on Kaggle. The bulk listing endpoint is advisory for UI
        # discovery, not a single point of failure: Cloudflare/local metadata must remain
        # visible when Kaggle rate-limits or transiently rejects `kernels list`.
        try:
            remote = ledger_mod.discover_jobs(client)
        except Exception as exc:
            log.warning("Kaggle bulk job listing degraded; serving durable metadata: %s", exc)
            remote = []
        merged = []
''',
)

# 4) Backward compatibility for workflows created before launch_config existed: inherit
# predecessor dataset sources. The actual ORCA direct URL was intentionally not persisted,
# so old link-only workflows still need a new submission; dataset-backed workflows recover.
replace_once(
    "orca_orchestrator/service.py",
    '''                    launch_cfg = dict(wf.metadata.get("launch_config") or {})
                    sub = self.submit(
                        creds,
                        input_filename=f"{slugify(step.step_name) or 'step'}-{step.step_index}.inp",
                        input_content=input_content,
                        job_name=f"{wf.title}-{step.step_name}",
                        aux_files=step_aux,
                        dataset_sources=list(launch_cfg.get("dataset_sources") or []),
                        orca_link=launch_cfg.get("orca_link") or None,
''',
    '''                    launch_cfg = dict(wf.metadata.get("launch_config") or {})
                    step_datasets = list(launch_cfg.get("dataset_sources") or [])
                    if not step_datasets and parent_job_id:
                        parent_manifest = self.store.get_job(parent_job_id)
                        if parent_manifest:
                            step_datasets = list(parent_manifest.dataset_sources or [])
                    sub = self.submit(
                        creds,
                        input_filename=f"{slugify(step.step_name) or 'step'}-{step.step_index}.inp",
                        input_content=input_content,
                        job_name=f"{wf.title}-{step.step_name}",
                        aux_files=step_aux,
                        dataset_sources=step_datasets,
                        orca_link=launch_cfg.get("orca_link") or None,
''',
)
