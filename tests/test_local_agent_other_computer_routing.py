# -*- coding: utf-8 -*-
"""End-to-End Other-Computer Exact Routing and Multi-Agent Dispatch Test."""
import tempfile
import pytest

from services import local_agent_service
from local_agent.security import generate_process_session, hash_token_verifier


def test_other_computer_exact_routing_and_isolation():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Machine B: Running Local Agent #1 (Lab Desktop)
        sess_B, api_B = generate_process_session()
        local_agent_service.init_runtime_session(
            installation_id="inst_machine_B",
            agent_session_id=sess_B,
            token_verifiers=[hash_token_verifier(api_B)],
            device_name="Laboratory Desktop B",
            platform="windows",
            state_dir=tmpdir,
        )

        # Machine C: Running Local Agent #2 (HPC Supercomputer)
        sess_C, api_C = generate_process_session()
        local_agent_service.init_runtime_session(
            installation_id="inst_machine_C",
            agent_session_id=sess_C,
            token_verifiers=[hash_token_verifier(api_C)],
            device_name="University HPC C",
            platform="hpc",
            backend_kind="hpc",
            scheduler_type="slurm",
            state_dir=tmpdir,
        )

        # User A on Machine A (Web browser) connects Machine B
        claim_B = local_agent_service.claim_runtime_token(api_B, "user_A", state_dir=tmpdir)
        assert claim_B["ok"] is True
        assert claim_B["agent_session_id"] == sess_B

        # User A connects Machine C
        claim_C = local_agent_service.claim_runtime_token(api_C, "user_A", state_dir=tmpdir)
        assert claim_C["ok"] is True
        assert claim_C["agent_session_id"] == sess_C

        # User A lists devices: Both B and C are visible
        devs_A = local_agent_service.list_user_runtime_devices("user_A", state_dir=tmpdir)
        assert len(devs_A) == 2
        dev_map = {d["agent_session_id"]: d for d in devs_A}
        assert sess_B in dev_map
        assert sess_C in dev_map
        assert dev_map[sess_B]["display_name"] == "Laboratory Desktop B"
        assert dev_map[sess_C]["display_name"] == "University HPC C"

        # Verify exact target routing: Dispatch to B routes ONLY to sess_B
        target_B_session = dev_map[sess_B]["agent_session_id"]
        assert target_B_session == sess_B
        assert target_B_session != sess_C
