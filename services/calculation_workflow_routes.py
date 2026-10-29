"""Owner-scoped HTTP boundary for durable calculation chains."""

from __future__ import annotations

import hmac
import os

from flask import jsonify, request, session

from services.auth_service import is_admin_user, require_authenticated_owner
from services.calculation_workflow_service import describe_workflow, register_workflow
from services.reaction_workflow_service import ReactionValidationError


def kaggle_execution_allowed(data):
    required = (
        os.environ.get("KAGGLE_EXECUTION_PASSCODE")
        or os.environ.get("KAGGLE_ACCESS_CODE")
        or os.environ.get("KAGGLE_PASSCODE")
        or ""
    ).strip()
    supplied = str(data.get("kaggle_passcode") or request.headers.get("X-Kaggle-Passcode") or "").strip()
    return not required or hmac.compare_digest(required.encode(), supplied.encode())


def register_calculation_routes(app, store_getter):
    @app.route("/api/v1/calculation-workflows", methods=["GET", "POST"])
    def calculation_workflows():
        request.max_content_length = 10 * 1024 * 1024
        owner = require_authenticated_owner(session=session, request=request)
        store = store_getter()
        if request.method == "GET":
            items = [
                describe_workflow(item) for item in store.list_reactions(owner) if item.get("kind") == "calculation"
            ]
            return jsonify({"ok": True, "workflows": items, "jobs": [job for item in items for job in item["jobs"]]})
        data = request.get_json(silent=True) or {}
        backend = data.get("backend") or "kaggle"
        if backend == "server_local" and not is_admin_user(owner):
            return jsonify({"ok": False, "error": "Server-host ORCA execution requires administrator access"}), 403
        if backend == "kaggle":
            if not kaggle_execution_allowed(data):
                return jsonify({"ok": False, "error": "INVALID_KAGGLE_PASSCODE"}), 403
            from orca_orchestrator.credential_vault import get_vault_manager
            from orca_orchestrator.credentials import parse
            from orca_orchestrator.service import get_service

            manager = get_vault_manager()
            creds = manager.load_credentials(owner)
            if data.get("kaggle_username") and data.get("kaggle_key"):
                try:
                    candidate = parse(str(data["kaggle_username"]), str(data["kaggle_key"]))
                    creds = get_service().ensure_authenticated(candidate)
                    manager.save_credentials(owner, creds)
                except Exception:
                    return jsonify(
                        {"ok": False, "error": "Could not verify and securely save the Kaggle connection"}
                    ), 401
            if not creds:
                return jsonify(
                    {"ok": False, "error": "Sign in to Kaggle before submitting a background calculation"}
                ), 401
        try:
            reaction = register_workflow(owner, data, store)
            if backend == "kaggle" and not reaction.get("kaggle_username"):
                reaction["kaggle_username"] = creds.username
                store.save_reaction(reaction)
        except (ReactionValidationError, ValueError, TypeError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "background": True, **describe_workflow(reaction)}), 202

    @app.route("/api/v1/calculation-workflows/<workflow_id>", methods=["GET"])
    def calculation_workflow(workflow_id):
        owner = require_authenticated_owner(session=session, request=request)
        reaction = store_getter().get_reaction(owner, workflow_id)
        if not reaction or reaction.get("kind") != "calculation":
            return jsonify({"ok": False, "error": "Calculation workflow not found"}), 404
        return jsonify({"ok": True, **describe_workflow(reaction)})

    @app.route("/api/v1/calculation-workflows/<workflow_id>/cancel", methods=["POST"])
    def cancel_calculation_workflow(workflow_id):
        owner = require_authenticated_owner(session=session, request=request)
        store = store_getter()
        with store._lock:
            reaction = store.get_reaction(owner, workflow_id)
            if not reaction or reaction.get("kind") != "calculation":
                return jsonify({"ok": False, "error": "Calculation workflow not found"}), 404
            if reaction.get("state") not in {"COMPLETE", "CANCELLED", "FAILED"}:
                reaction["state"] = "CANCEL_REQUESTED"
                store.save_reaction(reaction)
        return jsonify({"ok": True, **describe_workflow(reaction)}), 202
