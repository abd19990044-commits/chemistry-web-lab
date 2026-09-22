# -*- coding: utf-8 -*-
"""Wire protocol envelopes and message schemas for Agent <-> Server communication."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional


class MessageType:
    INIT_SESSION = "INIT_SESSION"
    HELLO = "HELLO"
    HELLO_ACK = "HELLO_ACK"
    HEARTBEAT = "HEARTBEAT"
    HEARTBEAT_ACK = "HEARTBEAT_ACK"
    CAPABILITIES = "CAPABILITIES"
    DISPATCH_JOB = "DISPATCH_JOB"
    JOB_STATUS_UPDATE = "JOB_STATUS_UPDATE"
    JOB_OUTPUT_CHUNK = "JOB_OUTPUT_CHUNK"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"
    CANCEL_JOB = "CANCEL_JOB"
    SESSION_END = "SESSION_END"
    ACK = "ACK"
    ERROR = "ERROR"


def create_envelope(
    msg_type: str,
    payload: Dict[str, Any],
    agent_session_id: str,
    installation_id: str,
    correlation_id: Optional[str] = None,
    seq: int = 0,
) -> Dict[str, Any]:
    return {
        "message_id": str(uuid.uuid4()),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "type": msg_type,
        "agent_session_id": agent_session_id,
        "installation_id": installation_id,
        "sequence_number": seq,
        "timestamp": time.time(),
        "payload": payload,
    }
