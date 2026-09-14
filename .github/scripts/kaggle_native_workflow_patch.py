from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    if old not in text:
        raise SystemExit(f'expected patch anchor missing in {path}: {old[:120]!r}')
    if text.count(old) != 1:
        raise SystemExit(f'patch anchor is not unique in {path}: {old[:120]!r}')
    p.write_text(text.replace(old, new, 1), encoding='utf-8')


# -------------------------------------------------------------------------
# Service: carry a complete workflow plan in the Kaggle job manifest and
# reconstruct workflow views from Kaggle-backed JobManifest objects.
# -------------------------------------------------------------------------
replace_once(
    'orca_orchestrator/service.py',
    '''        step_count: int = 1,\n        step_name: str = "CALC",\n        callback_base_url: str | None = None,\n    ) -> SubmitResult:''',
    '''        step_count: int = 1,\n        step_name: str = "CALC",\n        callback_base_url: str | None = None,\n        workflow_plan: list[dict[str, Any]] | None = None,\n        workflow_title: str | None = None,\n    ) -> SubmitResult:'''
)
replace_once(
    'orca_orchestrator/service.py',
    '''            "name": job_name,\n            "workflow_id": workflow_id,\n            "step_index": step_index,\n        }''',
    '''            "name": job_name,\n            "workflow_id": workflow_id,\n            "step_index": step_index,\n            "workflow_plan": workflow_plan or [],\n            "workflow_title": workflow_title or "",\n        }'''
)
replace_once(
    'orca_orchestrator/service.py',
    '''                        "step_count": step_count,\n                        "step_name": step_name,\n                        "callback_base_url": (callback_base_url or "").rstrip("/"),''',
    '''                        "step_count": step_count,\n                        "step_name": step_name,\n                        "workflow_plan": list(workflow_plan or []),\n                        "workflow_title": workflow_title or job_name or "",\n                        "callback_base_url": (callback_base_url or "").rstrip("/"),'''
)
replace_once(
    'orca_orchestrator/service.py',
    '''            "step_count": extra.get("step_count", 1),\n            "step_name": extra.get("step_name", "CALC"),\n            "resume_required":''',
    '''            "step_count": extra.get("step_count", 1),\n            "step_name": extra.get("step_name", "CALC"),\n            "workflow_title": extra.get("workflow_title"),\n            "resume_required":'''
)

old_workflows = '''    # -- workflows ---------------------------------------------------------\n    # Cloudflare-backed workflow orchestration has been retired. Restart chains\n    # are now Kaggle-native and self-continuing. The legacy workflow API stays\n    # readable so older frontends do not crash, but it no longer creates any\n    # external control-plane dependency.\n    def submit_workflow(self, creds: KaggleCredentials, title: str, steps: list[dict[str, Any]],\n                        *, aux_files: dict[str, bytes] | None = None,\n                        dataset_sources: list[str] | None = None,\n                        orca_link: str | None = None) -> dict[str, Any]:\n        raise ValidationError(\n            "Cloudflare-backed multi-step workflows were removed. Submit the ORCA job "\n            "normally; restart/continuation state is now carried entirely by Kaggle."\n        )\n\n    def get_workflow(self, creds: KaggleCredentials, workflow_id: str) -> dict[str, Any] | None:\n        return None\n\n    def list_workflows(self, creds: KaggleCredentials) -> list[dict[str, Any]]:\n        return []\n'''
new_workflows = '''    # -- Kaggle-native workflows -------------------------------------------\n    @staticmethod\n    def _normalise_workflow_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:\n        """Validate and canonicalise a deterministic linear ORCA workflow.\n\n        A single root Kaggle job owns the whole workflow. Stage transitions are\n        ordinary verified successor handoffs, so STATE.json remains the only\n        durable control plane. Branching DAGs are rejected rather than being\n        approximated with unsafe server-side scheduling.\n        """\n        if not isinstance(steps, list) or not steps:\n            raise ValidationError("A workflow must define at least one step.")\n        if len(steps) > 16:\n            raise ValidationError("A workflow may contain at most 16 sequential steps.")\n\n        plan = []\n        for index, raw in enumerate(steps):\n            if not isinstance(raw, dict):\n                raise ValidationError(f"Workflow step {index} must be an object.")\n            name = str(raw.get("step_name") or raw.get("name") or raw.get("title") or f"STEP {index + 1}").strip()\n            template = str(raw.get("input_template") or raw.get("input_content") or raw.get("input_text") or "")\n            if not template.strip():\n                raise ValidationError(f"Workflow step {index} ({name}) has no ORCA input template.")\n            required = raw.get("required_artifacts") or []\n            if isinstance(required, str):\n                required = [x.strip() for x in required.split(",") if x.strip()]\n            if not isinstance(required, list):\n                raise ValidationError(f"Workflow step {index} required_artifacts must be a list.")\n            required = [os.path.basename(str(x).strip()) for x in required if str(x).strip()]\n            if any(x.lower().endswith(".gbw") for x in required):\n                raise ValidationError(\n                    "Workflow stages cannot depend on a GBW binary restart file. Use geometry/ASCII "\n                    "artifacts (for example .xyz or .hess); the next stage will generate a fresh "\n                    "wavefunction, which is safer across Kaggle session boundaries."\n                )\n            prereq = raw.get("prerequisites")\n            if prereq is None:\n                prereq = [] if index == 0 else [index - 1]\n            try:\n                prereq = [int(x) for x in prereq]\n            except (TypeError, ValueError):\n                raise ValidationError(f"Workflow step {index} has invalid prerequisites.")\n            expected = [] if index == 0 else [index - 1]\n            if prereq != expected:\n                raise ValidationError(\n                    "Kaggle-native workflows are sequential in this release; each step after "\n                    "the first must depend only on the immediately preceding step."\n                )\n            plan.append({\n                "step_index": index,\n                "step_name": name[:80],\n                "input_template": template,\n                "required_artifacts": required,\n                "prerequisites": prereq,\n            })\n        return plan\n\n    @staticmethod\n    def _workflow_view(job: JobManifest) -> dict[str, Any] | None:\n        extra = dict(job._extra or {})\n        workflow_id = extra.get("workflow_id")\n        plan = extra.get("workflow_plan") or []\n        if not workflow_id or not isinstance(plan, list) or not plan:\n            return None\n        current = max(0, min(int(extra.get("step_index", 0) or 0), len(plan) - 1))\n        steps = []\n        for index, spec in enumerate(plan):\n            if index < current:\n                status = "COMPLETED"\n            elif index > current:\n                status = "PENDING"\n            elif job.state == JobState.FINISHED and current == len(plan) - 1:\n                status = "COMPLETED"\n            elif job.state in (JobState.FAILED, JobState.CANCELLED):\n                status = "FAILED"\n            else:\n                status = "RUNNING"\n            steps.append({\n                "step_index": index,\n                "step_name": spec.get("step_name") or f"STEP {index + 1}",\n                "job_id": job.job_id,\n                "status": status,\n                "prerequisites": list(spec.get("prerequisites") or []),\n                "required_artifacts": list(spec.get("required_artifacts") or []),\n            })\n        if job.state == JobState.FINISHED and current == len(plan) - 1:\n            status = "COMPLETED"\n        elif job.state in (JobState.FAILED, JobState.CANCELLED):\n            status = "FAILED"\n        else:\n            status = "IN_PROGRESS"\n        return {\n            "workflow_id": workflow_id,\n            "kaggle_username": job.owner,\n            "title": extra.get("workflow_title") or job.title,\n            "status": status,\n            "steps": steps,\n            "current_step_index": current,\n            "root_job_id": job.job_id,\n            "kaggle_url": job.current_url or f"https://www.kaggle.com/code/{job.owner}/{job.current_slug}",\n            "created_at": job.created_at,\n            "updated_at": job.updated_at,\n            "state_source": "kaggle",\n        }\n\n    def submit_workflow(self, creds: KaggleCredentials, title: str, steps: list[dict[str, Any]],\n                        *, aux_files: dict[str, bytes] | None = None,\n                        dataset_sources: list[str] | None = None,\n                        orca_link: str | None = None,\n                        callback_base_url: str | None = None) -> dict[str, Any]:\n        plan = self._normalise_workflow_steps(steps)\n        workflow_id = new_id("wf_")\n        first = plan[0]\n        result = self.submit(\n            creds,\n            input_filename=f"{slugify(first['step_name']) or 'step-1'}.inp",\n            input_content=first["input_template"],\n            job_name=f"{title}-{first['step_name']}",\n            aux_files=aux_files or {},\n            dataset_sources=dataset_sources or [],\n            orca_link=orca_link,\n            idempotency_key=f"workflow:{creds.fingerprint}:{workflow_id}:step:0",\n            workflow_id=workflow_id,\n            step_index=0,\n            step_count=len(plan),\n            step_name=first["step_name"],\n            callback_base_url=callback_base_url,\n            workflow_plan=plan,\n            workflow_title=title,\n        )\n        job = self.store.require_job(result.job_id)\n        return {\n            "ok": True,\n            "workflow_id": workflow_id,\n            "workflow": self._workflow_view(job),\n            "step_0_job": result.to_dict(),\n        }\n\n    def get_workflow(self, creds: KaggleCredentials, workflow_id: str) -> dict[str, Any] | None:\n        auth = self.ensure_authenticated(creds)\n        self.list_jobs(auth)  # rebuild/warm the local cache from Kaggle first\n        for job in self.store.list_jobs(auth.username):\n            view = self._workflow_view(job)\n            if view and view.get("workflow_id") == workflow_id:\n                return view\n        return None\n\n    def list_workflows(self, creds: KaggleCredentials) -> list[dict[str, Any]]:\n        auth = self.ensure_authenticated(creds)\n        self.list_jobs(auth)  # Kaggle is authoritative; SQLite is only a warm cache.\n        views = []\n        for job in self.store.list_jobs(auth.username):\n            view = self._workflow_view(job)\n            if view:\n                views.append(view)\n        views.sort(key=lambda x: float(x.get("updated_at") or 0), reverse=True)\n        return views\n'''
replace_once('orca_orchestrator/service.py', old_workflows, new_workflows)

# API must pass its own public base URL into workflow jobs too.
replace_once(
    'orca_orchestrator/api.py',
    '''        res = service.submit_workflow(creds, title=title, steps=payload.get("steps") or [], dataset_sources=datasets, orca_link=orca_link)''',
    '''        res = service.submit_workflow(\n            creds, title=title, steps=payload.get("steps") or [],\n            dataset_sources=datasets, orca_link=orca_link,\n            callback_base_url=request.url_root.rstrip("/"),\n        )'''
)

# -------------------------------------------------------------------------
# Builder: workflow metadata must be in every Kaggle window header.
# -------------------------------------------------------------------------
replace_once(
    'orca_orchestrator/runner/builder.py',
    '''        "callback_token": callback_token or (job._extra.get("callback_token") if getattr(job, "_extra", None) else None),\n        "kaggle_username": creds.username,''',
    '''        "callback_token": callback_token or (job._extra.get("callback_token") if getattr(job, "_extra", None) else None),\n        "workflow_id": job._extra.get("workflow_id") if getattr(job, "_extra", None) else None,\n        "workflow_title": job._extra.get("workflow_title") if getattr(job, "_extra", None) else None,\n        "workflow_plan": list(job._extra.get("workflow_plan") or []) if getattr(job, "_extra", None) else [],\n        "workflow_step_index": int(job._extra.get("step_index", 0) or 0) if getattr(job, "_extra", None) else 0,\n        "workflow_step_count": int(job._extra.get("step_count", 1) or 1) if getattr(job, "_extra", None) else 1,\n        "workflow_step_name": job._extra.get("step_name", "CALC") if getattr(job, "_extra", None) else "CALC",\n        "kaggle_username": creds.username,'''
)

# -------------------------------------------------------------------------
# Runner: advance the next workflow stage inside Kaggle itself.
# -------------------------------------------------------------------------
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''    header.setdefault("callback_token", None)\n    header.setdefault("kaggle_username", "")''',
    '''    header.setdefault("callback_token", None)\n    header.setdefault("workflow_id", None)\n    header.setdefault("workflow_title", None)\n    header.setdefault("workflow_plan", [])\n    header.setdefault("workflow_step_index", 0)\n    header.setdefault("workflow_step_count", 1)\n    header.setdefault("workflow_step_name", "CALC")\n    header.setdefault("kaggle_username", "")'''
)
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''def push_successor(manifest, next_epoch, job_kind, cumulative_cycles, disk_epochs):''',
    '''def push_successor(manifest, next_epoch, job_kind, cumulative_cycles, disk_epochs,\n                   header_overrides=None):'''
)
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''        "disk_epochs_used": disk_epochs,\n    })\n\n    job_dir = os.path.join(SCRATCH_ROOT, "next_window", slug)''',
    '''        "disk_epochs_used": disk_epochs,\n    })\n    if header_overrides:\n        header.update(dict(header_overrides))\n\n    job_dir = os.path.join(SCRATCH_ROOT, "next_window", slug)'''
)

workflow_helpers = r'''

def _workflow_plan():
    plan = H.get("workflow_plan") or []
    return plan if isinstance(plan, list) else []


def _workflow_geometry_for_next_step(next_index):
    """Return a verified complete XYZ copied under a stage-stable name."""
    candidates = []
    preferred = wp(BASENAME + ".xyz")
    if os.path.isfile(preferred):
        candidates.append(preferred)
    for path in sorted(glob.glob(wp("*.xyz"))):
        if path not in candidates and not path.lower().endswith("_trj.xyz"):
            candidates.append(path)
    for path in sorted(glob.glob(wp("*_trj.xyz"))):
        if path not in candidates:
            candidates.append(path)

    for path in candidates:
        try:
            frames = art.read_trajectory_frames(path)
        except Exception:
            frames = []
        if not frames:
            continue
        geom_name = "workflow_step_%d_geometry.xyz" % next_index
        geom_path = wp(geom_name)
        atomic_write_bytes(geom_path, (frames[-1].rstrip() + "\n").encode("utf-8"))
        verdict = art.validate_xyz(geom_path)
        if verdict.ok:
            return geom_name, geom_path
    raise RuntimeError(
        "the completed workflow stage produced no complete XYZ geometry; refusing to "
        "launch the next calculation with guessed coordinates"
    )


def _workflow_required_artifacts(step):
    carried = []
    for raw in step.get("required_artifacts") or []:
        name = os.path.basename(str(raw).strip())
        if not name or name.lower() in ("geometry", "xyz", "optimized_geometry", "optimised_geometry"):
            continue
        if name.lower().endswith(".gbw"):
            raise RuntimeError(
                "GBW is intentionally not a workflow dependency across sessions; request "
                "an ASCII artifact such as .xyz or .hess instead"
            )
        exact = wp(name)
        matches = [exact] if os.path.isfile(exact) else sorted(glob.glob(wp("*" + name)))
        matches = [p for p in matches if os.path.isfile(p)]
        if not matches:
            raise RuntimeError("required workflow artifact '%s' is missing" % name)
        carried.append(matches[0])
    return carried


def advance_workflow_stage(outcome, cumulative_cycles, disk_epochs):
    """Commit the completed stage and atomically launch the next stage in Kaggle."""
    plan = _workflow_plan()
    current = int(H.get("workflow_step_index") or 0)
    next_index = current + 1
    if not plan or next_index >= len(plan):
        return None
    step = dict(plan[next_index] or {})
    template = str(step.get("input_template") or "")
    if not template.strip():
        raise RuntimeError("workflow step %d has an empty input template" % next_index)

    geom_name, geom_path = _workflow_geometry_for_next_step(next_index)
    charge, mult = art.extract_charge_mult(template)
    next_text = art.set_geometry(template, geom_name, charge, mult)
    if geom_name not in next_text:
        raise RuntimeError(
            "workflow step %d has no replaceable ORCA geometry block; include a '* xyz ... *' "
            "or '* xyzfile ...' geometry declaration" % next_index
        )
    next_text = art.ensure_simple_keyword(next_text, "NoAutoStart")
    next_kind = art.detect_job_kind(next_text)
    carried = [geom_path] + _workflow_required_artifacts(step)

    write_state(
        "CHECKPOINTING", job_kind=H.get("job_kind") or "unknown",
        cumulative_cycles=cumulative_cycles, disk_epochs=disk_epochs,
        note="completed workflow step %d; staging verified handoff to step %d" % (current, next_index),
        extra={"workflow_step_index": current, "workflow_next_step_index": next_index},
        outcome=outcome,
    )
    manifest, verified = stage_and_verify_checkpoint(
        next_text, carried, next_kind, outcome, 0
    )
    if not verified:
        raise RuntimeError("workflow handoff checkpoint failed verification")

    write_state(
        "RESTARTING", checkpoint=manifest, job_kind=H.get("job_kind") or "unknown",
        cumulative_cycles=cumulative_cycles, disk_epochs=disk_epochs,
        note="workflow handoff verified; launching step %d" % next_index,
        extra={"workflow_step_index": current, "workflow_next_step_index": next_index},
        outcome=outcome,
    )
    next_name = str(step.get("step_name") or ("STEP %d" % (next_index + 1)))
    next_slug, next_url = push_successor(
        manifest, EPOCH + 1, next_kind, 0, disk_epochs,
        header_overrides={
            "workflow_step_index": next_index,
            "workflow_step_count": len(plan),
            "workflow_step_name": next_name,
            "workflow_plan": plan,
            "job_kind": next_kind,
            "cumulative_opt_cycles": 0,
        },
    )
    manifest["status"] = "committed"
    manifest["committed_at"] = time.time()
    atomic_write_json(CHECKPOINT_FILE, manifest)
    write_state(
        "QUEUED", checkpoint=manifest, job_kind=next_kind,
        cumulative_cycles=0, disk_epochs=disk_epochs,
        note="workflow step %d completed; step %d (%s) was accepted by Kaggle" % (
            current, next_index, next_name),
        extra={
            "workflow_step_index": next_index,
            "workflow_step_name": next_name,
            "workflow_step_count": len(plan),
        },
        next_slug=next_slug, next_url=next_url, outcome=outcome,
    )
    emit("workflow_step_advanced", "launched the next workflow stage",
         from_step=current, to_step=next_index, step_name=next_name, next_slug=next_slug)
    return next_slug, next_url
'''
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''\n\n# ---------------------------------------------------------------------------\n# 6. Results\n# ---------------------------------------------------------------------------''',
    workflow_helpers + '''\n\n# ---------------------------------------------------------------------------\n# 6. Results\n# ---------------------------------------------------------------------------'''
)

# Persist workflow data in the durable STATE job object. For a successful
# workflow handoff, `extra.workflow_step_index` names the successor stage.
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''    durable_epoch = EPOCH + 1 if (state == "QUEUED" and next_slug) else EPOCH\n    durable_slug = next_slug if (state == "QUEUED" and next_slug) else (os.environ.get("KAGGLE_KERNEL_SLUG", "") or JOB_ID)\n    job = {''',
    '''    durable_epoch = EPOCH + 1 if (state == "QUEUED" and next_slug) else EPOCH\n    durable_slug = next_slug if (state == "QUEUED" and next_slug) else (os.environ.get("KAGGLE_KERNEL_SLUG", "") or JOB_ID)\n    state_extra = dict(extra or {})\n    durable_step_index = int(state_extra.get("workflow_step_index", H.get("workflow_step_index", 0)) or 0)\n    durable_step_name = state_extra.get("workflow_step_name") or (\n        (H.get("workflow_plan") or [{}])[durable_step_index].get("step_name")\n        if isinstance(H.get("workflow_plan"), list) and durable_step_index < len(H.get("workflow_plan") or [])\n        else H.get("workflow_step_name", "CALC")\n    )\n    job = {'''
)
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''        "job_kind": job_kind,\n        "verified_checkpoint_id":''',
    '''        "job_kind": job_kind,\n        "workflow_id": H.get("workflow_id"),\n        "workflow_title": H.get("workflow_title"),\n        "workflow_plan": H.get("workflow_plan") or [],\n        "step_index": durable_step_index,\n        "step_count": int(H.get("workflow_step_count") or len(H.get("workflow_plan") or []) or 1),\n        "step_name": durable_step_name,\n        "verified_checkpoint_id":'''
)
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''        "extra": dict(extra or {}, next_slug=next_slug, next_url=next_url,\n                      producer_epoch=EPOCH, callback_base_url=H.get("callback_base_url") or "",''',
    '''        "extra": dict(state_extra, next_slug=next_slug, next_url=next_url,\n                      producer_epoch=EPOCH, callback_base_url=H.get("callback_base_url") or "",'''
)

# On genuine stage completion, advance the workflow before declaring the root
# job FINISHED. The final workflow stage retains the normal FINISHED behavior.
replace_once(
    'orca_orchestrator/runner/kernel_runner.py',
    '''    if outcome.is_complete:\n        note = ("The calculation finished: %s. Optimisation cycles in this window: %d; "''',
    '''    if outcome.is_complete:\n        plan = _workflow_plan()\n        current_step = int(H.get("workflow_step_index") or 0)\n        if plan and current_step + 1 < len(plan):\n            try:\n                advance_workflow_stage(outcome, cumulative, disk_epochs)\n                purge_scratch()\n                package_results(note="Workflow stage %d completed; successor stage launched." % current_step)\n                emit("window_end", "workflow stage complete; next stage launched",\n                     wall_seconds=round(time.time() - START_TIME))\n                return 0\n            except Exception as exc:\n                note = ("The ORCA stage completed, but the verified workflow handoff failed: %s. "\n                        "No next stage was guessed or launched." % _scrub(str(exc)))\n                write_state("FAILED", note=note, job_kind=job_kind,\n                            error={"code": "workflow_handoff_failed",\n                                   "detail": _scrub(str(exc))[:400]},\n                            cumulative_cycles=cumulative, disk_epochs=disk_epochs, outcome=outcome)\n                purge_scratch()\n                package_results(note=note)\n                return 1\n        note = ("The calculation finished: %s. Optimisation cycles in this window: %d; "'''
)

# -------------------------------------------------------------------------
# Regression tests for the workflow contract.
# -------------------------------------------------------------------------
Path('tests/test_kaggle_native_workflow.py').write_text(r'''import os

import pytest

from orca_orchestrator.config import StoreConfig
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.store import JobStore
from orca_orchestrator.runner.builder import build_header


def _service(monkeypatch, tmp_path):
    monkeypatch.setenv('ORCA_CALLBACK_SECRET', 'w' * 48)
    monkeypatch.setenv('ORCA_LOCAL_MODE', '1')
    monkeypatch.setenv('ORCA_STATE_DIR', str(tmp_path))
    store = JobStore(StoreConfig(state_dir=str(tmp_path), db_filename='workflow.db'))
    return OrchestratorService(store=store, start_watchdog=False)


def _steps():
    return [
        {
            'step_name': 'OPT',
            'input_template': '! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 1 0 0\n*\n',
        },
        {
            'step_name': 'TDDFT',
            'input_template': '! PBE0 def2-SVP TightSCF\n%tddft nroots 10 end\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 1 0 0\n*\n',
            'prerequisites': [0],
        },
    ]


def test_workflow_submission_is_kaggle_native(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    creds = KaggleCredentials(username='alice', key='0' * 32)
    result = svc.submit_workflow(
        creds, 'water chain', _steps(), dataset_sources=['owner/orca-linux'],
        callback_base_url='https://example.invalid',
    )
    assert result['ok'] is True
    job_id = result['step_0_job']['job_id']
    job = svc.store.require_job(job_id)
    assert job._extra['workflow_id'] == result['workflow_id']
    assert len(job._extra['workflow_plan']) == 2
    assert job._extra['step_index'] == 0
    header = build_header(job=job, epoch=0, creds=creds)
    assert header['workflow_plan'][1]['step_name'] == 'TDDFT'
    assert header['workflow_step_index'] == 0


def test_workflow_rejects_non_linear_dag(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    steps = _steps() + [{
        'step_name': 'SP',
        'input_template': '! B3LYP def2-TZVP\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 1 0 0\n*\n',
        'prerequisites': [0],
    }]
    with pytest.raises(Exception):
        svc._normalise_workflow_steps(steps)


def test_workflow_rejects_gbw_dependency(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    steps = _steps()
    steps[1]['required_artifacts'] = ['molecule.gbw']
    with pytest.raises(Exception):
        svc._normalise_workflow_steps(steps)


def test_runner_contains_in_kaggle_stage_advance():
    src = open('orca_orchestrator/runner/kernel_runner.py', encoding='utf-8').read()
    assert 'def advance_workflow_stage' in src
    assert 'workflow_step_advanced' in src
    assert 'workflow_handoff_failed' in src
    assert 'stage_and_verify_checkpoint(' in src
''', encoding='utf-8')

print('Kaggle-native workflow patch applied')
