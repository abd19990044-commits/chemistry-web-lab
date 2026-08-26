# -*- coding: utf-8 -*-
"""
Deep simulation and edge case verification for Cloudflare Recovery & Multi-step Workflows.
"""
from __future__ import annotations

import os
import tempfile
import time
import pytest

from orca_orchestrator.cloudflare_controller import (
    CloudflareCheckpointRecord,
    CloudflareConfig,
    CloudflareController,
    CloudflareHttpClient,
    CloudflareJobRecord,
    CloudflareSecurityViolation,
    CloudflareWorkflowRecord,
    InMemoryCloudflareBackend,
    LocalWorkflowState,
    ReconciliationCase,
    ReconciliationDecision,
    RemoteExecutionState,
    WorkflowStep,
    decide_reconciliation,
    reset_cloudflare_client,
    validate_workflow_step_prerequisites,
)
from orca_orchestrator.config import StoreConfig
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.kaggle_api import KernelStatus, PushResult
from orca_orchestrator.models import CheckpointManifest, CheckpointStatus, FileRecord, JobManifest, now
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.states import JobState
from orca_orchestrator.store import JobStore


@pytest.fixture
def in_memory_backend():
    backend = InMemoryCloudflareBackend()
    reset_cloudflare_client(backend)
    yield backend
    reset_cloudflare_client(None)


@pytest.fixture
def temp_store():
    d = tempfile.mkdtemp(prefix="cf-recov-test-")
    cfg = StoreConfig(state_dir=d, db_filename="test.sqlite3")
    store = JobStore(cfg)
    yield store
    store.close()


class TestMultiStepWorkflowLifecycle:
    """Simulates multi-stage calculations (e.g. OPT -> FREQ -> TD-DFT)."""

    def test_multi_step_progression(self, in_memory_backend, temp_store):
        creds = KaggleCredentials(username="Dr_Chemist", key="abcdef0123456789abcdef0123456789")
        controller = CloudflareController(client=in_memory_backend, store=temp_store)

        # 1. Create a 3-step workflow
        wf = controller.register_workflow(
            title="Naproxen Full Characterization",
            owner=creds.username,
            step_specs=[
                {"step_name": "OPT", "input_template": "! B3LYP Opt"},
                {"step_name": "FREQ", "input_template": "! B3LYP Freq", "prerequisites": [0]},
                {"step_name": "TD-DFT", "input_template": "! CAM-B3LYP", "prerequisites": [1]},
            ],
        )
        assert len(wf.steps) == 3
        assert wf.steps[0].status == "READY"
        assert wf.steps[1].status == "PENDING"
        assert wf.steps[2].status == "PENDING"

        # 2. Step 0 starts
        job0 = JobManifest.create(
            job_id="chem-tools-naproxen-opt",
            owner=creds.username,
            title="Naproxen OPT",
            input_filename="opt.inp",
            original_input_sha256="sha0",
            _extra={"workflow_id": wf.workflow_id, "step_index": 0, "step_name": "OPT"},
        )
        job0.enter_state(JobState.RUNNING)
        temp_store.put_job(job0)
        controller.register_job(job0, workflow_id=wf.workflow_id, step_index=0, step_name="OPT")
        wf.steps[0].job_id = job0.job_id
        wf.steps[0].status = "RUNNING"
        in_memory_backend.put_workflow(wf)

        # 3. Simulate Kaggle client where Step 0 finishes successfully
        class Step0DoneKaggleClient:
            def kernel_exists(self, slug):
                if slug == "chem-tools-naproxen-opt":
                    return KernelStatus(slug=slug, status="complete")
                return None

        # Reconcile user session
        res = controller.reconcile_user_session(creds, kaggle_client=Step0DoneKaggleClient())
        assert res["ok"] is True

        updated_wf = in_memory_backend.get_workflow(wf.workflow_id, creds.username)
        assert updated_wf.steps[0].status == "COMPLETED"
        assert updated_wf.steps[1].status == "READY"    # Prereq satisfied!
        assert updated_wf.steps[2].status == "PENDING"  # Prereq 1 not yet completed


class TestRuntimeExpirationAndContinuation:
    """Simulates 12-hour limit on Kaggle, checkpoint detection, and resumption."""

    def test_stopped_job_with_checkpoint_resumes(self, in_memory_backend, temp_store):
        creds = KaggleCredentials(username="Prof_Quantum", key="0123456789abcdef0123456789abcdef")
        controller = CloudflareController(client=in_memory_backend, store=temp_store)

        # 1. Job was running and saved a checkpoint at epoch 0
        job = JobManifest.create(
            job_id="chem-tools-large-cluster-opt",
            owner=creds.username,
            title="Large Cluster Opt",
            input_filename="cluster.inp",
            original_input_sha256="sha_cluster",
        )
        job.epoch = 0
        job.max_epochs = 5
        job.verified_checkpoint_id = "chk_geom_opt_cycle_42"
        job.enter_state(JobState.RUNNING)
        temp_store.put_job(job)

        cf_rec = controller.register_job(job)
        assert cf_rec.checkpoint_id == "chk_geom_opt_cycle_42"

        # 2. Simulate Kaggle 12-hour limit reached -> kernel status is "stopped"
        class StoppedKaggleClient:
            def kernel_exists(self, slug):
                return KernelStatus(slug=slug, status="stopped")

        # 3. Reconcile
        res = controller.reconcile_user_session(creds, kaggle_client=StoppedKaggleClient())
        assert res["ok"] is True
        assert "chem-tools-large-cluster-opt" in res["resumed_jobs"]

        # Check updated Cloudflare record
        updated_cf_job = in_memory_backend.get_job(job.job_id, creds.username)
        assert updated_cf_job.local_state == LocalWorkflowState.RECOVERY_PENDING.value
        assert updated_cf_job.resume_required is True
        assert updated_cf_job.resume_reason == "kaggle_runtime_expired_checkpoint_available"


class TestCloudflareOutageResilience:
    """Verifies that complete Cloudflare failure does not break local calculations."""

    def test_service_operates_normally_when_cloudflare_is_unreachable(self, temp_store):
        # Configure dead Cloudflare endpoint
        broken_cfg = CloudflareConfig(
            controller_url="http://127.0.0.1:59999/non-existent-endpoint",
            timeout_seconds=0.1,
            max_retries=1,
            enabled=True,
        )
        fallback_backend = InMemoryCloudflareBackend()
        http_client = CloudflareHttpClient(config=broken_cfg, fallback_backend=fallback_backend)

        creds = KaggleCredentials(username="Researcher_Beta", key="112233445566778899aabbccddeeff00")
        service = OrchestratorService(store=temp_store, start_watchdog=False)
        service.cf_controller.client = http_client

        # Mock push_kernel
        class FakeKaggleClient:
            def __init__(self, creds):
                self.creds = creds

            def push_kernel(self, work_dir, expected_slug=None, skip_if_active=False):
                slug = expected_slug or "chem-tools-test"
                return PushResult(slug=slug, owner="researcher_beta", url=f"https://kaggle.com/code/researcher_beta/{slug}", requested_slug=slug)

            def list_kernels(self):
                return []

        import orca_orchestrator.service as s_mod
        orig_kc = s_mod.KaggleClient
        s_mod.KaggleClient = FakeKaggleClient

        try:
            # Submit should succeed without throwing network errors
            sub_res = service.submit(
                creds,
                input_filename="test.inp",
                input_content="! HF 6-31G",
                job_name="Degraded Mode Test",
                dataset_sources=["licensed/orca"],
            )
            assert sub_res.job_id.startswith("chem-tools-")

            # Listing should succeed gracefully
            jobs = service.list_jobs(creds)
            assert len(jobs) >= 1
            assert jobs[0]["job_id"] == sub_res.job_id

            # Status should succeed
            status_data = service.status(creds, sub_res.job_id, reconcile=False)
            assert status_data["job_id"] == sub_res.job_id
        finally:
            s_mod.KaggleClient = orig_kc


class TestCredentialSafetyAndIsolation:
    """Verifies that no secrets exist in Cloudflare payloads and logs."""

    def test_secrets_scrubbed_from_cloudflare_payloads(self):
        job_rec = CloudflareJobRecord(
            internal_job_id="uuid-safe-1",
            kaggle_username="user_secure",
            kaggle_job_ref="chem-tools-sec",
            metadata={"random_info": "safe_val"},
        )
        d = job_rec.to_dict()
        assert "kaggle_key" not in d
        assert "password" not in d
        assert "api_token" not in d

    def test_rejects_payload_with_secret_field(self):
        backend = InMemoryCloudflareBackend()
        job_rec = CloudflareJobRecord(
            internal_job_id="uuid-bad",
            kaggle_username="user_bad",
            kaggle_job_ref="chem-tools-bad",
            metadata={"user_password": "super_secret_password"},
        )
        with pytest.raises(CloudflareSecurityViolation):
            backend.put_job(job_rec)
