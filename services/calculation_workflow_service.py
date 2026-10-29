"""Durable single-molecule calculation chains, shared with the reaction executor."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from services import reaction_workflow_service as workflows


def _safe_options(value):
    if not isinstance(value, dict):
        raise workflows.ReactionValidationError("Each stage needs an ORCA configuration object")
    forbidden = {"key", "password", "kaggle_key", "kaggle_api_token", "api_token", "authorization", "private_key"}

    def check(item):
        if isinstance(item, dict):
            if any(str(key).lower() in forbidden for key in item):
                raise workflows.ReactionValidationError("Credentials must not appear in calculation settings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    if len(json.dumps(value, allow_nan=False)) > 1024 * 1024:
        raise workflows.ReactionValidationError("Stage settings exceed 1 MB")
    return dict(value)


def register_workflow(owner, data, store):
    import chem_core

    stages = data.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= workflows.MAX_STAGES_PER_SPECIES:
        raise workflows.ReactionValidationError("Provide 1 to 20 calculation stages")
    backend = data.get("backend") or "kaggle"
    if backend not in {"kaggle", "server_local"}:
        raise workflows.ReactionValidationError("Unsupported calculation backend")
    request_id = str(data.get("request_id") or "").strip()
    if not request_id or len(request_id) > 160:
        raise workflows.ReactionValidationError("A stable request_id is required")
    name = str(data.get("name") or "calculation")[:160]
    sources = data.get("dataset_sources") or []
    if isinstance(sources, str):
        import re

        sources = [part for part in re.split(r"[\s,]+", sources.strip()) if part]
    if (
        not isinstance(sources, list)
        or len(sources) > 20
        or any(not isinstance(item, str) or len(item) > 300 for item in sources)
    ):
        raise workflows.ReactionValidationError("Invalid ORCA dataset sources")
    if sources:
        import kaggle_runner

        sources = kaggle_runner.clean_dataset_sources(sources)
    link = str(data.get("orca_link") or "").strip()
    if len(link) > 4096:
        raise workflows.ReactionValidationError("ORCA archive link exceeds the supported length")
    if backend == "kaggle" and not sources and not link:
        raise workflows.ReactionValidationError("Select an ORCA dataset or archive link")
    identity = hashlib.sha256((owner + ":" + request_id).encode()).hexdigest()[:32]
    options = [_safe_options(stage.get("config")) for stage in stages if isinstance(stage, dict)]
    if len(options) != len(stages):
        raise workflows.ReactionValidationError("Invalid calculation stages")
    signature = workflows.sha256_text(
        json.dumps(
            {
                "name": name,
                "backend": backend,
                "options": options,
                "inputs": [stage.get("input_text") for stage in stages],
                "sources": sources,
                "link": link,
            },
            sort_keys=True,
            allow_nan=False,
        )
    )
    with store._lock:
        existing = store.get_reaction(owner, "calc_" + identity)
        if existing:
            if existing.get("request_hash") != signature:
                raise workflows.ReactionValidationError("request_id already belongs to different calculation settings")
            return existing
        unfinished = sum(
            1
            for item in store.list_reactions(owner)
            for species in item.get("species", [])
            for stage in species.get("stages", [])
            if stage.get("state") not in {"COMPLETE", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}
        )
        if unfinished + len(stages) > 100:
            raise workflows.ReactionValidationError("The account already has 100 unfinished calculation stages")
        prepared = []
        for index, config in enumerate(options):
            calc_type = str(config.get("calc_type") or stages[index].get("type") or "sp").upper()
            kind = calc_type if calc_type in workflows.STAGE_KINDS else "CUSTOM_ORCA"
            stage = workflows.new_stage(
                kind, str(stages[index].get("name") or f"{name}_stage{index + 1}")[:160], order=index, backend=backend
            )
            config.update({"dataset_sources": sources, "orca_link": link})
            stage["stage_options"] = config
            raw_input = str(stages[index].get("input_text") or "")
            if len(raw_input.encode("utf-8")) > 2 * 1024 * 1024:
                raise workflows.ReactionValidationError("Calculation input exceeds 2 MB")
            stage["input_text"] = chem_core.generate_orca_6_input(config) if options[index].get("coords") else raw_input
            if not stage["input_text"].strip():
                raise workflows.ReactionValidationError("Calculation input is empty")
            stage["input_hash"] = workflows.sha256_text(stage["input_text"])
            stage["attempt_id"] = hashlib.sha256(f"{identity}:{index}".encode()).hexdigest()[:32]
            stage["state"] = "READY" if index == 0 else "BLOCKED_BY_DEPENDENCY"
            stage["parent_stage_id"] = prepared[-1]["stage_id"] if prepared else None
            prepared.append(stage)
        reaction = {
            "reaction_id": "calc_" + identity,
            "kind": "calculation",
            "owner": owner,
            "name": name,
            "equation": name,
            "request_hash": signature,
            "created_at": workflows.utcnow_iso(),
            "state": "READY",
            "background_enabled": True,
            "species": [
                {
                    "species_id": "species_" + identity,
                    "workflow_id": "workflow_" + identity,
                    "display_name": name,
                    "role": "calculation",
                    "nu": 1,
                    "state": "PENDING",
                    "initial_geometry": options[0].get("coords"),
                    "stages": prepared,
                }
            ],
        }
        store.save_reaction(reaction)
    return reaction


def describe_workflow(reaction):
    def millis(value):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000) if value else None
        except (ValueError, TypeError, AttributeError):
            return None

    states = {
        "READY": "queued",
        "QUEUED": "queued",
        "BLOCKED_BY_DEPENDENCY": "waiting_dependency",
        "SUBMITTED": "queued",
        "STARTING": "running",
        "RUNNING": "running",
        "COMPLETE": "complete",
        "COMPLETED_WITH_WARNINGS": "complete",
        "FAILED": "error",
        "CANCELLED": "cancelled",
        "CANCEL_REQUESTED": "running",
        "PAUSED": "waiting_dependency",
    }
    jobs = []
    for species in reaction.get("species", []):
        stages = species.get("stages", [])
        for stage in stages:
            jobs.append(
                {
                    "jobId": stage.get("kaggle_job_id") or stage.get("local_job_id") or stage["stage_id"],
                    "name": stage.get("label"),
                    "backend": "server_workflow",
                    "executionBackend": stage.get("backend"),
                    "serverWorkflowId": reaction["reaction_id"],
                    "serverStageId": stage["stage_id"],
                    "workflowId": species.get("workflow_id"),
                    "workflowStageCount": len(stages),
                    "stage": int(stage.get("order") or 0) + 1,
                    "stageType": stage.get("kind"),
                    "status": states.get(stage.get("state"), "queued"),
                    "kaggleUsername": stage.get("kaggle_slug", "").split("/")[0] or reaction.get("kaggle_username"),
                    "localJobId": stage.get("local_job_id"),
                    "kaggleUrl": stage.get("kaggle_url"),
                    "warning": stage.get("error") or stage.get("waiting_reason"),
                    "remoteActive": stage.get("remote_state") == "RUNNING" if stage.get("remote_state") else None,
                    "submittedAt": millis(
                        stage.get("submitted_at") or stage.get("queued_at") or reaction.get("created_at")
                    ),
                    "startedAt": millis(stage.get("started_at")),
                    "finishedAt": millis(stage.get("finished_at") or reaction.get("finished_at")),
                }
            )
    return {
        "workflow_id": reaction["reaction_id"],
        "state": reaction.get("state"),
        "name": reaction.get("name"),
        "jobs": jobs,
    }
