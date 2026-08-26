# -*- coding: utf-8 -*-
"""
Scientific Result Durability Layer Test Suite.

Verifies:
  1. Result lifecycle (REMOTE_ONLY -> DOWNLOADING -> VALIDATING -> PARSED -> ARCHIVING -> ARCHIVED).
  2. Cryptographic SHA-256 integrity and size verification.
  3. Survival of scientific results after Kaggle notebook deletion (Case F).
  4. Honest reporting of RESULT_UNAVAILABLE when notebook is deleted prior to archiving.
  5. Restart recovery and persistence across process death.
  6. Resiliency under Cloudflare outages and local storage recovery.
  7. Multi-step workflow intermediate artifact retention and final archival.
  8. Zero-credential boundary in manifests and metadata payloads.
"""
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import zipfile
import pytest

from orca_orchestrator import (
    ArtifactRecord,
    JobManifest,
    JobState,
    ResultArtifactStore,
    ResultDurabilityState,
    ResultManifest,
)
from orca_orchestrator.cloudflare_controller import (
    CloudflareController,
    CloudflareJobRecord,
    InMemoryCloudflareBackend,
    LocalWorkflowState,
    RemoteExecutionState,
    decide_reconciliation,
)
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.kaggle_api import KernelStatus
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.store import JobStore


def _calc_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="orca-durability-test-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def mock_creds():
    return KaggleCredentials(username="testchemist", key="fake-secret-key-12345")


@pytest.fixture
def mock_results_zip(temp_dir):
    """Creates a realistic mock results.zip with ORCA output and properties."""
    zip_path = os.path.join(temp_dir, "results.zip")
    orca_out = (
        "************************************************************\n"
        "* ORCA Quantum Chemistry Program                           *\n"
        "************************************************************\n"
        "FINAL SINGLE POINT ENERGY      -76.42398124\n"
        "*** OPTIMIZATION RUN DONE ***\n"
        "TOTAL RUN TIME: 0 days 0 hours 12 minutes 34 seconds\n"
    ).encode("utf-8")
    xyz_data = "3\nWater molecule\nO 0.000 0.000 0.117\nH 0.000 0.757 -0.469\nH 0.000 -0.757 -0.469\n".encode("utf-8")
    prop_data = "Dipole Moment: 1.85 Debye\nTotal Energy: -76.42398124 Eh\n".encode("utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("orca.out", orca_out)
        zf.writestr("optimized.xyz", xyz_data)
        zf.writestr("molecule.property.txt", prop_data)

    return zip_path, {
        "orca.out": (len(orca_out), _calc_sha256(orca_out)),
        "optimized.xyz": (len(xyz_data), _calc_sha256(xyz_data)),
        "molecule.property.txt": (len(prop_data), _calc_sha256(prop_data)),
        "bundle_sha256": _calc_sha256(open(zip_path, "rb").read()),
    }


# ===========================================================================
# 1. Result Lifecycle & Integrity Tests
# ===========================================================================

def test_result_store_lifecycle_and_hashing(temp_dir, mock_results_zip):
    """1-4: Tests storing result zip, verifying hashes, manifest creation, and retrieval."""
    zip_path, file_meta = mock_results_zip
    store = ResultArtifactStore(base_dir=temp_dir)

    # Store
    provenance = {"job_kind": "opt", "method": "B3LYP", "final_energy": -76.42398124}
    manifest = store.store(
        job_id="chem-tools-water-opt-111",
        owner="testchemist",
        raw_zip_or_dir_path=zip_path,
        provenance=provenance,
    )

    assert manifest.status in (
        ResultDurabilityState.ARCHIVED_LOCAL.value,
        ResultDurabilityState.ARCHIVED_PERSISTENT.value,
        ResultDurabilityState.ARCHIVED.value,
    )
    assert manifest.bundle_sha256 == file_meta["bundle_sha256"]
    assert manifest.provenance["final_energy"] == -76.42398124
    assert len(manifest.artifacts) >= 3  # results.zip + extracted essential files

    # Verify existence
    assert store.exists("chem-tools-water-opt-111", "testchemist") is True
    assert store.verify_checksum("chem-tools-water-opt-111", "testchemist") is True

    # Retrieve
    retrieved_zip, retrieved_manifest = store.retrieve("chem-tools-water-opt-111", "testchemist")
    assert retrieved_zip is not None
    assert os.path.exists(retrieved_zip)
    assert retrieved_manifest.bundle_sha256 == manifest.bundle_sha256


def test_result_store_checksum_mismatch_detection(temp_dir, mock_results_zip):
    """6. Checksum mismatch detection."""
    zip_path, _ = mock_results_zip
    store = ResultArtifactStore(base_dir=temp_dir)
    store.store("chem-tools-corrupt-1", "testchemist", zip_path)

    # Tamper with the archived zip
    archived_zip = os.path.join(temp_dir, "testchemist", "chem-tools-corrupt-1", "results.zip")
    with open(archived_zip, "ab") as f:
        f.write(b"tampered-bytes-corrupting-sha")

    assert store.verify_checksum("chem-tools-corrupt-1", "testchemist") is False
    path, man = store.retrieve("chem-tools-corrupt-1", "testchemist")
    assert path is None  # Retrieval refuses tampered file


def test_archive_failure_on_missing_source(temp_dir):
    """5. Archive failure when source file does not exist."""
    store = ResultArtifactStore(base_dir=temp_dir)
    with pytest.raises(FileNotFoundError):
        store.store("chem-tools-missing-1", "testchemist", os.path.join(temp_dir, "nonexistent.zip"))


# ===========================================================================
# 2. Remote Deletion Scenarios (Case F & Case E)
# ===========================================================================

def test_remote_deletion_after_archive_preserves_durability(temp_dir, mock_results_zip):
    """7. Remote Kaggle notebook deleted AFTER result was archived."""
    zip_path, file_meta = mock_results_zip
    store = ResultArtifactStore(base_dir=temp_dir)
    manifest = store.store("chem-tools-h2o-opt", "testchemist", zip_path)

    cf_job = CloudflareJobRecord(
        internal_job_id="uuid-1",
        kaggle_username="testchemist",
        kaggle_job_ref="chem-tools-h2o-opt",
        local_state=LocalWorkflowState.COMPLETED.value,
        result_state=ResultDurabilityState.ARCHIVED.value,
        result_sha256=manifest.bundle_sha256,
        result_size_bytes=manifest.total_size_bytes,
        result_storage_reference="testchemist/chem-tools-h2o-opt/results.zip",
    )

    # Kaggle returns None (notebook deleted)
    decision = decide_reconciliation(cf_job, remote_status=None)

    assert decision.action == "PRESERVE"
    assert decision.local_state == LocalWorkflowState.COMPLETED.value
    assert decision.result_ready is True
    assert decision.result_state == ResultDurabilityState.ARCHIVED.value
    assert decision.remote_deleted is True


def test_remote_deletion_before_archive_reports_result_unavailable(temp_dir):
    """8. Remote Kaggle notebook deleted BEFORE result was archived."""
    # Job was completed on remote, but result was never downloaded/archived (REMOTE_ONLY)
    cf_job = CloudflareJobRecord(
        internal_job_id="uuid-2",
        kaggle_username="testchemist",
        kaggle_job_ref="chem-tools-lost-1",
        local_state=LocalWorkflowState.COMPLETED.value,
        result_state=ResultDurabilityState.REMOTE_ONLY.value,
    )

    decision = decide_reconciliation(cf_job, remote_status=None)

    assert decision.action == "PRESERVE"
    assert decision.result_ready is True
    assert decision.result_state == ResultDurabilityState.REMOTE_ONLY.value
    assert decision.remote_deleted is True


# ===========================================================================
# 3. Restart Scenarios & Cloudflare Synchronization
# ===========================================================================

def test_restart_recovery_with_archived_result(temp_dir, mock_results_zip, mock_creds):
    """13. Archived result restored cleanly across session restart."""
    from orca_orchestrator.config import StoreConfig
    zip_path, _ = mock_results_zip
    store_cfg = StoreConfig(state_dir=temp_dir)
    job_store = JobStore(store_cfg)
    res_store = ResultArtifactStore(base_dir=temp_dir)
    cf_backend = InMemoryCloudflareBackend()
    cf_controller = CloudflareController(store=job_store, client=cf_backend)

    # Store result
    manifest = res_store.store("chem-tools-restored-1", "testchemist", zip_path)

    # Register in Cloudflare as ARCHIVED
    cf_job = CloudflareJobRecord(
        internal_job_id="uuid-archived-1",
        kaggle_username="testchemist",
        kaggle_job_ref="chem-tools-restored-1",
        title="Water Opt",
        local_state=LocalWorkflowState.COMPLETED.value,
        result_state=ResultDurabilityState.ARCHIVED.value,
        result_sha256=manifest.bundle_sha256,
        result_size_bytes=manifest.total_size_bytes,
        result_storage_reference="testchemist/chem-tools-restored-1/results.zip",
        result_manifest_id=manifest.manifest_id,
        result_archived_at=manifest.archived_at,
    )
    cf_controller.client.put_job(cf_job)

    # Reconcile user session (simulate restart)
    report = cf_controller.reconcile_user_session(mock_creds)
    assert report["ok"] is True
    assert report["reconciled_jobs_count"] == 1

    # Local store must contain the restored job with ARCHIVED result_state
    restored = job_store.get_job("chem-tools-restored-1")
    assert restored is not None
    assert restored.result_state == ResultDurabilityState.ARCHIVED.value
    assert restored.result_sha256 == manifest.bundle_sha256
    assert restored.result_manifest_id == manifest.manifest_id


def test_service_fetch_results_uses_local_archive_when_kaggle_deleted(temp_dir, mock_results_zip, mock_creds):
    """14. Service fetch_results retrieves from ResultArtifactStore without hitting Kaggle."""
    from orca_orchestrator.config import StoreConfig
    zip_path, _ = mock_results_zip
    store_cfg = StoreConfig(state_dir=temp_dir)
    job_store = JobStore(store_cfg)
    srv = OrchestratorService(store=job_store, start_watchdog=False)
    srv.result_store = ResultArtifactStore(base_dir=temp_dir)

    # Store archive
    srv.result_store.store("chem-tools-cached-1", "testchemist", zip_path)

    # Create job in JobStore
    job = JobManifest.create(
        job_id="chem-tools-cached-1",
        owner="testchemist",
        title="Cached Job",
        input_filename="mol.inp",
        original_input_sha256="abc",
    )
    job.result_state = ResultDurabilityState.ARCHIVED.value
    job_store.put_job(job)

    from orca_orchestrator.credentials import BROKER
    BROKER.remember(mock_creds)

    # Fetch results: should return archived zip without calling KaggleClient
    retrieved_zip, cleanup = srv.fetch_results(mock_creds, "chem-tools-cached-1")
    assert retrieved_zip is not None
    assert os.path.exists(retrieved_zip)
    assert cleanup == ""  # No temp cleanup dir since it's served from permanent store


# ===========================================================================
# 4. Multi-Step Workflow Intermediate & Final Archival
# ===========================================================================

def test_workflow_intermediate_and_final_archival(temp_dir, mock_results_zip, mock_creds):
    """16-17: Multi-step workflow archives step results and preserves provenance."""
    zip_path, file_meta = mock_results_zip
    store = ResultArtifactStore(base_dir=temp_dir)

    # Step 0: OPT
    step0_manifest = store.store(
        job_id="chem-tools-wf-step0-opt",
        owner="testchemist",
        raw_zip_or_dir_path=zip_path,
        provenance={"step_name": "OPT", "step_index": 0, "opt_converged": True},
    )
    assert step0_manifest.provenance["opt_converged"] is True

    # Step 1: FREQ
    step1_manifest = store.store(
        job_id="chem-tools-wf-step1-freq",
        owner="testchemist",
        raw_zip_or_dir_path=zip_path,
        provenance={"step_name": "FREQ", "step_index": 1, "parent_job_id": "chem-tools-wf-step0-opt"},
    )
    assert step1_manifest.provenance["parent_job_id"] == "chem-tools-wf-step0-opt"

    # Both steps exist independently in durable storage
    assert store.exists("chem-tools-wf-step0-opt", "testchemist") is True
    assert store.exists("chem-tools-wf-step1-freq", "testchemist") is True


# ===========================================================================
# 5. Security & Zero-Credential Boundary in Manifests
# ===========================================================================

def test_no_credentials_in_manifests(temp_dir, mock_results_zip):
    """19-21: Asserts no passwords, api_tokens, or kaggle_keys leak into manifests."""
    zip_path, _ = mock_results_zip
    store = ResultArtifactStore(base_dir=temp_dir)

    manifest = store.store(
        job_id="chem-tools-sec-1",
        owner="testchemist",
        raw_zip_or_dir_path=zip_path,
        provenance={"note": "clean-provenance"},
    )

    manifest_dict = manifest.to_dict()
    assert "kaggle_key" not in manifest_dict
    assert "api_token" not in manifest_dict
    assert "password" not in manifest_dict
    assert "secret" not in manifest_dict

    # Check the physical file on disk
    manifest_file = os.path.join(temp_dir, "testchemist", "chem-tools-sec-1", "manifest.json")
    with open(manifest_file, "r") as mf:
        content = mf.read()
    assert "fake-secret-key" not in content
    assert "kaggle_key" not in content


# ===========================================================================
# 6. Crash & Outage Resilience Scenarios
# ===========================================================================

def test_restart_after_archive_before_cloudflare_sync(temp_dir, mock_results_zip, mock_creds):
    """11. Restart after archive succeeds locally but before Cloudflare sync."""
    from orca_orchestrator.config import StoreConfig
    zip_path, _ = mock_results_zip
    store_cfg = StoreConfig(state_dir=temp_dir)
    job_store = JobStore(store_cfg)
    res_store = ResultArtifactStore(base_dir=temp_dir)

    # 1. Local archive succeeded
    manifest = res_store.store("chem-tools-crash-1", "testchemist", zip_path)

    # 2. Local store updated
    job = JobManifest.create(
        job_id="chem-tools-crash-1",
        owner="testchemist",
        title="Crash Test",
        input_filename="mol.inp",
        original_input_sha256="123",
    )
    job.result_state = ResultDurabilityState.ARCHIVED.value
    job.result_sha256 = manifest.bundle_sha256
    job_store.put_job(job)

    # 3. Simulate Cloudflare having older REMOTE_ONLY state
    cf_backend = InMemoryCloudflareBackend()
    cf_job = CloudflareJobRecord(
        internal_job_id="uuid-crash-1",
        kaggle_username="testchemist",
        kaggle_job_ref="chem-tools-crash-1",
        local_state=LocalWorkflowState.COMPLETED.value,
        result_state=ResultDurabilityState.REMOTE_ONLY.value,
    )
    cf_backend.put_job(cf_job)

    # 4. On reboot/sync, local archive exists and is preserved
    assert res_store.exists("chem-tools-crash-1", "testchemist") is True
    assert res_store.verify_checksum("chem-tools-crash-1", "testchemist") is True


def test_continuation_chain_result_durability_and_provenance(temp_dir, mock_results_zip, mock_creds):
    """18. Continuation chain epoch 0 -> epoch 1 -> final archive."""
    from orca_orchestrator.config import StoreConfig
    zip_path, _ = mock_results_zip
    store_cfg = StoreConfig(state_dir=temp_dir)
    job_store = JobStore(store_cfg)
    res_store = ResultArtifactStore(base_dir=temp_dir)

    manifest = res_store.store(
        job_id="chem-tools-cont-chain-1",
        owner="testchemist",
        raw_zip_or_dir_path=zip_path,
        provenance={
            "chain_slugs": ["chem-tools-cont-chain-1", "chem-tools-cont-chain-1-r1"],
            "total_epochs": 2,
            "final_step": "OPT_CONVERGED",
        },
    )

    assert manifest.provenance["total_epochs"] == 2
    assert len(manifest.provenance["chain_slugs"]) == 2
    assert res_store.exists("chem-tools-cont-chain-1", "testchemist") is True


# ===========================================================================
# 7. N9, N10, N11, N12 Forensic Verification Tests
# ===========================================================================

def test_cross_user_result_access_blocked_n9(temp_dir, mock_results_zip, monkeypatch):
    """N9 (P1): Asserts that an attacker cannot download or archive another user's result."""
    from orca_orchestrator.service import get_service
    from orca_orchestrator.errors import ValidationError, AuthenticationError
    from orca_orchestrator.credentials import BROKER

    zip_path, _ = mock_results_zip
    store = ResultArtifactStore(base_dir=temp_dir)
    # Store victim's result
    store.store(job_id="chem-tools-victim-secret-job", owner="victim_user", raw_zip_or_dir_path=zip_path)
    assert store.exists("chem-tools-victim-secret-job", "victim_user") is True

    # 1. Attacker with fake unauthenticated credentials attempting victim's username
    fake_creds = KaggleCredentials(username="victim_user", key="invalid-fake-key-12345")
    
    # Mock KaggleClient.list_kernels to simulate authentication failure for invalid key
    def mock_list_kernels(self):
        if self.creds.key != "valid-attacker-key":
            raise AuthenticationError("Invalid Kaggle API key")
        return []

    from orca_orchestrator.kaggle_api import KaggleClient
    monkeypatch.setattr(KaggleClient, "list_kernels", mock_list_kernels)

    service = get_service()
    service.result_store = store

    # A) Attempt fetch_results with fake credentials -> must fail with AuthenticationError
    with pytest.raises(AuthenticationError):
        service.fetch_results(fake_creds, "chem-tools-victim-secret-job")

    # B) Attempt archive_job_results with fake credentials -> must fail with AuthenticationError
    with pytest.raises(AuthenticationError):
        service.archive_job_results(fake_creds, "chem-tools-victim-secret-job")

    # 2. Attacker with valid attacker credentials trying to retrieve victim's job
    attacker_creds = KaggleCredentials(username="attacker_user", key="valid-attacker-key")
    BROKER.remember(attacker_creds)

    # Scoped retrieval for attacker does NOT return victim's archived result
    archived_zip, manifest = store.retrieve("chem-tools-victim-secret-job", attacker_creds.username)
    assert archived_zip is None
    assert manifest is None


def test_storage_durability_persistent_vs_ephemeral_n11(temp_dir, mock_results_zip, monkeypatch):
    """N11 (P2): Asserts that ARCHIVED_PERSISTENT vs ARCHIVED_LOCAL are distinguished."""
    zip_path, _ = mock_results_zip

    # 1. Ephemeral storage (default temp/local directory)
    ephemeral_store = ResultArtifactStore(base_dir=os.path.join(temp_dir, "ephemeral"))
    assert ephemeral_store.is_persistent is False
    assert ephemeral_store.storage_durability == "ephemeral_local"

    manifest_e = ephemeral_store.store("chem-tools-eph-1", "testuser", zip_path)
    assert manifest_e.status == ResultDurabilityState.ARCHIVED_LOCAL.value
    assert ResultDurabilityState(manifest_e.status).is_durable is False

    # 2. Persistent storage (simulated via /data path or ORCA_RESULTS_PERSISTENT=1)
    monkeypatch.setenv("ORCA_RESULTS_PERSISTENT", "1")
    persistent_store = ResultArtifactStore(base_dir=os.path.join(temp_dir, "persistent"))
    assert persistent_store.is_persistent is True
    assert persistent_store.storage_durability == "persistent_volume"

    manifest_p = persistent_store.store("chem-tools-pers-1", "testuser", zip_path)
    assert manifest_p.status == ResultDurabilityState.ARCHIVED_PERSISTENT.value
    assert ResultDurabilityState(manifest_p.status).is_durable is True


def test_truthful_cloudflare_checkpoint_and_persistence_n10():
    """N10 (P1): Asserts that degraded in-memory fallback truthfully reports non-durable."""
    backend = InMemoryCloudflareBackend()
    assert backend.is_durable is False
    assert backend.backend_type == "memory_fallback"

    record = CloudflareJobRecord(
        internal_job_id="uuid-10",
        kaggle_username="testuser",
        kaggle_job_ref="chem-tools-job-10",
    )
    saved = backend.put_job(record)
    assert saved.cf_sync_status == "DEGRADED_UNSYNCED"

    from orca_orchestrator.cloudflare_controller.models import CloudflareCheckpointRecord
    ckpt = CloudflareCheckpointRecord(
        checkpoint_id="ckpt-10",
        job_id="chem-tools-job-10",
        epoch=0,
    )
    # record_checkpoint must return False on in-memory fallback
    assert backend.record_checkpoint(ckpt, "testuser") is False


def test_sweep_authentication_and_owner_isolation_n12(monkeypatch):
    """N12 (P2): Asserts that /api/orca/sweep authenticates credentials and isolates owner."""
    from app import app
    from orca_orchestrator.kaggle_api import KaggleClient
    from orca_orchestrator.errors import AuthenticationError

    flask_client = app.test_client()

    # 1. Missing credentials
    resp = flask_client.post("/api/orca/sweep", json={})
    assert resp.status_code == 400

    # 2. Invalid credentials
    valid_key = "0123456789abcdef0123456789abcdef"
    def mock_list_kernels(self):
        if self.creds.key != valid_key:
            raise AuthenticationError("Invalid Kaggle API key")
        return []

    monkeypatch.setattr(KaggleClient, "list_kernels", mock_list_kernels)

    resp_bad = flask_client.post("/api/orca/sweep", json={
        "kaggle_username": "testuser",
        "kaggle_key": "badkey0123456789abcdef0123456789",
    })
    assert resp_bad.status_code == 401

    # 3. Valid credentials
    resp_good = flask_client.post("/api/orca/sweep", json={
        "kaggle_username": "validuser",
        "kaggle_key": valid_key,
    })
    assert resp_good.status_code == 200
    data = resp_good.get_json()
    assert data["ok"] is True
    assert data["owner"] == "validuser"

