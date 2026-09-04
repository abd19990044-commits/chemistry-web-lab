# Chemistry Lab Local Agent Wire Protocol

## 1. Ephemeral Process Registration (/runtime/init)
- Method: POST /api/v1/local-agent/runtime/init
- Payload: installation_id, agent_session_id, token_verifiers (SHA-256), platform, capabilities.

## 2. Website Claim (/runtime/claim)
- Method: POST /api/v1/local-agent/runtime/claim
- Payload: connection_api (CLA_...), custom_device_name.
- Response: owner_id, agent_session_id, runtime_session_secret (CRS_...).
