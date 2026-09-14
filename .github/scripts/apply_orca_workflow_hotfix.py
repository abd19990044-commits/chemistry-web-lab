from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    assert count == 1, f"{path}: expected exactly one match, found {count}"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# API: workflow creation referenced an undefined local variable `steps`.
replace_once(
    "orca_orchestrator/api.py",
    'res = service.submit_workflow(creds, title=title, steps=steps, dataset_sources=datasets, orca_link=orca_link)',
    'res = service.submit_workflow(creds, title=title, steps=payload.get("steps") or [], dataset_sources=datasets, orca_link=orca_link)',
)

# Service imports needed for deterministic workflow handoff.
replace_once(
    "orca_orchestrator/service.py",
    "from dataclasses import dataclass\n",
    "from dataclasses import dataclass\nfrom typing import Any\n",
)
replace_once(
    "orca_orchestrator/service.py",
    "from .orca_artifacts import detect_job_kind\n",
    "from .orca_artifacts import (detect_job_kind, extract_charge_mult,\n                             read_trajectory_frames, set_geometry)\n",
)

# Register durable job metadata BEFORE the Kaggle side effect.
replace_once(
    "orca_orchestrator/service.py",
    '''                self.store.put_job(job, expected_version=job._extra.get("_version"))

                inline = {input_filename: input_content.encode("utf-8")}
''',
    '''                self.store.put_job(job, expected_version=job._extra.get("_version"))

                # Persist durable metadata BEFORE pushing to Kaggle. If this process dies
                # after Kaggle accepts the notebook but before the HTTP request returns,
                # My Jobs can still recover the job from Cloudflare on the next request.
                try:
                    self.cf_controller.register_job(
                        job,
                        workflow_id=workflow_id,
                        parent_job_id=parent_job_id,
                        step_index=step_index,
                        step_count=step_count,
                        step_name=step_name,
                    )
                except Exception as exc:
                    log.warning("Could not pre-register job in Cloudflare control plane: %s", exc)

                inline = {input_filename: input_content.encode("utf-8")}
''',
)

replace_once(
    "orca_orchestrator/service.py",
    '''                # Persist metadata to Cloudflare Control Plane
                try:
                    self.cf_controller.register_job(
                        job,
                        workflow_id=workflow_id,
                        parent_job_id=parent_job_id,
                        step_index=step_index,
                        step_count=step_count,
                        step_name=step_name,
                    )
                except Exception as exc:
                    log.warning("Could not register job in Cloudflare control plane: %s", exc)
''',
    '''                # Finalise the durable metadata with the real Kaggle slug/URL.
                try:
                    self.cf_controller.sync_job_state(job)
                except Exception as exc:
                    log.warning("Could not sync pushed job to Cloudflare control plane: %s", exc)
''',
)

# My Jobs: bulk listing omission is not deletion.
replace_once(
    "orca_orchestrator/service.py",
    '''        # 2. Discover jobs on Kaggle
        remote = ledger_mod.discover_jobs(client)
        merged = []
''',
    '''        # Snapshot durable job metadata after exact per-job reconciliation.
        try:
            cf_by_ref = {j.kaggle_job_ref: j for j in self.cf_controller.client.list_user_jobs(creds.username)}
        except Exception:
            cf_by_ref = {}

        # Reconciliation may have completed a workflow step. Launch at most one READY
        # successor before reading the bulk Kaggle list; the newly submitted job is kept
        # visible from local/Cloudflare metadata even while Kaggle listing catches up.
        try:
            self._drive_ready_workflows(creds)
        except Exception as exc:
            log.warning("Workflow driver degraded during job listing: %s", exc)

        # 2. Discover jobs on Kaggle
        remote = ledger_mod.discover_jobs(client)
        merged = []
''',
)

replace_once(
    "orca_orchestrator/service.py",
    '''        # 3. Include jobs in local store/Cloudflare that are no longer present on Kaggle
        for job in self.store.list_jobs(creds.username):
            if job.job_id not in seen:
                seen.add(job.job_id)
                described = self.describe(job)
                described["deleted_on_kaggle"] = True
                merged.append(described)
''',
    '''        # 3. Include durable/local jobs not returned by the bulk Kaggle listing.
        # `kernels list` is eventually consistent and occasionally incomplete, so an
        # omission must never be translated into "deleted". Exact reconciliation of the
        # Cloudflare record is the source of that flag.
        for job in self.store.list_jobs(creds.username):
            if job.job_id not in seen:
                seen.add(job.job_id)
                described = self.describe(job)
                cf_rec = cf_by_ref.get(job.job_id)
                described["not_returned_by_kaggle_listing"] = True
                described["deleted_on_kaggle"] = bool(getattr(cf_rec, "remote_deleted", False))
                merged.append(described)
''',
)

# Workflow execution helpers.
p = Path("orca_orchestrator/service.py")
text = p.read_text(encoding="utf-8")
marker = "    # -- workflows ---------------------------------------------------------\n"
assert text.count(marker) == 1
helpers = '''    # -- workflow execution -------------------------------------------------
    def _workflow_result_payload(self, creds: KaggleCredentials, parent_job_id: str,
                                 step) -> tuple[str, dict[str, bytes]]:
        """Build the next step from the predecessor's actual calculated geometry."""
        parent = self.store.get_job(parent_job_id)
        zip_path, cleanup_dir = self.fetch_results(creds, parent_job_id)
        if not zip_path or not os.path.exists(zip_path):
            raise ValidationError(
                "the predecessor finished but its Kaggle output is not available yet; "
                "the workflow will retry this READY step"
            )

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                file_names = [n for n in zf.namelist() if not n.endswith("/")]
                xyz_names = [n for n in file_names if n.lower().endswith(".xyz")]
                if not xyz_names:
                    raise ValidationError(
                        "the predecessor result contains no XYZ geometry, so the next "
                        "calculation cannot be started safely"
                    )

                wanted = ""
                if parent and parent.input_filename:
                    wanted = os.path.splitext(os.path.basename(parent.input_filename))[0].lower() + ".xyz"

                def xyz_rank(name: str) -> tuple[int, str]:
                    base = os.path.basename(name).lower()
                    if wanted and base == wanted:
                        return (0, base)
                    if base == "last_geometry.xyz":
                        return (1, base)
                    if not base.endswith("_trj.xyz"):
                        return (2, base)
                    return (3, base)

                geometry_text = None
                for name in sorted(xyz_names, key=xyz_rank):
                    try:
                        raw = zf.read(name).decode("utf-8", errors="replace")
                        frames = read_trajectory_frames(raw, is_text=True)
                    except Exception:
                        frames = []
                    if frames:
                        geometry_text = frames[-1].rstrip() + "\\n"
                        break
                if not geometry_text:
                    raise ValidationError(
                        "XYZ files were present in the predecessor output, but none contained "
                        "a complete geometry frame"
                    )

                geom_name = f"workflow_step_{step.step_index}_geometry.xyz"
                aux = {geom_name: geometry_text.encode("utf-8")}

                geometry_aliases = {"geometry", "xyz", "optimized_geometry", "optimised_geometry"}
                by_base = {os.path.basename(n).lower(): n for n in file_names}
                for requested in step.required_artifacts or []:
                    req = str(requested).strip()
                    if not req or req.lower() in geometry_aliases:
                        continue
                    match = by_base.get(os.path.basename(req).lower())
                    if not match:
                        matches = [n for n in file_names if n.lower().endswith(req.lower())]
                        match = matches[0] if matches else None
                    if not match:
                        raise ValidationError(
                            f"required workflow artefact '{req}' is absent from predecessor "
                            f"job {parent_job_id}"
                        )
                    data = zf.read(match)
                    if len(data) > CONFIG.runner.inline_carry_limit_bytes // 2:
                        raise ValidationError(
                            f"required workflow artefact '{req}' is too large for safe inline "
                            "handoff; put it in a Kaggle Dataset or use a restartable single job"
                        )
                    aux[os.path.basename(match)] = data

            template = step.input_template or ""
            if not template.strip():
                raise ValidationError(f"workflow step {step.step_index} has no ORCA input template")
            charge, mult = extract_charge_mult(template)
            rewritten = set_geometry(template, geom_name, charge, mult)
            if rewritten == template and not re.search(r"(?i)\\*\\s*xyzfile\\b", template):
                raise ValidationError(
                    "the next workflow step has no replaceable ORCA geometry block; include "
                    "a '* xyz charge multiplicity ... *' block in its input template"
                )
            return rewritten, aux
        finally:
            if cleanup_dir:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    def _drive_ready_workflows(self, creds: KaggleCredentials,
                               workflow_id: str | None = None) -> list[dict[str, Any]]:
        """Launch READY workflow steps exactly once using durable Cloudflare fencing."""
        owner = creds.username.lower()
        workflows = ([self.cf_controller.get_workflow(workflow_id, owner)] if workflow_id
                     else self.cf_controller.list_user_workflows(owner))
        workflows = [wf for wf in workflows if wf is not None]
        try:
            jobs = self.cf_controller.client.list_user_jobs(owner)
        except Exception:
            jobs = []
        existing = {(j.workflow_id, int(j.step_index)): j for j in jobs if j.workflow_id}
        launched = []

        for wf in workflows:
            if wf.status in ("COMPLETED", "FAILED", "PAUSED"):
                continue
            for step in sorted(wf.steps, key=lambda s: s.step_index):
                if step.status != "READY":
                    continue

                prior = existing.get((wf.workflow_id, int(step.step_index)))
                if prior:
                    step.job_id = prior.kaggle_job_ref
                    if prior.local_state in ("COMPLETED", "REMOTE_COMPLETED"):
                        step.status = "COMPLETED"
                    elif prior.local_state in ("FAILED", "REMOTE_FAILED", "REMOTE_DELETED"):
                        step.status = "FAILED"
                    else:
                        step.status = "RUNNING"
                    wf.current_step_index = max(wf.current_step_index, step.step_index)
                    wf.updated_at = now()
                    self.cf_controller.client.put_workflow(wf)
                    break

                parent_step = None
                if step.prerequisites:
                    completed = [wf.get_step(i) for i in step.prerequisites]
                    completed = [s for s in completed if s and s.status == "COMPLETED" and s.job_id]
                    if completed:
                        parent_step = max(completed, key=lambda s: s.step_index)
                elif step.step_index > 0:
                    candidate = wf.get_step(step.step_index - 1)
                    if candidate and candidate.status == "COMPLETED" and candidate.job_id:
                        parent_step = candidate

                try:
                    if step.step_index > 0:
                        if parent_step is None:
                            raise ValidationError(
                                f"workflow step {step.step_index} is READY but has no completed "
                                "predecessor with a job id"
                            )
                        input_content, step_aux = self._workflow_result_payload(
                            creds, parent_step.job_id, step
                        )
                        parent_job_id = parent_step.job_id
                    else:
                        input_content = step.input_template
                        step_aux = {}
                        parent_job_id = None

                    launch_cfg = dict(wf.metadata.get("launch_config") or {})
                    sub = self.submit(
                        creds,
                        input_filename=f"{slugify(step.step_name) or 'step'}-{step.step_index}.inp",
                        input_content=input_content,
                        job_name=f"{wf.title}-{step.step_name}",
                        aux_files=step_aux,
                        dataset_sources=list(launch_cfg.get("dataset_sources") or []),
                        orca_link=launch_cfg.get("orca_link") or None,
                        idempotency_key=f"workflow:{wf.workflow_id}:step:{step.step_index}",
                        workflow_id=wf.workflow_id,
                        parent_job_id=parent_job_id,
                        step_index=step.step_index,
                        step_count=len(wf.steps),
                        step_name=step.step_name,
                    )
                except Exception as exc:
                    step.result_data["last_launch_error"] = f"{type(exc).__name__}: {exc}"
                    step.result_data["last_launch_attempt_at"] = now()
                    wf.updated_at = now()
                    self.cf_controller.client.put_workflow(wf)
                    log.warning("Could not launch READY workflow %s step %s: %s",
                                wf.workflow_id, step.step_index, exc)
                    break

                step.job_id = sub.job_id
                step.status = "RUNNING"
                step.result_data.pop("last_launch_error", None)
                wf.status = "IN_PROGRESS"
                wf.current_step_index = step.step_index
                wf.updated_at = now()
                self.cf_controller.client.put_workflow(wf)
                launched.append({"workflow_id": wf.workflow_id,
                                 "step_index": step.step_index,
                                 "job_id": sub.job_id})
                break
        return launched

    def _reconcile_and_drive_workflows(self, creds: KaggleCredentials) -> None:
        """Watchdog hook: advance workflows even when the browser is closed."""
        try:
            self.cf_controller.reconcile_user_session(creds, KaggleClient(creds))
            self._drive_ready_workflows(creds)
        except Exception as exc:
            log.warning("Background workflow reconciliation degraded: %s", exc)

'''
text = text.replace(marker, helpers + marker, 1)
p.write_text(text, encoding="utf-8")

# Persist dataset/link launch configuration and reconcile workflow polling.
replace_once(
    "orca_orchestrator/service.py",
    '''        wf = self.cf_controller.register_workflow(title=title, owner=creds.username, step_specs=steps)

        # Launch Step 0
''',
    '''        wf = self.cf_controller.register_workflow(title=title, owner=creds.username, step_specs=steps)
        wf.metadata["launch_config"] = {
            "dataset_sources": list(dataset_sources or []),
            "orca_link": orca_link,
        }
        self.cf_controller.client.put_workflow(wf)

        # Launch Step 0
''',
)

replace_once(
    "orca_orchestrator/service.py",
    '''    def get_workflow(self, creds: KaggleCredentials, workflow_id: str) -> dict[str, Any] | None:
        BROKER.remember(creds)
        wf = self.cf_controller.get_workflow(workflow_id, creds.username)
        return wf.to_dict() if wf else None

    def list_workflows(self, creds: KaggleCredentials) -> list[dict[str, Any]]:
        BROKER.remember(creds)
        wfs = self.cf_controller.list_user_workflows(creds.username)
        return [w.to_dict() for w in wfs]
''',
    '''    def get_workflow(self, creds: KaggleCredentials, workflow_id: str) -> dict[str, Any] | None:
        BROKER.remember(creds)
        try:
            self.cf_controller.reconcile_user_session(creds, KaggleClient(creds))
            self._drive_ready_workflows(creds, workflow_id=workflow_id)
        except Exception as exc:
            log.warning("Workflow reconciliation degraded for %s: %s", workflow_id, exc)
        wf = self.cf_controller.get_workflow(workflow_id, creds.username)
        return wf.to_dict() if wf else None

    def list_workflows(self, creds: KaggleCredentials) -> list[dict[str, Any]]:
        BROKER.remember(creds)
        try:
            self.cf_controller.reconcile_user_session(creds, KaggleClient(creds))
            self._drive_ready_workflows(creds)
        except Exception as exc:
            log.warning("Workflow listing reconciliation degraded: %s", exc)
        wfs = self.cf_controller.list_user_workflows(creds.username)
        return [w.to_dict() for w in wfs]
''',
)

# Wire background watchdog driver.
replace_once(
    "orca_orchestrator/service.py",
    '''        self.store = store or get_store()
        self.reconciler = Reconciler(self.store)
        self.watchdog = Watchdog(self.store, self.reconciler, BROKER)
        self.cf_controller = CloudflareController(store=self.store)
        self.result_store = ResultArtifactStore()
''',
    '''        self.store = store or get_store()
        self.reconciler = Reconciler(self.store)
        self.cf_controller = CloudflareController(store=self.store)
        self.result_store = ResultArtifactStore()
        self.watchdog = Watchdog(
            self.store, self.reconciler, BROKER,
            workflow_driver=self._reconcile_and_drive_workflows,
        )
''',
)

replace_once(
    "orca_orchestrator/watchdog.py",
    '''    def __init__(self, store: JobStore, reconciler: Reconciler | None = None,
                 broker: CredentialBroker | None = None, vault_manager: Any | None = None,
                 *, config=CONFIG) -> None:
        self.store = store
        self.reconciler = reconciler or Reconciler(store, config=config)
        self.broker = broker or BROKER
        self.vault_manager = vault_manager
        self.config = config
''',
    '''    def __init__(self, store: JobStore, reconciler: Reconciler | None = None,
                 broker: CredentialBroker | None = None, vault_manager: Any | None = None,
                 *, config=CONFIG, workflow_driver=None) -> None:
        self.store = store
        self.reconciler = reconciler or Reconciler(store, config=config)
        self.broker = broker or BROKER
        self.vault_manager = vault_manager
        self.workflow_driver = workflow_driver
        self.config = config
''',
)

replace_once(
    "orca_orchestrator/watchdog.py",
    '''                    self.reconciler.reconcile(job.job_id, creds, actor="watchdog")
                    result.recovered += 1
''',
    '''                    self.reconciler.reconcile(job.job_id, creds, actor="watchdog")
                    if self.workflow_driver is not None:
                        try:
                            self.workflow_driver(creds)
                        except Exception as exc:
                            log.warning("Workflow driver failed after watchdog reconciliation: %s", exc)
                    result.recovered += 1
''',
)

# Remove the misleading silent artefact-validation no-op.
replace_once(
    "orca_orchestrator/cloudflare_controller/reconciliation.py",
    '''        # Check required artifacts if specified
        for req_art in step.required_artifacts:
            if prereq_step.result_data and not prereq_step.result_data.get(req_art):
                # If result_data specifically misses the required artifact
                pass
''',
    '''        # Required artefact bytes are validated by OrchestratorService immediately
        # before launching the READY step, because Cloudflare stores metadata only.
''',
)
