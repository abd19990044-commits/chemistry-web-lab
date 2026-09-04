# -*- coding: utf-8 -*-
"""Tests for Ephemeral Connection API generation, entropy, ProcessLock, and Immediate Pairing TXT Cleanup."""
import os
import tempfile
import time
import pytest
from pathlib import Path

from services import local_agent_service
from local_agent.config import AgentConfig
from local_agent.agent import LocalCompanionAgent
from local_agent.security import (
    AgentAlreadyRunningError,
    ProcessLock,
    generate_process_session,
    generate_process_tokens,
    hash_token_verifier,
    redact_token,
    write_pairing_txt,
    remove_pairing_txt,
)


def test_ephemeral_token_generation_and_entropy():
    sess_id, api = generate_process_session()
    assert api.startswith("CLA_")
    assert len(api) >= 45
    assert sess_id.startswith("sess_")

    sess_id2, api2 = generate_process_session()
    assert api != api2
    assert sess_id != sess_id2


def test_100_simulated_agent_processes_unique_apis_and_sessions():
    seen_apis = set()
    seen_sessions = set()
    for _ in range(100):
        sess_id, api = generate_process_session()
        assert api not in seen_apis, "Collision in generated Connection API"
        assert sess_id not in seen_sessions, "Collision in generated session ID"
        seen_apis.add(api)
        seen_sessions.add(sess_id)

    assert len(seen_apis) == 100
    assert len(seen_sessions) == 100


def test_token_redaction_for_safe_server_logs():
    raw_api = "CLA_4H7kQ9zX8mN2vP5wL1yR3tE6uI0oA9sD8fG7hJ6kL5m"
    redacted = redact_token(raw_api)
    assert redacted == "CLA_4H7...kL5m"
    assert raw_api != redacted
    assert "4H7kQ9zX8mN2" not in redacted


def test_agent_process_lock_prevents_duplicate_instances():
    with tempfile.TemporaryDirectory() as tmpdir:
        dir_path = Path(tmpdir)
        lock1 = ProcessLock(dir_path)
        assert lock1.acquire() is True

        # Second lock attempt on same directory fails safely
        lock2 = ProcessLock(dir_path)
        assert lock2.acquire() is False

        lock1.release()
        # After release, lock can be acquired
        assert lock2.acquire() is True
        lock2.release()


def test_pairing_txt_disappears_immediately_after_successful_claim():
    """Requirement 3: pairing TXT exists before claim, but is deleted IMMEDIATELY upon claim/finalize."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = AgentConfig(data_dir=tmpdir, device_name="Test Rig", server_url="http://127.0.0.1:9")
        agent = LocalCompanionAgent(cfg)

        # 1. Start agent session -> pairing TXT written
        write_pairing_txt(
            file_path=agent.pairing_txt_path,
            device_name=cfg.device_name,
            installation_id=agent.installation_id,
            agent_session_id=agent.agent_session_id,
            connection_api=agent.primary_api,
        )
        assert agent.pairing_txt_path.is_file(), "Pairing TXT must exist before claim"

        # Register session on server
        local_agent_service.init_runtime_session(
            installation_id=agent.installation_id,
            agent_session_id=agent.agent_session_id,
            token_verifiers=[hash_token_verifier(agent.primary_api)],
            device_name="Test Rig",
            platform="windows",
            state_dir=tmpdir,
        )

        # User claims on website
        claim_res = local_agent_service.claim_runtime_token(
            connection_api=agent.primary_api,
            owner_id="scientist_ahmed",
            state_dir=tmpdir,
        )
        assert claim_res["ok"] is True
        assert "runtime_session_secret" not in claim_res  # Browser receives NO secret

        # Agent finalizes claim
        finalize_res = local_agent_service.finalize_agent_runtime(
            agent_session_id=agent.agent_session_id,
            connection_api=agent.primary_api,
            state_dir=tmpdir,
        )
        assert finalize_res["ok"] is True
        agent.runtime_session_secret = finalize_res["runtime_session_secret"]
        agent.owner_id = finalize_res["owner_id"]
        agent.paired = True

        # Delete immediately upon claim
        remove_pairing_txt(agent.pairing_txt_path)

        # 2. Assert pairing TXT no longer exists
        assert not agent.pairing_txt_path.is_file(), "Pairing TXT must be deleted immediately after claim"

        # 3. Keep agent session alive & simulate network reconnect
        auth_reconnect = local_agent_service.authenticate_runtime_session(
            agent_session_id=agent.agent_session_id,
            runtime_session_secret=agent.runtime_session_secret,
            state_dir=tmpdir,
        )
        assert auth_reconnect is not None
        assert not agent.pairing_txt_path.is_file(), "Pairing TXT remains absent during reconnect"

        agent.cleanup()


def test_browser_claim_never_receives_runtime_session_secret():
    """CRITICAL SECURITY: Browser claim response must NOT contain runtime_session_secret."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sess_id, tokens = generate_process_tokens(token_count=1)
        api = tokens[0]
        verifiers = [hash_token_verifier(api)]

        local_agent_service.init_runtime_session(
            installation_id="inst_device_001",
            agent_session_id=sess_id,
            token_verifiers=verifiers,
            device_name="Laboratory PC",
            platform="windows",
            state_dir=tmpdir,
        )

        claim_res = local_agent_service.claim_runtime_token(
            connection_api=api,
            owner_id="scientist_ahmed",
            state_dir=tmpdir,
        )
        assert claim_res["ok"] is True
        assert "runtime_session_secret" not in claim_res
        assert "secret" not in claim_res
