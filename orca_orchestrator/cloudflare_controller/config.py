# -*- coding: utf-8 -*-
"""
Configuration for Cloudflare persistent control plane.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CloudflareConfig:
    """Settings for Cloudflare metadata persistence."""

    controller_url: str = os.environ.get("CLOUDFLARE_CONTROLLER_URL", "")
    api_token: str = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    project_id: str = os.environ.get("CLOUDFLARE_PROJECT_ID", "orca-web-lab")
    namespace: str = os.environ.get("CLOUDFLARE_NAMESPACE", "production")
    timeout_seconds: float = float(os.environ.get("CLOUDFLARE_TIMEOUT_SECONDS", "5.0"))
    max_retries: int = int(os.environ.get("CLOUDFLARE_MAX_RETRIES", "3"))
    enabled: bool = os.environ.get("CLOUDFLARE_ENABLED", "1").lower() not in ("0", "false", "no")
    schema_version: int = 1


CLOUDFLARE_CONFIG = CloudflareConfig()
