# -*- coding: utf-8 -*-
"""
End-to-End Contract Verification Suite for Cloudflare Workers Control Plane.

Spawns an authentic HTTP server backed by an in-memory SQLite database running
the exact D1 schema from migrations/0001_initial.sql, and verifies full
compatibility with CloudflareHttpClient, all data models, fail-closed auth,
Kaggle slashed slug lookups, optimistic concurrency, and namespace scoping.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest

from orca_orchestrator.cloudflare_controller.client import (
    CloudflareHttpClient,
    CloudflareSecurityViolation,
    InMemoryCloudflareBackend,
)
from orca_orchestrator.cloudflare_controller.config import CloudflareConfig
from orca_orchestrator.cloudflare_controller.models import (
    CloudflareCheckpointRecord,
    CloudflareCredentialMetadata,
    CloudflareCredentialVaultRecord,
    CloudflareJobRecord,
    CloudflareWorkflowRecord,
    WorkflowStep,
)

AUTH_TOKEN = "test_cf_token_secret_12345"
PROJECT_ID = "orca-web-lab"
NAMESPACE = "test-env"


class D1SimulatedServer(BaseHTTPRequestHandler):
    """HTTP Request Handler implementing exact Worker routing, security, and D1 persistence."""

    db: sqlite3.Connection
    expected_token: str | None = AUTH_TOKEN
    expected_project_id: str | None = PROJECT_ID
    expected_namespace: str | None = NAMESPACE

    def log_message(self, format, *args):
        pass  # Suppress default server logs

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        # CW-2: Fail closed if token unconfigured
        if not self.expected_token:
            self._send_json({
                "ok": False,
                "error": {"code": "CONFIG_ERROR", "message": "Control plane authentication token is not configured (FAIL CLOSED)."}
            }, 503)
            return False

        auth = self.headers.get("Authorization", "")
        if not auth:
            self._send_json({"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Missing Authorization header."}}, 401)
            return False

        match = re.match(r"^Bearer\s+(.+)$", auth, re.IGNORECASE)
        if not match:
            self._send_json({"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Malformed Authorization header."}}, 401)
            return False

        if match.group(1).strip() != self.expected_token:
            self._send_json({"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Invalid API token."}}, 401)
            return False

        # CW-6: Validate Project ID & Namespace
        if self.expected_project_id:
            proj = self.headers.get("X-Project-Id", "").strip()
            if proj != self.expected_project_id:
                self._send_json({"ok": False, "error": {"code": "FORBIDDEN", "message": "Missing or mismatched X-Project-Id header."}}, 403)
                return False

        if self.expected_namespace:
            ns = self.headers.get("X-Namespace", "").strip()
            if ns != self.expected_namespace:
                self._send_json({"ok": False, "error": {"code": "FORBIDDEN", "message": "Missing or mismatched X-Namespace header."}}, 403)
                return False

        return True

    def _read_json_body(self) -> dict | None:
        cl = int(self.headers.get("Content-Length", 0))
        if cl == 0:
            return {}
        raw = self.rfile.read(cl).decode("utf-8")
        return json.loads(raw)

    def do_GET(self):
        url_parsed = urllib.parse.urlparse(self.path)
        path = url_parsed.path

        if path in ("/health", "/health/"):
            self._send_json({
                "ok": True,
                "service": "orca-cloudflare-control-plane",
                "version": "1.0.2",
                "database_available": True,
                "type": "http",
            })
            return

        if not self._check_auth():
            return

        query_params = urllib.parse.parse_qs(url_parsed.query)

        # CW-3: GET /api/users/:owner/jobs/ref?ref=...
        m = re.match(r"^/api/users/([^/]+)/jobs/ref$", path)
        if m:
            owner = m.group(1).lower()
            ref_param = query_params.get("ref", [""])[0]
            if not ref_param:
                self._send_json({"ok": False, "error": {"code": "BAD_REQUEST", "message": "Missing ref parameter."}}, 400)
                return
            cur = self.db.cursor()
            cur.execute("SELECT * FROM jobs WHERE owner = ? AND kaggle_job_ref = ?", (owner, ref_param))
            row = cur.fetchone()
            if not row:
                self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Job record not found for reference."}}, 404)
                return
            job = self._row_to_job(row)
            self._send_json({"ok": True, "job": job})
            return

        # CW-3: GET /api/users/:owner/jobs/id/:job_id
        m = re.match(r"^/api/users/([^/]+)/jobs/id/([^/]+)$", path)
        if m:
            owner, job_id = m.group(1).lower(), m.group(2)
            cur = self.db.cursor()
            cur.execute("SELECT * FROM jobs WHERE owner = ? AND internal_job_id = ?", (owner, job_id))
            row = cur.fetchone()
            if not row:
                self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Job record not found."}}, 404)
                return
            job = self._row_to_job(row)
            self._send_json({"ok": True, "job": job})
            return

        # GET /api/users/:owner/jobs/:job_id (fallback)
        m = re.match(r"^/api/users/([^/]+)/jobs/([^/]+)$", path)
        if m:
            owner, id_or_ref = m.group(1).lower(), m.group(2)
            cur = self.db.cursor()
            cur.execute(
                "SELECT * FROM jobs WHERE owner = ? AND (internal_job_id = ? OR kaggle_job_ref = ?)",
                (owner, id_or_ref, id_or_ref),
            )
            row = cur.fetchone()
            if not row:
                self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Job not found"}}, 404)
                return
            job = self._row_to_job(row)
            self._send_json({"ok": True, "job": job})
            return

        # GET /api/users/:owner/jobs
        m = re.match(r"^/api/users/([^/]+)/jobs$", path)
        if m:
            owner = m.group(1).lower()
            cur = self.db.cursor()
            cur.execute("SELECT * FROM jobs WHERE owner = ? ORDER BY created_at DESC", (owner,))
            rows = cur.fetchall()
            jobs = [self._row_to_job(r) for r in rows]
            self._send_json({"ok": True, "jobs": jobs})
            return

        # GET /api/users/:owner/workflows/:workflow_id
        m = re.match(r"^/api/users/([^/]+)/workflows/([^/]+)$", path)
        if m:
            owner, wf_id = m.group(1).lower(), m.group(2)
            cur = self.db.cursor()
            cur.execute("SELECT * FROM workflows WHERE owner = ? AND workflow_id = ?", (owner, wf_id))
            row = cur.fetchone()
            if not row:
                self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Workflow not found"}}, 404)
                return
            wf = self._row_to_wf(row)
            self._send_json({"ok": True, "workflow": wf})
            return

        # GET /api/users/:owner/workflows
        m = re.match(r"^/api/users/([^/]+)/workflows$", path)
        if m:
            owner = m.group(1).lower()
            cur = self.db.cursor()
            cur.execute("SELECT * FROM workflows WHERE owner = ? ORDER BY created_at DESC", (owner,))
            rows = cur.fetchall()
            wfs = [self._row_to_wf(r) for r in rows]
            self._send_json({"ok": True, "workflows": wfs})
            return

        # GET /api/users/:owner/credentials/kaggle
        m = re.match(r"^/api/users/([^/]+)/credentials/kaggle$", path)
        if m:
            owner = m.group(1).lower()
            cur = self.db.cursor()
            include_vault = query_params.get("vault", [""])[0] == "1"
            if include_vault:
                cur.execute("SELECT * FROM credential_vault WHERE owner = ?", (owner,))
                row = cur.fetchone()
                if not row:
                    self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "No stored credentials found for owner."}}, 404)
                    return
                self._send_json({
                    "ok": True,
                    "credential": {
                        "owner": row[0],
                        "kaggle_username": row[1],
                        "ciphertext": row[2],
                        "nonce": row[3],
                        "tag": row[4],
                        "encryption_version": row[5],
                        "status": row[6],
                        "created_at": row[7],
                        "updated_at": row[8],
                        "last_verified_at": row[9],
                    }
                })
                return
            else:
                cur.execute("SELECT owner, kaggle_username, encryption_version, status, created_at, updated_at, last_verified_at FROM credential_vault WHERE owner = ?", (owner,))
                row = cur.fetchone()
                if not row:
                    self._send_json({"ok": True, "credential": {"exists": False, "owner": owner}})
                    return
                self._send_json({
                    "ok": True,
                    "credential": {
                        "exists": True,
                        "owner": row[0],
                        "kaggle_username": row[1],
                        "encryption_version": row[2],
                        "status": row[3],
                        "created_at": row[4],
                        "updated_at": row[5],
                        "last_verified_at": row[6],
                    }
                })
                return

        self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Route not found"}}, 404)

    def do_PUT(self):
        if not self._check_auth():
            return

        url_parsed = urllib.parse.urlparse(self.path)
        path = url_parsed.path
        body = self._read_json_body()

        # PUT /api/users/:owner/jobs/:job_id
        m = re.match(r"^/api/users/([^/]+)/jobs/([^/]+)$", path)
        if m:
            try:
                owner, job_id = m.group(1).lower(), m.group(2)
                cur = self.db.cursor()
                cur.execute("INSERT OR IGNORE INTO users (owner, created_at, updated_at) VALUES (?, 0, 0)", (owner,))

                # CW-5: Check existing version
                cur.execute("SELECT version FROM jobs WHERE owner = ? AND internal_job_id = ?", (owner, job_id))
                existing_ver_row = cur.fetchone()
                next_ver = 1
                if existing_ver_row:
                    existing_ver = existing_ver_row[0] or 1
                    incoming_ver = body.get("version", existing_ver)
                    if incoming_ver < existing_ver:
                        self._send_json({
                            "ok": False,
                            "error": {
                                "code": "CONFLICT",
                                "message": f"Optimistic concurrency conflict: current is {existing_ver}, payload is {incoming_ver}"
                            }
                        }, 409)
                        return
                    next_ver = existing_ver + 1

                cur.execute(
                    """INSERT INTO jobs (
                        internal_job_id, owner, kaggle_job_ref, kaggle_url, title,
                        input_filename, job_kind, created_at, updated_at, last_reconciliation_at,
                        last_seen_remote_state, local_state, workflow_id, parent_job_id,
                        step_index, step_count, step_name, epoch, checkpoint_id,
                        checkpoint_version, resume_required, resume_reason, result_state,
                        storage_durability, cf_sync_status, result_artifact_id,
                        result_storage_reference, result_manifest_id, result_sha256,
                        result_size_bytes, result_archived_at, result_downloaded_at,
                        result_provenance_json, last_error_code, remote_deleted,
                        chain_slugs_json, schema_version, version, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(internal_job_id) DO UPDATE SET
                        owner = excluded.owner,
                        kaggle_job_ref = excluded.kaggle_job_ref,
                        kaggle_url = excluded.kaggle_url,
                        title = excluded.title,
                        updated_at = excluded.updated_at,
                        local_state = excluded.local_state,
                        last_seen_remote_state = excluded.last_seen_remote_state,
                        workflow_id = excluded.workflow_id,
                        result_state = excluded.result_state,
                        storage_durability = excluded.storage_durability,
                        cf_sync_status = 'SYNCED',
                        result_sha256 = excluded.result_sha256,
                        result_size_bytes = excluded.result_size_bytes,
                        result_storage_reference = excluded.result_storage_reference,
                        version = excluded.version""",
                    (
                        body.get("internal_job_id", job_id),
                        owner,
                        body.get("kaggle_job_ref", job_id),
                        body.get("kaggle_url", ""),
                        body.get("title", ""),
                        body.get("input_filename", ""),
                        body.get("job_kind", "unknown"),
                        body.get("created_at", 0.0),
                        body.get("updated_at", 0.0),
                        body.get("last_reconciliation_at", 0.0),
                        body.get("last_seen_remote_state", "UNKNOWN"),
                        body.get("local_state", "CREATED"),
                        body.get("workflow_id"),
                        body.get("parent_job_id"),
                        body.get("step_index", 0),
                        body.get("step_count", 1),
                        body.get("step_name", "CALC"),
                        body.get("epoch", 0),
                        body.get("checkpoint_id"),
                        body.get("checkpoint_version"),
                        1 if body.get("resume_required") else 0,
                        body.get("resume_reason"),
                        body.get("result_state", "REMOTE_ONLY"),
                        body.get("storage_durability", "none"),
                        "SYNCED",
                        body.get("result_artifact_id"),
                        body.get("result_storage_reference"),
                        body.get("result_manifest_id"),
                        body.get("result_sha256"),
                        body.get("result_size_bytes"),
                        body.get("result_archived_at"),
                        body.get("result_downloaded_at"),
                        json.dumps(body.get("result_provenance", {})),
                        body.get("last_error_code"),
                        1 if body.get("remote_deleted") else 0,
                        json.dumps(body.get("chain_slugs", [])),
                        body.get("schema_version", 1),
                        next_ver,
                        json.dumps(body.get("metadata", {})),
                    ),
                )
                self.db.commit()

                cur.execute("SELECT * FROM jobs WHERE owner = ? AND internal_job_id = ?", (owner, job_id))
                saved_job = self._row_to_job(cur.fetchone())
                self._send_json({"ok": True, "job": saved_job}, 200)
                return
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send_json({"ok": False, "error": {"code": "INTERNAL", "message": str(e)}}, 500)
                return

        # PUT /api/users/:owner/workflows/:workflow_id
        m = re.match(r"^/api/users/([^/]+)/workflows/([^/]+)$", path)
        if m:
            owner, wf_id = m.group(1).lower(), m.group(2)
            cur = self.db.cursor()
            cur.execute("INSERT OR IGNORE INTO users (owner, created_at, updated_at) VALUES (?, 0, 0)", (owner,))

            # Check existing version
            cur.execute("SELECT version FROM workflows WHERE owner = ? AND workflow_id = ?", (owner, wf_id))
            existing_wf_ver = cur.fetchone()
            next_wf_ver = 1
            if existing_wf_ver:
                existing_ver = existing_wf_ver[0] or 1
                incoming_ver = body.get("version", existing_ver)
                if incoming_ver < existing_ver:
                    self._send_json({
                        "ok": False,
                        "error": {
                            "code": "CONFLICT",
                            "message": f"Optimistic concurrency conflict for workflow: current is {existing_ver}, payload is {incoming_ver}"
                        }
                    }, 409)
                    return
                next_wf_ver = existing_ver + 1

            cur.execute(
                """INSERT INTO workflows (
                    workflow_id, owner, title, status, current_step_index,
                    created_at, updated_at, schema_version, version, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    owner = excluded.owner,
                    title = excluded.title,
                    status = excluded.status,
                    current_step_index = excluded.current_step_index,
                    updated_at = excluded.updated_at,
                    version = excluded.version""",
                (
                    body.get("workflow_id", wf_id),
                    owner,
                    body.get("title", "Untitled"),
                    body.get("status", "CREATED"),
                    body.get("current_step_index", 0),
                    body.get("created_at", 0.0),
                    body.get("updated_at", 0.0),
                    body.get("schema_version", 1),
                    next_wf_ver,
                    json.dumps(body.get("metadata", {})),
                ),
            )
            # Steps
            for s in body.get("steps", []):
                cur.execute(
                    """INSERT INTO workflow_steps (
                        workflow_id, step_index, step_name, job_id, input_template,
                        status, prerequisites_json, required_artifacts_json, result_data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workflow_id, step_index) DO UPDATE SET
                        step_name = excluded.step_name,
                        job_id = excluded.job_id,
                        status = excluded.status""",
                    (
                        wf_id,
                        s.get("step_index", 0),
                        s.get("step_name", "CALC"),
                        s.get("job_id"),
                        s.get("input_template", ""),
                        s.get("status", "PENDING"),
                        json.dumps(s.get("prerequisites", [])),
                        json.dumps(s.get("required_artifacts", [])),
                        json.dumps(s.get("result_data", {})),
                    ),
                )
            self.db.commit()

            cur.execute("SELECT * FROM workflows WHERE owner = ? AND workflow_id = ?", (owner, wf_id))
            saved_wf = self._row_to_wf(cur.fetchone())
            self._send_json({"ok": True, "workflow": saved_wf}, 200)
            return

        # PUT /api/users/:owner/credentials/kaggle
        m = re.match(r"^/api/users/([^/]+)/credentials/kaggle$", path)
        if m:
            owner = m.group(1).lower()
            cur = self.db.cursor()
            cur.execute("INSERT OR IGNORE INTO users (owner, created_at, updated_at) VALUES (?, 0, 0)", (owner,))
            cur.execute(
                """INSERT INTO credential_vault (
                    owner, kaggle_username, ciphertext, nonce, tag,
                    encryption_version, status, created_at, updated_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner) DO UPDATE SET
                    kaggle_username = excluded.kaggle_username,
                    ciphertext = excluded.ciphertext,
                    nonce = excluded.nonce,
                    tag = excluded.tag,
                    encryption_version = excluded.encryption_version,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    last_verified_at = COALESCE(excluded.last_verified_at, credential_vault.last_verified_at)""",
                (
                    owner,
                    body.get("kaggle_username", "").lower(),
                    body.get("ciphertext", ""),
                    body.get("nonce", ""),
                    body.get("tag", ""),
                    body.get("encryption_version", 1),
                    body.get("status", "ACTIVE"),
                    body.get("created_at", time.time()),
                    body.get("updated_at", time.time()),
                    body.get("last_verified_at"),
                ),
            )
            self.db.commit()
            self._send_json({"ok": True, "owner": owner, "status": body.get("status", "ACTIVE")}, 200)
            return

        self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Route not found"}}, 404)

    def do_POST(self):
        if not self._check_auth():
            return

        path = self.path
        body = self._read_json_body()

        # POST /api/users/:owner/checkpoints
        m = re.match(r"^/api/users/([^/]+)/checkpoints$", path)
        if m:
            owner = m.group(1).lower()
            cur = self.db.cursor()
            cur.execute("INSERT OR IGNORE INTO users (owner, created_at, updated_at) VALUES (?, 0, 0)", (owner,))
            cur.execute(
                """INSERT INTO checkpoints (
                    checkpoint_id, owner, job_id, epoch, bundle_digest,
                    status, orca_phase, created_at, verified_at,
                    source_kernel_slug, file_manifest_json, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    status = excluded.status,
                    verified_at = excluded.verified_at""",
                (
                    body["checkpoint_id"],
                    owner,
                    body["job_id"],
                    body.get("epoch", 0),
                    body.get("bundle_digest", ""),
                    body.get("status", "STAGED"),
                    body.get("orca_phase", "unknown"),
                    body.get("created_at", 0.0),
                    body.get("verified_at"),
                    body.get("source_kernel_slug", ""),
                    json.dumps(body.get("file_manifest", [])),
                    body.get("schema_version", 1),
                ),
            )
            self.db.commit()
            self._send_json({"ok": True, "checkpoint": body}, 201)
            return

        self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Route not found"}}, 404)

    def do_DELETE(self):
        if not self._check_auth():
            return

        url_parsed = urllib.parse.urlparse(self.path)
        path = url_parsed.path
        query_params = urllib.parse.parse_qs(url_parsed.query)

        # DELETE /api/users/:owner/credentials/kaggle
        m = re.match(r"^/api/users/([^/]+)/credentials/kaggle$", path)
        if m:
            owner = m.group(1).lower()
            cur = self.db.cursor()
            cur.execute("DELETE FROM credential_vault WHERE owner = ?", (owner,))
            deleted = cur.rowcount > 0
            self.db.commit()
            self._send_json({"ok": True, "deleted": deleted}, 200)
            return

        # CW-3: DELETE /api/users/:owner/jobs/ref?ref=...
        m = re.match(r"^/api/users/([^/]+)/jobs/ref$", path)
        if m:
            owner = m.group(1).lower()
            ref_param = query_params.get("ref", [""])[0]
            cur = self.db.cursor()
            cur.execute("DELETE FROM jobs WHERE owner = ? AND kaggle_job_ref = ?", (owner, ref_param))
            self.db.commit()
            self._send_json({"ok": True}, 200)
            return

        # CW-3: DELETE /api/users/:owner/jobs/id/:job_id
        m = re.match(r"^/api/users/([^/]+)/jobs/id/([^/]+)$", path)
        if m:
            owner, job_id = m.group(1).lower(), m.group(2)
            cur = self.db.cursor()
            cur.execute("DELETE FROM jobs WHERE owner = ? AND internal_job_id = ?", (owner, job_id))
            self.db.commit()
            self._send_json({"ok": True}, 200)
            return

        # DELETE /api/users/:owner/jobs/:job_id (fallback)
        m = re.match(r"^/api/users/([^/]+)/jobs/([^/]+)$", path)
        if m:
            owner, id_or_ref = m.group(1).lower(), m.group(2)
            cur = self.db.cursor()
            cur.execute("DELETE FROM jobs WHERE owner = ? AND (internal_job_id = ? OR kaggle_job_ref = ?)", (owner, id_or_ref, id_or_ref))
            self.db.commit()
            self._send_json({"ok": True}, 200)
            return

        self._send_json({"ok": False, "error": {"code": "NOT_FOUND", "message": "Route not found"}}, 404)

    def _row_to_job(self, row: tuple) -> dict:
        return {
            "internal_job_id": row[0],
            "kaggle_username": row[1],
            "kaggle_job_ref": row[2],
            "kaggle_url": row[3],
            "title": row[4],
            "input_filename": row[5],
            "job_kind": row[6],
            "created_at": row[7],
            "updated_at": row[8],
            "last_reconciliation_at": row[9],
            "last_seen_remote_state": row[10],
            "local_state": row[11],
            "workflow_id": row[12],
            "parent_job_id": row[13],
            "step_index": row[14],
            "step_count": row[15],
            "step_name": row[16],
            "epoch": row[17],
            "checkpoint_id": row[18],
            "checkpoint_version": row[19],
            "resume_required": bool(row[20]),
            "resume_reason": row[21],
            "result_state": row[22],
            "storage_durability": row[23],
            "cf_sync_status": row[24],
            "result_artifact_id": row[25],
            "result_storage_reference": row[26],
            "result_manifest_id": row[27],
            "result_sha256": row[28],
            "result_size_bytes": row[29],
            "result_archived_at": row[30],
            "result_downloaded_at": row[31],
            "result_provenance": json.loads(row[32] or "{}"),
            "last_error_code": row[33],
            "remote_deleted": bool(row[34]),
            "chain_slugs": json.loads(row[35] or "[]"),
            "schema_version": row[36],
            "version": row[37],
            "metadata": json.loads(row[38] or "{}"),
        }

    def _row_to_wf(self, row: tuple) -> dict:
        cur = self.db.cursor()
        cur.execute("SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY step_index ASC", (row[0],))
        step_rows = cur.fetchall()
        steps = []
        for s in step_rows:
            steps.append({
                "step_index": s[1],
                "step_name": s[2],
                "job_id": s[3],
                "input_template": s[4],
                "status": s[5],
                "prerequisites": json.loads(s[6] or "[]"),
                "required_artifacts": json.loads(s[7] or "[]"),
                "result_data": json.loads(s[8] or "{}"),
            })

        return {
            "workflow_id": row[0],
            "kaggle_username": row[1],
            "title": row[2],
            "status": row[3],
            "current_step_index": row[4],
            "created_at": row[5],
            "updated_at": row[6],
            "schema_version": row[7],
            "version": row[8],
            "metadata": json.loads(row[9] or "{}"),
            "steps": steps,
        }


@pytest.fixture(scope="module")
def d1_http_server():
    """Initializes in-memory SQLite with migrations and runs HTTP server in background thread."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    with open("cloudflare-control-plane/migrations/0001_initial.sql", "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    if os.path.exists("cloudflare-control-plane/migrations/0002_credential_vault.sql"):
        with open("cloudflare-control-plane/migrations/0002_credential_vault.sql", "r", encoding="utf-8") as f:
            conn.executescript(f.read())
    conn.commit()

    D1SimulatedServer.db = conn
    D1SimulatedServer.expected_token = AUTH_TOKEN
    D1SimulatedServer.expected_project_id = PROJECT_ID
    D1SimulatedServer.expected_namespace = NAMESPACE

    server = HTTPServer(("127.0.0.1", 0), D1SimulatedServer)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    conn.close()


@pytest.fixture
def cf_http_client(d1_http_server):
    cfg = CloudflareConfig(
        controller_url=d1_http_server,
        api_token=AUTH_TOKEN,
        project_id=PROJECT_ID,
        namespace=NAMESPACE,
        timeout_seconds=2.0,
        max_retries=1,
    )
    return CloudflareHttpClient(config=cfg, fallback_backend=InMemoryCloudflareBackend())


def test_health_check_endpoint(cf_http_client):
    health = cf_http_client.health()
    assert health["ok"] is True
    assert health["type"] == "http"
    assert health["remote"]["service"] == "orca-cloudflare-control-plane"
    assert health["remote"]["database_available"] is True


def test_cw2_fail_closed_and_auth_checks(d1_http_server):
    # 1. Missing Token on Server -> 503 Fail Closed
    orig_token = D1SimulatedServer.expected_token
    try:
        D1SimulatedServer.expected_token = None
        req = urllib.request.Request(
            f"{d1_http_server}/api/users/alice/jobs",
            headers={"Authorization": f"Bearer {AUTH_TOKEN}", "X-Project-Id": PROJECT_ID, "X-Namespace": NAMESPACE},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 503
    finally:
        D1SimulatedServer.expected_token = orig_token

    # 2. Missing Auth Header -> 401
    req = urllib.request.Request(
        f"{d1_http_server}/api/users/alice/jobs",
        headers={"X-Project-Id": PROJECT_ID, "X-Namespace": NAMESPACE},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401

    # 3. Wrong Token -> 401
    req = urllib.request.Request(
        f"{d1_http_server}/api/users/alice/jobs",
        headers={"Authorization": "Bearer wrong_token", "X-Project-Id": PROJECT_ID, "X-Namespace": NAMESPACE},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401


def test_cw6_namespace_and_project_validation(d1_http_server):
    # 1. Wrong Project ID -> 403
    req = urllib.request.Request(
        f"{d1_http_server}/api/users/alice/jobs",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}", "X-Project-Id": "wrong-proj", "X-Namespace": NAMESPACE},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 403

    # 2. Wrong Namespace -> 403
    req = urllib.request.Request(
        f"{d1_http_server}/api/users/alice/jobs",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}", "X-Project-Id": PROJECT_ID, "X-Namespace": "wrong-namespace"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 403


def test_cw3_kaggle_slug_with_slash_lookup(cf_http_client):
    # Test job reference with real Kaggle slash: "alice/chem-tools-benzene-opt-42"
    rec = CloudflareJobRecord(
        internal_job_id="uuid-benzene-42",
        kaggle_username="alice",
        kaggle_job_ref="alice/chem-tools-benzene-opt-42",
        title="Benzene Opt with Slash",
        version=1,
    )
    saved = cf_http_client.put_job(rec)
    assert saved.cf_sync_status == "SYNCED"

    # Fresh client without fallback memory to prove lookup hits the real server
    fresh_client = CloudflareHttpClient(config=cf_http_client.config, fallback_backend=InMemoryCloudflareBackend())

    # Get by slashed Kaggle ref
    fetched = fresh_client.get_job("alice/chem-tools-benzene-opt-42", "alice")
    assert fetched is not None
    assert fetched.internal_job_id == "uuid-benzene-42"
    assert fetched.kaggle_job_ref == "alice/chem-tools-benzene-opt-42"
    assert fetched.cf_sync_status == "SYNCED"

    # Delete by slashed Kaggle ref
    del_ok = fresh_client.delete_job("alice/chem-tools-benzene-opt-42", "alice")
    assert del_ok is True

    # Confirm it's gone
    after_del = fresh_client.get_job("alice/chem-tools-benzene-opt-42", "alice")
    assert after_del is None


def test_cw5_optimistic_concurrency(cf_http_client):
    rec = CloudflareJobRecord(
        internal_job_id="uuid-concurrency-test",
        kaggle_username="bob",
        kaggle_job_ref="chem-tools-concurrency",
        title="Concurrency Calc",
        version=1,
    )
    saved1 = cf_http_client.put_job(rec)
    assert saved1.cf_sync_status == "SYNCED"
    assert saved1.version == 1

    # Client A updates with version 1 -> gets version 2
    rec.title = "Updated Title by A"
    rec.version = 1
    saved_a = cf_http_client.put_job(rec)
    assert saved_a.cf_sync_status == "SYNCED"
    assert saved_a.version == 2

    # Client B attempts to update with stale version 1 -> 409 Conflict
    stale_rec = CloudflareJobRecord(
        internal_job_id="uuid-concurrency-test",
        kaggle_username="bob",
        kaggle_job_ref="chem-tools-concurrency",
        title="Stale Update by B",
        version=1,
    )
    saved_b = cf_http_client.put_job(stale_rec)
    assert saved_b.cf_sync_status == "CONFLICT_STALE"


def test_job_crud_lifecycle_and_owner_isolation(cf_http_client):
    # 1. Put Job for Alice
    rec_alice = CloudflareJobRecord(
        internal_job_id="job-alice-100",
        kaggle_username="alice",
        kaggle_job_ref="chem-tools-alice-100",
        title="Ethanol B3LYP",
        result_state="ARCHIVED_PERSISTENT",
        storage_durability="persistent_external",
        result_sha256="abc123sha256",
        result_size_bytes=1048576,
    )
    saved_alice = cf_http_client.put_job(rec_alice)
    assert saved_alice.cf_sync_status == "SYNCED"

    # 2. Put Job for Bob
    rec_bob = CloudflareJobRecord(
        internal_job_id="job-bob-200",
        kaggle_username="bob",
        kaggle_job_ref="chem-tools-bob-200",
        title="Benzene HF",
    )
    saved_bob = cf_http_client.put_job(rec_bob)
    assert saved_bob.cf_sync_status == "SYNCED"

    # 3. Get Alice's job by ID and by Kaggle Ref
    fetched_by_id = cf_http_client.get_job("job-alice-100", "alice")
    assert fetched_by_id is not None
    assert fetched_by_id.internal_job_id == "job-alice-100"
    assert fetched_by_id.result_state == "ARCHIVED_PERSISTENT"
    assert fetched_by_id.result_sha256 == "abc123sha256"

    fetched_by_ref = cf_http_client.get_job("chem-tools-alice-100", "alice")
    assert fetched_by_ref is not None
    assert fetched_by_ref.internal_job_id == "job-alice-100"

    # 4. Enforce Owner Isolation: Alice cannot fetch Bob's job
    fresh_alice_client = CloudflareHttpClient(config=cf_http_client.config, fallback_backend=InMemoryCloudflareBackend())
    bob_job_via_alice = fresh_alice_client.get_job("job-bob-200", "alice")
    assert bob_job_via_alice is None

    # 5. List Alice's jobs
    alice_jobs = cf_http_client.list_user_jobs("alice")
    assert len(alice_jobs) >= 1
    assert any(j.internal_job_id == "job-alice-100" for j in alice_jobs)

    # 6. Delete Alice's job
    del_ok = cf_http_client.delete_job("job-alice-100", "alice")
    assert del_ok is True


def test_workflow_crud_and_steps(cf_http_client):
    step0 = WorkflowStep(step_index=0, step_name="OPT", status="COMPLETED", job_id="chem-tools-naproxen-0")
    step1 = WorkflowStep(step_index=1, step_name="FREQ", status="PENDING", prerequisites=[0])

    wf = CloudflareWorkflowRecord(
        workflow_id="wf-naproxen-77",
        kaggle_username="chemist",
        title="Naproxen Calculation",
        steps=[step0, step1],
        version=1,
    )

    # 1. Put Workflow
    saved_wf = cf_http_client.put_workflow(wf)
    assert saved_wf.workflow_id == "wf-naproxen-77"

    # 2. Get Workflow
    fetched_wf = cf_http_client.get_workflow("wf-naproxen-77", "chemist")
    assert fetched_wf is not None
    assert len(fetched_wf.steps) == 2
    assert fetched_wf.steps[0].status == "COMPLETED"
    assert fetched_wf.steps[1].prerequisites == [0]

    # 3. List Workflows
    wfs = cf_http_client.list_user_workflows("chemist")
    assert len(wfs) == 1
    assert wfs[0].workflow_id == "wf-naproxen-77"


def test_checkpoint_recording(cf_http_client):
    ckpt = CloudflareCheckpointRecord(
        checkpoint_id="chk_stage_99",
        job_id="chem-tools-h2o-calc",
        epoch=2,
        bundle_digest="sha256_bundle_digest_123",
        status="VERIFIED",
        orca_phase="SCF_CONVERGED",
    )
    ok = cf_http_client.record_checkpoint(ckpt, "chemist")
    assert ok is True


def test_security_assert_no_secrets_enforcement(cf_http_client):
    # Attempting to pass Kaggle API key must raise CloudflareSecurityViolation before network call
    rec = CloudflareJobRecord(
        internal_job_id="sec-fail",
        kaggle_username="alice",
        kaggle_job_ref="chem-tools-sec",
        metadata={"kaggle_key": "raw_kaggle_secret_123"},
    )
    with pytest.raises(CloudflareSecurityViolation):
        cf_http_client.put_job(rec)


def test_cw_credential_vault_put_and_get(cf_http_client):
    """Verifies PUT and GET for credential vault over HTTP transport."""
    vault_rec = CloudflareCredentialVaultRecord(
        owner="alice_chemist",
        kaggle_username="alice_kaggle",
        ciphertext="abcd1234ef5678",
        nonce="0102030405060708090a0b0c",
        tag="11223344556677889900aabbccddeeff",
        encryption_version=1,
        status="ACTIVE",
    )

    # 1. PUT credential vault
    ok = cf_http_client.put_credential_vault(vault_rec)
    assert ok is True

    # 2. GET public metadata (no ciphertext)
    meta = cf_http_client.get_credential_metadata("alice_chemist")
    assert meta.exists is True
    assert meta.owner == "alice_chemist"
    assert meta.kaggle_username == "alice_kaggle"
    assert meta.status == "ACTIVE"

    # 3. GET full vault ciphertext record
    fetched = cf_http_client.get_credential_vault("alice_chemist")
    assert fetched is not None
    assert fetched.owner == "alice_chemist"
    assert fetched.kaggle_username == "alice_kaggle"
    assert fetched.ciphertext == "abcd1234ef5678"
    assert fetched.nonce == "0102030405060708090a0b0c"
    assert fetched.tag == "11223344556677889900aabbccddeeff"


def test_cw_credential_vault_delete(cf_http_client):
    """Verifies DELETE credential vault removes stored ciphertext and updates metadata."""
    vault_rec = CloudflareCredentialVaultRecord(
        owner="bob_chemist",
        kaggle_username="bob_kaggle",
        ciphertext="feedbeef998877",
        nonce="0a0b0c0d0e0f101112131415",
        tag="ffeeddccbbaa99887766554433221100",
        encryption_version=1,
        status="ACTIVE",
    )
    cf_http_client.put_credential_vault(vault_rec)

    # Delete
    deleted = cf_http_client.delete_credential_vault("bob_chemist")
    assert deleted is True

    # Check vault is empty
    assert cf_http_client.get_credential_vault("bob_chemist") is None
    meta = cf_http_client.get_credential_metadata("bob_chemist")
    assert meta.exists is False

