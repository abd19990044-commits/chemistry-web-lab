# -*- coding: utf-8 -*-
"""Tests for Multi-User Isolation and Cross-User Claim Protection."""
import tempfile
import pytest

from services import local_agent_service
from local_agent.security import generate_process_session, hash_token_verifier


def test_multi_user_runtime_device_isolation():
    with tempfile.TemporaryDirectory() as tmpdir:
        # User A registers Device A1
        sess_a1, api_a1 = generate_process_session()
        local_agent_service.init_runtime_session("inst_A1", sess_a1, [hash_token_verifier(api_a1)], "UserA PC", "windows", state_dir=tmpdir)
        local_agent_service.claim_runtime_token(api_a1, "user_A", state_dir=tmpdir)

        # User A registers Device A2
        sess_a2, api_a2 = generate_process_session()
        local_agent_service.init_runtime_session("inst_A2", sess_a2, [hash_token_verifier(api_a2)], "UserA Mac", "macos", state_dir=tmpdir)
        local_agent_service.claim_runtime_token(api_a2, "user_A", state_dir=tmpdir)

        # User B registers Device B1
        sess_b1, api_b1 = generate_process_session()
        local_agent_service.init_runtime_session("inst_B1", sess_b1, [hash_token_verifier(api_b1)], "UserB HPC", "hpc", state_dir=tmpdir)
        local_agent_service.claim_runtime_token(api_b1, "user_B", state_dir=tmpdir)

        # User A sees only A1 and A2
        devs_a = local_agent_service.list_user_runtime_devices("user_A", state_dir=tmpdir)
        assert len(devs_a) == 2
        session_ids_a = {d["agent_session_id"] for d in devs_a}
        assert sess_a1 in session_ids_a
        assert sess_a2 in session_ids_a
        assert sess_b1 not in session_ids_a

        # User B sees only B1
        devs_b = local_agent_service.list_user_runtime_devices("user_B", state_dir=tmpdir)
        assert len(devs_b) == 1
        assert devs_b[0]["agent_session_id"] == sess_b1


def test_cross_user_claim_blocked():
    with tempfile.TemporaryDirectory() as tmpdir:
        sess_id, api = generate_process_session()
        local_agent_service.init_runtime_session("inst_001", sess_id, [hash_token_verifier(api)], "Lab PC", "linux", state_dir=tmpdir)

        # User A claims first
        res_a = local_agent_service.claim_runtime_token(api, "user_A", state_dir=tmpdir)
        assert res_a["ok"] is True

        # User B attempts to claim the same running session
        res_b = local_agent_service.claim_runtime_token(api, "user_B", state_dir=tmpdir)
        assert res_b["ok"] is False
        assert res_b["error_code"] in ("CROSS_USER_CLAIM_BLOCKED", "TOKEN_ALREADY_CLAIMED")
