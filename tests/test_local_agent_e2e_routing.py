# -*- coding: utf-8 -*-
"""Comprehensive End-to-End Tests for Process Restart, In-Memory Reconnect, and Server Restart."""
import tempfile
import pytest

from services import local_agent_service
from local_agent.security import generate_process_session, generate_process_tokens, hash_token_verifier


def test_process_restart_generates_new_api_and_invalidates_old():
    with tempfile.TemporaryDirectory() as tmpdir:
        inst_id = "inst_lab_workstation"

        # --- Process #1 ---
        sess_1, api_1 = generate_process_session()
        local_agent_service.init_runtime_session(inst_id, sess_1, [hash_token_verifier(api_1)], "Lab PC", "windows", state_dir=tmpdir)
        claim_1 = local_agent_service.claim_runtime_token(api_1, "user_ahmed", state_dir=tmpdir)
        assert claim_1["ok"] is True
        assert "runtime_session_secret" not in claim_1

        local_agent_service.end_runtime_session(sess_1, state_dir=tmpdir)

        # --- Process #2 (Restart) ---
        sess_2, api_2 = generate_process_session()
        assert api_1 != api_2
        assert sess_1 != sess_2

        local_agent_service.init_runtime_session(inst_id, sess_2, [hash_token_verifier(api_2)], "Lab PC", "windows", state_dir=tmpdir)

        # Attempt to claim old API_1 -> REJECTED
        claim_old = local_agent_service.claim_runtime_token(api_1, "user_ahmed", state_dir=tmpdir)
        assert claim_old["ok"] is False
        assert claim_old["error_code"] in ("TOKEN_NOT_FOUND", "TOKEN_INVALIDATED", "SESSION_INACTIVE")

        # Claim using new API_2 -> ACCEPTED
        claim_new = local_agent_service.claim_runtime_token(api_2, "user_ahmed", state_dir=tmpdir)
        assert claim_new["ok"] is True
        assert claim_new["agent_session_id"] == sess_2


def test_same_process_in_memory_reconnect_and_server_restart():
    """Agent remains running, WebSocket drops / server restarts -> reconnects via in-memory secret."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sess_id, api = generate_process_session()
        local_agent_service.init_runtime_session("inst_001", sess_id, [hash_token_verifier(api)], "Lab PC", "linux", state_dir=tmpdir)

        claim = local_agent_service.claim_runtime_token(api, "user_ahmed", state_dir=tmpdir)
        assert claim["ok"] is True

        finalize = local_agent_service.finalize_agent_runtime(sess_id, api, state_dir=tmpdir)
        in_memory_secret = finalize["runtime_session_secret"]

        # Reconnect WebSocket using the in-memory runtime session credential
        auth_info = local_agent_service.authenticate_runtime_session(
            agent_session_id=sess_id,
            runtime_session_secret=in_memory_secret,
            state_dir=tmpdir,
        )
        assert auth_info is not None
        assert auth_info["owner_id"] == "user_ahmed"
        assert auth_info["agent_session_id"] == sess_id


def test_multiple_token_count_per_process_and_single_owner_invalidation():
    with tempfile.TemporaryDirectory() as tmpdir:
        sess_id, tokens = generate_process_tokens(token_count=3)
        assert len(tokens) == 3
        verifiers = [hash_token_verifier(t) for t in tokens]

        local_agent_service.init_runtime_session("inst_srv", sess_id, verifiers, "Server", "linux", state_dir=tmpdir)

        res1 = local_agent_service.claim_runtime_token(tokens[0], "user_admin", single_owner_mode=True, state_dir=tmpdir)
        assert res1["ok"] is True

        res2 = local_agent_service.claim_runtime_token(tokens[1], "user_other", state_dir=tmpdir)
        assert res2["ok"] is False
        assert res2["error_code"] in ("TOKEN_INVALIDATED", "CROSS_USER_CLAIM_BLOCKED")
