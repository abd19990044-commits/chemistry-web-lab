# -*- coding: utf-8 -*-
"""
Cloudflare API Client and In-Memory Backend.

Enforces:
1. NO Kaggle API keys, passwords, or secrets are ever sent.
2. Graceful degradation: If Cloudflare is unreachable, local operations NEVER crash or fail.
3. Thread-safe in-memory backend for testing, offline use, and fallback.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .config import CLOUDFLARE_CONFIG, CloudflareConfig
from .models import (
    CloudflareCheckpointRecord,
    CloudflareCredentialMetadata,
    CloudflareCredentialVaultRecord,
    CloudflareJobRecord,
    CloudflareWorkflowRecord,
    _clean_owner,
)

log = logging.getLogger("orca.cloudflare_client")


class CloudflareSecurityViolation(Exception):
    """Raised if any secret material is detected in Cloudflare payload."""
    pass


def _assert_no_secrets(payload: Any) -> None:
    """Verifies that no API key or credential string is included in data sent to Cloudflare."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            k_lower = str(k).lower()
            if any(forbidden in k_lower for forbidden in ("kaggle_key", "api_token", "password", "raw_secret", "private_key", "kaggle_api_key")):
                raise CloudflareSecurityViolation(f"Attempted to pass forbidden credential field '{k}' to Cloudflare.")
            _assert_no_secrets(v)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_secrets(item)


@dataclass
class PersistenceResult:
    """Explicit report of metadata persistence operation."""

    ok: bool
    durable: bool  # True if confirmed written to Cloudflare HTTP backend, False if in local RAM fallback
    backend: str   # "cloudflare" | "memory_fallback"
    record: Any | None = None
    error: str | None = None


class CloudflareClientProtocol(ABC):
    """Abstract contract for Cloudflare metadata persistence."""

    is_durable: bool = False
    backend_type: str = "unknown"

    @abstractmethod
    def get_job(self, internal_job_id_or_ref: str, owner: str) -> CloudflareJobRecord | None:
        """Fetch job metadata by internal UUID or Kaggle job ref."""
        pass

    @abstractmethod
    def put_job(self, record: CloudflareJobRecord) -> CloudflareJobRecord:
        """Upsert job metadata record."""
        pass

    @abstractmethod
    def list_user_jobs(self, owner: str) -> list[CloudflareJobRecord]:
        """List all known jobs for a user identity."""
        pass

    @abstractmethod
    def delete_job(self, internal_job_id: str, owner: str) -> bool:
        """Remove a job metadata record."""
        pass

    @abstractmethod
    def get_workflow(self, workflow_id: str, owner: str) -> CloudflareWorkflowRecord | None:
        """Fetch workflow metadata by ID."""
        pass

    @abstractmethod
    def put_workflow(self, record: CloudflareWorkflowRecord) -> CloudflareWorkflowRecord:
        """Upsert workflow metadata record."""
        pass

    @abstractmethod
    def list_user_workflows(self, owner: str) -> list[CloudflareWorkflowRecord]:
        """List all workflows for a user identity."""
        pass

    @abstractmethod
    def record_checkpoint(self, checkpoint: CloudflareCheckpointRecord, owner: str) -> bool:
        """Record checkpoint metadata reference. Returns True ONLY if durable across restarts."""
        pass

    @abstractmethod
    def get_credential_vault(self, owner: str) -> CloudflareCredentialVaultRecord | None:
        """Fetch encrypted credential vault record for an owner."""
        pass

    @abstractmethod
    def get_credential_metadata(self, owner: str) -> CloudflareCredentialMetadata:
        """Fetch non-secret credential metadata for an owner."""
        pass

    @abstractmethod
    def put_credential_vault(self, record: CloudflareCredentialVaultRecord) -> bool:
        """Store or update encrypted credential vault record."""
        pass

    @abstractmethod
    def delete_credential_vault(self, owner: str) -> bool:
        """Delete encrypted credential vault record for an owner."""
        pass

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return connectivity and storage status."""
        pass


class InMemoryCloudflareBackend(CloudflareClientProtocol):
    """Thread-safe in-memory backend for tests, offline mode, and local fallback."""

    is_durable: bool = False
    backend_type: str = "memory_fallback"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Key: (owner, internal_job_id) -> CloudflareJobRecord
        self._jobs: dict[tuple[str, str], CloudflareJobRecord] = {}
        # Key: (owner, kaggle_job_ref) -> internal_job_id
        self._ref_index: dict[tuple[str, str], str] = {}
        # Key: (owner, workflow_id) -> CloudflareWorkflowRecord
        self._workflows: dict[tuple[str, str], CloudflareWorkflowRecord] = {}
        # Key: (owner, checkpoint_id) -> CloudflareCheckpointRecord
        self._checkpoints: dict[tuple[str, str], CloudflareCheckpointRecord] = {}
        # Key: owner -> CloudflareCredentialVaultRecord
        self._vault: dict[str, CloudflareCredentialVaultRecord] = {}

    def get_credential_vault(self, owner: str) -> CloudflareCredentialVaultRecord | None:
        owner = _clean_owner(owner)
        with self._lock:
            return self._vault.get(owner)

    def get_credential_metadata(self, owner: str) -> CloudflareCredentialMetadata:
        owner = _clean_owner(owner)
        with self._lock:
            rec = self._vault.get(owner)
            if not rec:
                return CloudflareCredentialMetadata(exists=False, owner=owner)
            return CloudflareCredentialMetadata(
                exists=True,
                owner=rec.owner,
                kaggle_username=rec.kaggle_username,
                status=rec.status,
                encryption_version=rec.encryption_version,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
                last_verified_at=rec.last_verified_at,
            )

    def put_credential_vault(self, record: CloudflareCredentialVaultRecord) -> bool:
        _assert_no_secrets(record.to_dict())
        owner = _clean_owner(record.owner)
        with self._lock:
            self._vault[owner] = record
            return True

    def delete_credential_vault(self, owner: str) -> bool:
        owner = _clean_owner(owner)
        with self._lock:
            return self._vault.pop(owner, None) is not None

    def get_job(self, internal_job_id_or_ref: str, owner: str) -> CloudflareJobRecord | None:
        owner = _clean_owner(owner)
        with self._lock:
            # Try by internal_job_id first
            if (owner, internal_job_id_or_ref) in self._jobs:
                return self._jobs[(owner, internal_job_id_or_ref)]
            # Try by kaggle_job_ref index
            job_id = self._ref_index.get((owner, internal_job_id_or_ref))
            if job_id and (owner, job_id) in self._jobs:
                return self._jobs[(owner, job_id)]
            return None

    def put_job(self, record: CloudflareJobRecord) -> CloudflareJobRecord:
        _assert_no_secrets(record.to_dict())
        if not getattr(record, "cf_sync_status", None):
            record.cf_sync_status = "DEGRADED_UNSYNCED"
        owner = _clean_owner(record.kaggle_username)
        with self._lock:
            self._jobs[(owner, record.internal_job_id)] = record
            if record.kaggle_job_ref:
                self._ref_index[(owner, record.kaggle_job_ref)] = record.internal_job_id
            return record

    def list_user_jobs(self, owner: str) -> list[CloudflareJobRecord]:
        owner = _clean_owner(owner)
        with self._lock:
            user_jobs = [j for (o, _), j in self._jobs.items() if o == owner]
            user_jobs.sort(key=lambda x: x.created_at, reverse=True)
            return user_jobs

    def delete_job(self, internal_job_id_or_ref: str, owner: str) -> bool:
        owner = _clean_owner(owner)
        with self._lock:
            # Try by internal_job_id first
            job = self._jobs.pop((owner, internal_job_id_or_ref), None)
            if job:
                if job.kaggle_job_ref:
                    self._ref_index.pop((owner, job.kaggle_job_ref), None)
                return True
            # Try by kaggle_job_ref in _ref_index
            job_id = self._ref_index.pop((owner, internal_job_id_or_ref), None)
            if job_id:
                job = self._jobs.pop((owner, job_id), None)
                return bool(job)
            return False

    def get_workflow(self, workflow_id: str, owner: str) -> CloudflareWorkflowRecord | None:
        owner = _clean_owner(owner)
        with self._lock:
            return self._workflows.get((owner, workflow_id))

    def put_workflow(self, record: CloudflareWorkflowRecord) -> CloudflareWorkflowRecord:
        _assert_no_secrets(record.to_dict())
        owner = _clean_owner(record.kaggle_username)
        with self._lock:
            self._workflows[(owner, record.workflow_id)] = record
            return record

    def list_user_workflows(self, owner: str) -> list[CloudflareWorkflowRecord]:
        owner = _clean_owner(owner)
        with self._lock:
            user_wf = [w for (o, _), w in self._workflows.items() if o == owner]
            user_wf.sort(key=lambda x: x.created_at, reverse=True)
            return user_wf

    def record_checkpoint(self, checkpoint: CloudflareCheckpointRecord, owner: str) -> bool:
        _assert_no_secrets(checkpoint.to_dict())
        owner = _clean_owner(owner)
        with self._lock:
            self._checkpoints[(owner, checkpoint.checkpoint_id)] = checkpoint
            return False

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "type": "in_memory",
                "jobs_count": len(self._jobs),
                "workflows_count": len(self._workflows),
            }


class CloudflareHttpClient(CloudflareClientProtocol):
    """HTTP Client communicating with Cloudflare REST API / Worker endpoint."""

    is_durable: bool = True
    backend_type: str = "cloudflare"

    def __init__(self, config: CloudflareConfig | None = None, fallback_backend: CloudflareClientProtocol | None = None) -> None:
        self.config = config or CLOUDFLARE_CONFIG
        self.fallback = fallback_backend or InMemoryCloudflareBackend()

    def _make_request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not self.config.controller_url:
            return None

        if payload is not None:
            _assert_no_secrets(payload)

        url = f"{self.config.controller_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "orca-web-lab-controller/1.0",
            "X-Project-Id": self.config.project_id,
            "X-Namespace": self.config.namespace,
        }
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"

        data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

        for attempt in range(max(1, self.config.max_retries)):
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    resp_data = resp.read()
                    if not resp_data:
                        return {"ok": True}
                    return json.loads(resp_data.decode("utf-8"))
            except urllib.error.HTTPError as http_err:
                # If HTTPError (e.g. 400, 404, 409 Conflict), parse response JSON body if available
                try:
                    err_body = http_err.read().decode("utf-8")
                    parsed = json.loads(err_body)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
                log.warning(
                    "Cloudflare request failed with HTTP %d (%s %s, attempt %d/%d): %s",
                    http_err.code,
                    method,
                    path,
                    attempt + 1,
                    self.config.max_retries,
                    http_err,
                )
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                log.warning(
                    "Cloudflare request failed (%s %s, attempt %d/%d): %s",
                    method,
                    path,
                    attempt + 1,
                    self.config.max_retries,
                    exc,
                )
                if attempt < self.config.max_retries - 1:
                    time.sleep(0.2 * (2**attempt))
        return None

    def get_job(self, internal_job_id_or_ref: str, owner: str) -> CloudflareJobRecord | None:
        owner = _clean_owner(owner)
        target = (internal_job_id_or_ref or "").strip()
        if not target:
            return None

        # CW-3: Distinguish slashed Kaggle refs vs internal UUIDs
        try:
            resp = None
            if "/" in target or target.startswith("chem-tools-"):
                quoted_ref = urllib.parse.quote(target, safe="")
                resp = self._make_request("GET", f"api/users/{owner}/jobs/ref?ref={quoted_ref}")
            else:
                resp = self._make_request("GET", f"api/users/{owner}/jobs/id/{target}")
                if not resp or not resp.get("ok"):
                    quoted_ref = urllib.parse.quote(target, safe="")
                    resp = self._make_request("GET", f"api/users/{owner}/jobs/ref?ref={quoted_ref}")

            if resp and resp.get("ok") and resp.get("job"):
                record = CloudflareJobRecord.from_dict(resp["job"])
                record.cf_sync_status = "SYNCED"
                self.fallback.put_job(record)
                return record
        except Exception as exc:
            log.warning("Falling back to local cache on get_job error: %s", exc)

        fallback_rec = self.fallback.get_job(target, owner)
        if fallback_rec and fallback_rec.cf_sync_status != "SYNCED":
            fallback_rec.cf_sync_status = "DEGRADED_UNSYNCED"
        return fallback_rec

    def put_job(self, record: CloudflareJobRecord) -> CloudflareJobRecord:
        _assert_no_secrets(record.to_dict())
        try:
            resp = self._make_request("PUT", f"api/users/{record.kaggle_username}/jobs/{record.internal_job_id}", record.to_dict())
            if resp and resp.get("ok") and resp.get("job"):
                synced = CloudflareJobRecord.from_dict(resp["job"])
                synced.cf_sync_status = "SYNCED"
                self.fallback.put_job(synced)
                return synced
            elif resp and resp.get("ok"):
                record.cf_sync_status = "SYNCED"
                self.fallback.put_job(record)
                return record
            elif resp and resp.get("error", {}).get("code") == "CONFLICT":
                log.warning("Optimistic concurrency conflict for job %s", record.internal_job_id)
                record.cf_sync_status = "CONFLICT_STALE"
                self.fallback.put_job(record)
                return record
        except Exception as exc:
            log.warning("Failed to sync put_job to Cloudflare, retained in local fallback: %s", exc)
        record.cf_sync_status = "DEGRADED_UNSYNCED"
        self.fallback.put_job(record)
        return record

    def list_user_jobs(self, owner: str) -> list[CloudflareJobRecord]:
        owner = _clean_owner(owner)
        try:
            resp = self._make_request("GET", f"api/users/{owner}/jobs")
            if resp and resp.get("ok") and "jobs" in resp:
                jobs = [CloudflareJobRecord.from_dict(j) for j in resp["jobs"]]
                for j in jobs:
                    j.cf_sync_status = "SYNCED"
                    self.fallback.put_job(j)
                return jobs
        except Exception as exc:
            log.warning("Failed to list_user_jobs from Cloudflare, returning local fallback: %s", exc)
        return self.fallback.list_user_jobs(owner)

    def delete_job(self, internal_job_id: str, owner: str) -> bool:
        owner = _clean_owner(owner)
        target = (internal_job_id or "").strip()
        self.fallback.delete_job(target, owner)
        try:
            if "/" in target or target.startswith("chem-tools-"):
                quoted_ref = urllib.parse.quote(target, safe="")
                self._make_request("DELETE", f"api/users/{owner}/jobs/ref?ref={quoted_ref}")
            else:
                self._make_request("DELETE", f"api/users/{owner}/jobs/id/{target}")
        except Exception:
            pass
        return True

    def get_workflow(self, workflow_id: str, owner: str) -> CloudflareWorkflowRecord | None:
        owner = _clean_owner(owner)
        try:
            resp = self._make_request("GET", f"api/users/{owner}/workflows/{workflow_id}")
            if resp and resp.get("ok") and resp.get("workflow"):
                wf = CloudflareWorkflowRecord.from_dict(resp["workflow"])
                self.fallback.put_workflow(wf)
                return wf
        except Exception as exc:
            log.warning("Falling back to local cache on get_workflow error: %s", exc)
        return self.fallback.get_workflow(workflow_id, owner)

    def put_workflow(self, record: CloudflareWorkflowRecord) -> CloudflareWorkflowRecord:
        _assert_no_secrets(record.to_dict())
        try:
            resp = self._make_request("PUT", f"api/users/{record.kaggle_username}/workflows/{record.workflow_id}", record.to_dict())
            if resp and resp.get("ok") and resp.get("workflow"):
                synced = CloudflareWorkflowRecord.from_dict(resp["workflow"])
                self.fallback.put_workflow(synced)
                return synced
            elif resp and resp.get("ok"):
                self.fallback.put_workflow(record)
                return record
        except Exception as exc:
            log.warning("Failed to sync put_workflow to Cloudflare: %s", exc)
        self.fallback.put_workflow(record)
        return record

    def list_user_workflows(self, owner: str) -> list[CloudflareWorkflowRecord]:
        owner = _clean_owner(owner)
        try:
            resp = self._make_request("GET", f"api/users/{owner}/workflows")
            if resp and resp.get("ok") and "workflows" in resp:
                wfs = [CloudflareWorkflowRecord.from_dict(w) for w in resp["workflows"]]
                for w in wfs:
                    self.fallback.put_workflow(w)
                return wfs
        except Exception as exc:
            log.warning("Failed to list_user_workflows from Cloudflare: %s", exc)
        return self.fallback.list_user_workflows(owner)

    def record_checkpoint(self, checkpoint: CloudflareCheckpointRecord, owner: str) -> bool:
        _assert_no_secrets(checkpoint.to_dict())
        self.fallback.record_checkpoint(checkpoint, owner)
        try:
            resp = self._make_request("POST", f"api/users/{owner}/checkpoints", checkpoint.to_dict())
            if resp and resp.get("ok"):
                return True
        except Exception as exc:
            log.warning("Failed to persist checkpoint to Cloudflare HTTP backend: %s", exc)
        return False

    def get_credential_vault(self, owner: str) -> CloudflareCredentialVaultRecord | None:
        owner = _clean_owner(owner)
        try:
            resp = self._make_request("GET", f"api/users/{owner}/credentials/kaggle?vault=1")
            if resp and resp.get("ok") and resp.get("credential"):
                vault_rec = CloudflareCredentialVaultRecord.from_dict(resp["credential"])
                self.fallback.put_credential_vault(vault_rec)
                return vault_rec
            elif resp and resp.get("ok") is False:
                return None
        except Exception as exc:
            log.warning("Failed to fetch credential vault from Cloudflare, checking fallback: %s", exc)
        return self.fallback.get_credential_vault(owner)

    def get_credential_metadata(self, owner: str) -> CloudflareCredentialMetadata:
        owner = _clean_owner(owner)
        try:
            resp = self._make_request("GET", f"api/users/{owner}/credentials/kaggle")
            if resp and resp.get("ok") and resp.get("credential"):
                return CloudflareCredentialMetadata.from_dict(resp["credential"])
        except Exception as exc:
            log.warning("Failed to fetch credential metadata from Cloudflare: %s", exc)
        return self.fallback.get_credential_metadata(owner)

    def put_credential_vault(self, record: CloudflareCredentialVaultRecord) -> bool:
        _assert_no_secrets(record.to_dict())
        self.fallback.put_credential_vault(record)
        owner = _clean_owner(record.owner)
        try:
            resp = self._make_request("PUT", f"api/users/{owner}/credentials/kaggle", record.to_dict())
            if resp and resp.get("ok"):
                return True
        except Exception as exc:
            log.warning("Failed to persist credential vault to Cloudflare HTTP backend: %s", exc)
        return False

    def delete_credential_vault(self, owner: str) -> bool:
        owner = _clean_owner(owner)
        self.fallback.delete_credential_vault(owner)
        try:
            resp = self._make_request("DELETE", f"api/users/{owner}/credentials/kaggle")
            if resp and resp.get("ok"):
                return True
        except Exception as exc:
            log.warning("Failed to delete credential vault from Cloudflare HTTP backend: %s", exc)
        return False

    def health(self) -> dict[str, Any]:
        resp = self._make_request("GET", "health")
        if resp and resp.get("ok"):
            return {"ok": True, "type": "http", "remote": resp}
        return {"ok": True, "type": "degraded_fallback", "fallback": self.fallback.health()}


_CLIENT_INSTANCE: CloudflareClientProtocol | None = None
_CLIENT_LOCK = threading.Lock()


def get_cloudflare_client(config: CloudflareConfig | None = None) -> CloudflareClientProtocol:
    """Returns the singleton Cloudflare client instance."""
    global _CLIENT_INSTANCE
    with _CLIENT_LOCK:
        if _CLIENT_INSTANCE is None:
            cfg = config or CLOUDFLARE_CONFIG
            if cfg.controller_url and cfg.enabled:
                _CLIENT_INSTANCE = CloudflareHttpClient(cfg)
            else:
                _CLIENT_INSTANCE = InMemoryCloudflareBackend()
        return _CLIENT_INSTANCE


def reset_cloudflare_client(instance: CloudflareClientProtocol | None = None) -> None:
    """Allows resetting or injecting a mock client for testing."""
    global _CLIENT_INSTANCE
    with _CLIENT_LOCK:
        _CLIENT_INSTANCE = instance
