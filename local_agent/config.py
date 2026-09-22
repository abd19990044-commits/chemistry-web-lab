# -*- coding: utf-8 -*-
"""Configuration management for Chemistry Lab Local Companion Agent."""
from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


def get_default_data_dir() -> Path:
    custom = os.environ.get("CHEMISTRY_LAB_AGENT_DATA_DIR")
    if custom:
        return Path(custom)
    sys_name = platform.system()
    if sys_name == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "ChemistryLabAgent"
    elif sys_name == "Darwin":
        return Path.home() / "Library" / "Application Support" / "ChemistryLabAgent"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        return Path(base) / "chemistry_lab_agent"


@dataclass
class AgentConfig:
    server_url: str = "http://localhost:7860"
    device_name: str = field(default_factory=lambda: f"{platform.node() or 'Local Computer'}")
    backend_kind: str = "local"  # "local" or "hpc"
    scheduler_type: Optional[str] = None  # "slurm", "pbs", "lsf"
    token_count: int = 1  # 1..10 ephemeral tokens per process
    orca_executable: str = ""
    orca_input_dir: str = ""
    orca_output_dir: str = ""
    orca_working_dir: str = ""
    max_concurrent_jobs: int = 1
    heartbeat_interval_seconds: int = 15
    # A finite ceiling prevents a wedged child from occupying the companion
    # forever.  Fourteen days still accommodates genuinely long calculations
    # and can be overridden in the persisted agent configuration.
    max_job_runtime_seconds: int = 14 * 24 * 60 * 60
    reconnect_delay_seconds: int = 3
    max_reconnect_delay_seconds: int = 60
    data_dir: str = field(default_factory=lambda: str(get_default_data_dir()))

    def save(self, file_path: Optional[Path] = None) -> None:
        p = file_path or (Path(self.data_dir) / "agent_config.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, file_path: Optional[Path] = None) -> AgentConfig:
        default_dir = get_default_data_dir()
        p = file_path or (default_dir / "agent_config.json")
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        return cls()
