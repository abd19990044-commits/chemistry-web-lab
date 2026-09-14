# -*- coding: utf-8 -*-
"""
Verification suite for Cloudflare persistent control plane and recovery subsystem.
"""
from __future__ import annotations

import os
import tempfile
import time
import pytest

from orca_orchestrator.cloudflare_controller import (
    CLOUDFLARE_CONFIG,
    CloudflareCheckpointRecord,
    CloudflareClientProtocol,
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
    get_cloudflare_client,
    reset_cloudflare_client,
    validate_workflow_step_prerequisites,
)
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.kaggle_api import KernelStatus
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
    d = tempfile.mkdtemp(prefix="cf-store-test-")
    from orca_orchestrator.config import StoreConfig
    cfg = StoreConfig(state_dir=d, db_filename="test.sqlite3")
    store = JobStore(cfg)
    yield store
    store.close()


class TestCloudflareDataModel:
    """Tests for metadata schema, versioning, and conversion methods."""

    def test_job_record_creation_and_defaults(self):
        rec = CloudflareJobRecord(
            internal_job_id="test-uuid-1",
            kaggle_username="Researcher_1",
            kaggle_job_ref="chem-tools-opt-1234",
            title="Water Opt",
        )
        assert rec.kaggle_username == "researcher_1"  # lowercased
        assert rec.schema_version == 1
        assert rec.local_state == LocalWorkflowState.CREATED.value
        assert rec.last_seen_remote_state == RemoteExecutionState.UNKNOWN.value

    def test_job_record_serialization_round_trip(self):
        rec = CloudflareJobRecord(
            internal_job_id="test-uuid-2",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-h2o",
            kaggle_url="https://kaggle.com/code/user1/chem-tools-h2o",
            title="H2O Freq",
            workflow_id="wf-123",
            parent_job_id="parent-456",
            step_index=1,
            step_count=3,
            step_name="FREQ",
            checkpoint_id="chk_001",
        )
        d = rec.to_dict()
        assert d["internal_job_id"] == "test-uuid-2"
        assert d["workflow_id"] == "wf-123"
        assert d["step_name"] == "FREQ"

        restored = CloudflareJobRecord.from_dict(d)
        assert restored.internal_job_id == rec.internal_job_id
        assert restored.workflow_id == rec.workflow_id
        assert restored.step_name == rec.step_name
        assert restored.checkpoint_id == rec.checkpoint_id

    def test_job_record_from_and_to_job_manifest(self):
        manifest = JobManifest.create(
            job_id="chem-tools-benzene-opt",
            owner="Alice",
            title="Benzene Opt",
            input_filename="benzene.inp",
            original_input_sha256="abc123sha",
        )
        manifest.enter_state(JobState.RUNNING)
        manifest.verified_checkpoint_id = "chk_opt_final"

        rec = CloudflareJobRecord.from_job_manifest(
            manifest,
            workflow_id="wf_benzene",
            step_index=0,
            step_count=2,
            step_name="OPT",
        )
        assert rec.kaggle_username == "alice"
        assert rec.kaggle_job_ref == "chem-tools-benzene-opt"
        assert rec.checkpoint_id == "chk_opt_final"
        assert rec.step_name == "OPT"

        restored_manifest = rec.to_job_manifest()
        assert restored_manifest.job_id == manifest.job_id
        assert restored_manifest.owner == "alice"
        assert restored_manifest.state == JobState.RUNNING
        assert restored_manifest.verified_checkpoint_id == "chk_opt_final"
        assert restored_manifest._extra.get("workflow_id") == "wf_benzene"

    def test_workflow_record_and_step_prerequisites(self):
        step0 = WorkflowStep(step_index=0, step_name="OPT", status="COMPLETED")
        step1 = WorkflowStep(step_index=1, step_name="FREQ", status="PENDING", prerequisites=[0])
        step2 = WorkflowStep(step_index=2, step_name="TD-DFT", status="PENDING", prerequisites=[1])

        wf = CloudflareWorkflowRecord(
            workflow_id="wf-poly-1",
            kaggle_username="Bob",
            title="Naproxen Multi-stage",
            steps=[step0, step1, step2],
        )
        assert wf.kaggle_username == "bob"
        assert wf.is_step_ready(0) is False  # already completed
        assert wf.is_step_ready(1) is True   # prereq 0 is COMPLETED
        assert wf.is_step_ready(2) is False  # prereq 1 is still PENDING

        d = wf.to_dict()
        restored = CloudflareWorkflowRecord.from_dict(d)
        assert len(restored.steps) == 3
        assert restored.steps[1].step_name == "FREQ"


class TestCloudflareSecurity:
    """Ensures no credential or secret material is ever sent to or stored in Cloudflare."""

    def test_secrets_rejected_by_in_memory_backend(self, in_memory_backend):
        # Direct attempt to put a record with secret fields
        rec = CloudflareJobRecord(
            internal_job_id="sec-1",
            kaggle_username="alice",
            kaggle_job_ref="chem-tools-1",
            metadata={"kaggle_key": "secret_key_12345"},  # Forbidden field
        )
        with pytest.raises(CloudflareSecurityViolation):
            in_memory_backend.put_job(rec)

    def test_to_dict_scrubs_forbidden_fields(self):
        rec = CloudflareJobRecord(
            internal_job_id="sec-2",
            kaggle_username="alice",
            kaggle_job_ref="chem-tools-2",
        )
        d = rec.to_dict()
        assert "kaggle_key" not in d
        assert "api_token" not in d
        assert "password" not in d

    def test_cross_user_isolation(self, in_memory_backend):
        rec_alice = CloudflareJobRecord(
            internal_job_id="alice-job-1",
            kaggle_username="alice",
            kaggle_job_ref="chem-tools-alice-1",
        )
        rec_bob = CloudflareJobRecord(
            internal_job_id="bob-job-1",
            kaggle_username="bob",
            kaggle_job_ref="chem-tools-bob-1",
        )
        in_memory_backend.put_job(rec_alice)
        in_memory_backend.put_job(rec_bob)

        alice_jobs = in_memory_backend.list_user_jobs("alice")
        bob_jobs = in_memory_backend.list_user_jobs("bob")

        assert len(alice_jobs) == 1
        assert alice_jobs[0].internal_job_id == "alice-job-1"
        assert len(bob_jobs) == 1
        assert bob_jobs[0].internal_job_id == "bob-job-1"

        # Alice cannot fetch Bob's job
        assert in_memory_backend.get_job("bob-job-1", "alice") is None


class TestDeterministicReconciliationMatrix:
    """Tests the exact Case A - Case F deterministic reconciliation logic."""

    def test_case_a_reconnect_running(self):
        rec = CloudflareJobRecord(
            internal_job_id="j1",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-j1",
            local_state=LocalWorkflowState.RUNNING.value,
        )
        remote = KernelStatus(slug="chem-tools-j1", status="running")
        decision = decide_reconciliation(rec, remote)
        assert decision.case == ReconciliationCase.CASE_A_RUNNING
        assert decision.action == "RECONNECT"
        assert decision.local_state == LocalWorkflowState.RUNNING.value
        assert decision.remote_state == RemoteExecutionState.RUNNING.value

    def test_case_b_remote_completed(self):
        rec = CloudflareJobRecord(
            internal_job_id="j2",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-j2",
            local_state=LocalWorkflowState.RUNNING.value,
        )
        remote = KernelStatus(slug="chem-tools-j2", status="complete")
        decision = decide_reconciliation(rec, remote)
        assert decision.case == ReconciliationCase.CASE_B_COMPLETED
        assert decision.action == "COMPLETE"
        assert decision.local_state == LocalWorkflowState.COMPLETED.value
        assert decision.result_ready is True

    def test_case_c_remote_failed_with_and_without_checkpoint(self):
        # Without checkpoint
        rec_no_ckpt = CloudflareJobRecord(
            internal_job_id="j3a",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-j3a",
            local_state=LocalWorkflowState.RUNNING.value,
            checkpoint_id=None,
        )
        remote_fail = KernelStatus(slug="chem-tools-j3a", status="error", raw="SCF failed")
        dec_no_ckpt = decide_reconciliation(rec_no_ckpt, remote_fail)
        assert dec_no_ckpt.case == ReconciliationCase.CASE_C_FAILED
        assert dec_no_ckpt.action == "FAIL"
        assert dec_no_ckpt.local_state == LocalWorkflowState.FAILED.value
        assert dec_no_ckpt.resume_required is False

        # With checkpoint and budget
        rec_with_ckpt = CloudflareJobRecord(
            internal_job_id="j3b",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-j3b",
            local_state=LocalWorkflowState.RUNNING.value,
            checkpoint_id="chk_stage1",
            epoch=1,
        )
        dec_with_ckpt = decide_reconciliation(rec_with_ckpt, remote_fail, max_epochs=5)
        assert dec_with_ckpt.case == ReconciliationCase.CASE_C_FAILED
        assert dec_with_ckpt.action == "RECOVERY_PENDING"
        assert dec_with_ckpt.local_state == LocalWorkflowState.RECOVERY_PENDING.value
        assert dec_with_ckpt.resume_required is True

    def test_case_d_remote_stopped_runtime_expired_resumable(self):
        # Calculation stopped due to Kaggle 12-hour limit, verified checkpoint exists
        rec = CloudflareJobRecord(
            internal_job_id="j4",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-j4",
            local_state=LocalWorkflowState.RUNNING.value,
            checkpoint_id="chk_geom_step50",
            epoch=0,
        )
        remote_stopped = KernelStatus(slug="chem-tools-j4", status="stopped")
        decision = decide_reconciliation(rec, remote_stopped, max_epochs=5)
        assert decision.case == ReconciliationCase.CASE_D_STOPPED
        assert decision.action == "RECOVERY_PENDING"
        assert decision.local_state == LocalWorkflowState.RECOVERY_PENDING.value
        assert decision.resume_required is True
        assert decision.resume_reason == "kaggle_runtime_expired_checkpoint_available"

    def test_case_e_remote_deleted_marked_properly(self):
        # Cloudflare thought it was running, but notebook was deleted on Kaggle
        rec = CloudflareJobRecord(
            internal_job_id="j5",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-j5",
            local_state=LocalWorkflowState.RUNNING.value,
        )
        decision = decide_reconciliation(rec, remote_status=None)
        assert decision.case == ReconciliationCase.CASE_E_DELETED
        assert decision.action == "MARK_DELETED"
        assert decision.local_state == LocalWorkflowState.REMOTE_DELETED.value
        assert decision.remote_deleted is True

    def test_case_f_history_preserved_when_completed_job_deleted_remotely(self):
        # Job had completed earlier, notebook was deleted afterwards
        rec = CloudflareJobRecord(
            internal_job_id="j6",
            kaggle_username="user1",
            kaggle_job_ref="chem-tools-j6",
            local_state=LocalWorkflowState.COMPLETED.value,
        )
        decision = decide_reconciliation(rec, remote_status=None)
        assert decision.case == ReconciliationCase.CASE_F_PRESERVED
        assert decision.action == "PRESERVE"
        assert decision.local_state == LocalWorkflowState.COMPLETED.value
        assert decision.result_ready is True
        assert decision.remote_deleted is True


class TestWorkflowPrerequisites:
    """Tests multi-step calculation dependency enforcement."""

    def test_prerequisite_validation_passes_when_prereqs_completed(self):
        wf = CloudflareWorkflowRecord(
            workflow_id="wf_co2",
            kaggle_username="chemist",
            title="CO2 Opt + Freq",
            steps=[
                WorkflowStep(step_index=0, step_name="OPT", status="COMPLETED", job_id="chem-tools-co2-opt"),
                WorkflowStep(step_index=1, step_name="FREQ", status="PENDING", prerequisites=[0], job_id="chem-tools-co2-freq"),
            ],
        )
        completed_jobs = {
            "chem-tools-co2-opt": CloudflareJobRecord(
                internal_job_id="uuid-co2-opt",
                kaggle_username="chemist",
                kaggle_job_ref="chem-tools-co2-opt",
                local_state=LocalWorkflowState.COMPLETED.value,
            )
        }
        step1 = wf.get_step(1)
        ready, reason = validate_workflow_step_prerequisites(step1, wf, completed_jobs)
        assert ready is True

    def test_prerequisite_validation_fails_when_prereq_failed(self):
        wf = CloudflareWorkflowRecord(
            workflow_id="wf_co2_fail",
            kaggle_username="chemist",
            title="CO2 Opt + Freq",
            steps=[
                WorkflowStep(step_index=0, step_name="OPT", status="FAILED", job_id="chem-tools-co2-opt-fail"),
                WorkflowStep(step_index=1, step_name="FREQ", status="PENDING", prerequisites=[0]),
            ],
        )
        completed_jobs = {
            "chem-tools-co2-opt-fail": CloudflareJobRecord(
                internal_job_id="uuid-fail",
                kaggle_username="chemist",
                kaggle_job_ref="chem-tools-co2-opt-fail",
                local_state=LocalWorkflowState.FAILED.value,
            )
        }
        step1 = wf.get_step(1)
        ready, reason = validate_workflow_step_prerequisites(step1, wf, completed_jobs)
        assert ready is False
        assert "not COMPLETED" in reason


class TestRestartRecoveryLifecycle:
    """Simulates Hugging Face restart and full user session reconnect flow."""

    def test_restart_recovery_restores_store_and_workflows(self, in_memory_backend, temp_store):
        creds = KaggleCredentials(username="Researcher_Alpha", key="0123456789abcdef0123456789abcdef")

        # Step 1: Pre-populate Cloudflare with 3 jobs and 1 workflow from earlier runs
        cf_job1 = CloudflareJobRecord(
            internal_job_id="job-1-uuid",
            kaggle_username=creds.username,
            kaggle_job_ref="chem-tools-h2o-opt",
            title="H2O Optimization",
            local_state=LocalWorkflowState.RUNNING.value,
            workflow_id="wf-h2o",
            step_index=0,
            step_name="OPT",
        )
        cf_job2 = CloudflareJobRecord(
            internal_job_id="job-2-uuid",
            kaggle_username=creds.username,
            kaggle_job_ref="chem-tools-h2o-freq",
            title="H2O Frequency",
            local_state=LocalWorkflowState.RUNNING.value,
            workflow_id="wf-h2o",
            step_index=1,
            step_name="FREQ",
            checkpoint_id="chk_freq_step",
            epoch=0,
        )
        cf_job3 = CloudflareJobRecord(
            internal_job_id="job-3-uuid",
            kaggle_username=creds.username,
            kaggle_job_ref="chem-tools-old-done",
            title="Old Completed Calc",
            local_state=LocalWorkflowState.COMPLETED.value,
        )
        in_memory_backend.put_job(cf_job1)
        in_memory_backend.put_job(cf_job2)
        in_memory_backend.put_job(cf_job3)

        wf = CloudflareWorkflowRecord(
            workflow_id="wf-h2o",
            kaggle_username=creds.username,
            title="H2O Full Study",
            steps=[
                WorkflowStep(step_index=0, step_name="OPT", status="RUNNING", job_id="chem-tools-h2o-opt"),
                WorkflowStep(step_index=1, step_name="FREQ", status="RUNNING", job_id="chem-tools-h2o-freq", prerequisites=[0]),
            ],
        )
        in_memory_backend.put_workflow(wf)

        # Step 2: Simulate Space restart (temp_store is completely empty)
        assert len(temp_store.list_jobs(creds.username)) == 0

        # Step 3: Mock Kaggle client responses
        class MockKaggleClient:
            def __init__(self, creds):
                self.creds = creds

            def kernel_exists(self, slug):
                if slug == "chem-tools-h2o-opt":
                    return KernelStatus(slug=slug, status="complete")
                elif slug == "chem-tools-h2o-freq":
                    # Simulated 12h runtime limit reached on Kaggle
                    return KernelStatus(slug=slug, status="stopped")
                elif slug == "chem-tools-old-done":
                    # Notebook was deleted on Kaggle
                    return None
                return None

        mock_client = MockKaggleClient(creds)
        controller = CloudflareController(client=in_memory_backend, store=temp_store)

        # Step 4: Run session reconciliation upon reconnect
        res = controller.reconcile_user_session(creds, kaggle_client=mock_client)

        assert res["ok"] is True
        assert res["reconciled_jobs_count"] == 3
        # Job 2 is stopped with checkpoint -> identified for recovery
        assert "chem-tools-h2o-freq" in res["resumed_jobs"]

        # Step 5: Verify that local JobStore was repopulated across the restart
        stored_jobs = temp_store.list_jobs(creds.username)
        assert len(stored_jobs) == 3

        job1_stored = temp_store.get_job("chem-tools-h2o-opt")
        assert job1_stored is not None
        assert job1_stored.state == JobState.FINISHED

        job2_stored = temp_store.get_job("chem-tools-h2o-freq")
        assert job2_stored is not None
        assert job2_stored.state == JobState.FAILED
        assert job2_stored.verified_checkpoint_id == "chk_freq_step"

        # Step 6: Verify that workflow was updated (Step 0 is COMPLETED, Step 1 is in RECOVERY_PENDING)
        updated_wf = in_memory_backend.get_workflow("wf-h2o", creds.username)
        assert updated_wf is not None
        assert updated_wf.steps[0].status == "COMPLETED"
        assert updated_wf.steps[1].status == "RECOVERY_PENDING"


class TestServiceAndApiIntegration:
    """Verifies service-level workflow submission and list integration."""

    def test_service_submit_and_list_workflows(self, in_memory_backend, temp_store):
        creds = KaggleCredentials(username="Dr_Chemistry", key="112233445566778899aabbccddeeff00")
        service = OrchestratorService(store=temp_store, start_watchdog=False)

        # Mock push_kernel. Workflow durability is now verified through the
        # Kaggle-backed JobManifest rather than an external Cloudflare record. so submission doesn't make real external Kaggle call
        class FakeKaggleClient:
            def __init__(self, creds):
                self.creds = creds

            def push_kernel(self, work_dir, expected_slug=None, skip_if_active=False):
                from orca_orchestrator.kaggle_api import PushResult
                slug = expected_slug or "chem-tools-test"
                return PushResult(slug=slug, owner="dr_chemistry", url=f"https://kaggle.com/code/dr_chemistry/{slug}", requested_slug=slug)

            def list_kernels(self, *args, **kwargs):
                return []

            def kernel_exists(self, slug):
                return True

        import orca_orchestrator.service as s_mod
        orig_kc = s_mod.KaggleClient
        s_mod.KaggleClient = FakeKaggleClient

        try:
            res = service.submit_workflow(
                creds,
                title="Ethanol Study",
                steps=[
                    {"step_name": "OPT", "input_template": "! B3LYP def2-SVP Opt\n* xyz 0 1\nC 0 0 0\n*"},
                    {"step_name": "FREQ", "input_template": "! B3LYP def2-SVP Freq\n* xyz 0 1\nC 0 0 0\n*", "prerequisites": [0]},
                ],
                dataset_sources=["licensed/orca"],
            )
            assert res["ok"] is True
            assert "workflow_id" in res
            assert res["workflow"]["steps"][0]["status"] == "RUNNING"
            assert res["workflow"]["steps"][1]["status"] == "PENDING"

            # Check listing workflows
            wfs = service.list_workflows(creds)
            assert len(wfs) == 1
            assert wfs[0]["title"] == "Ethanol Study"
        finally:
            s_mod.KaggleClient = orig_kc
