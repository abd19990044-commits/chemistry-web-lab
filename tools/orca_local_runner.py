#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-line utility for managing and running Local ORCA calculations.

Cross-Platform Local ORCA Manager:
- Windows: Runtime tested & verified
- Linux: Architecture / code path covered hermetically (shell=False, argument array)
- macOS: Architecture / code path covered hermetically (shell=False, argument array)

Reuses services/local_orca_service.py directly. Does NOT duplicate execution logic.
Does NOT provide arbitrary shell passthrough (shell=False strictly enforced).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import sys
from typing import Any, Dict, Optional

# Ensure repository root is on sys.path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.local_orca_service import (
    cancel_local_orca_job,
    detect_orca_candidates,
    execute_local_orca_job,
    get_active_local_process_info,
    get_local_orca_settings,
    save_local_orca_settings,
    validate_local_orca_config,
)


def cmd_validate(args: argparse.Namespace) -> int:
    """Validates Local ORCA configuration."""
    cfg = get_local_orca_settings(args.state_dir)
    if args.orca:
        cfg["orca_executable"] = args.orca
    if args.input_dir:
        cfg["input_directory"] = args.input_dir
    if args.output_dir:
        cfg["output_directory"] = args.output_dir
    if args.work_dir:
        cfg["working_directory"] = args.work_dir

    result = validate_local_orca_config(cfg, args.state_dir)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ready") else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Displays Local ORCA settings and active process status."""
    cfg = get_local_orca_settings(args.state_dir)
    val = validate_local_orca_config(cfg, args.state_dir)
    active = get_active_local_process_info(args.state_dir)

    status_data = {
        "platform": platform.system(),
        "settings": cfg,
        "validation": val,
        "active_process": active,
    }
    print(json.dumps(status_data, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Runs a single ORCA calculation locally with scientific validation."""
    inp_path = pathlib.Path(args.input)
    if not inp_path.is_file():
        print(json.dumps({
            "ok": False,
            "error": f"Input file not found: {args.input}",
            "error_code": "INPUT_FILE_NOT_FOUND"
        }, indent=2), file=sys.stderr)
        return 1

    input_text = inp_path.read_text(encoding="utf-8", errors="replace")
    job_id = args.job_id or inp_path.stem

    cfg = get_local_orca_settings(args.state_dir)
    if args.orca:
        cfg["orca_executable"] = args.orca
    if args.input_dir:
        cfg["input_directory"] = args.input_dir
    if args.output_dir:
        cfg["output_directory"] = args.output_dir
    if args.work_dir:
        cfg["working_directory"] = args.work_dir

    result = execute_local_orca_job(
        job_id=job_id,
        input_text=input_text,
        stage_kind=args.stage_kind,
        settings=cfg,
        state_dir=args.state_dir,
        timeout_seconds=args.timeout,
    )

    print(json.dumps(result, indent=2))
    if result.get("ok"):
        return 0
    elif not result.get("process_ok"):
        return 1
    else:
        return 2  # Process exit 0 but scientific parse failed


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancels a running Local ORCA calculation."""
    success = cancel_local_orca_job(args.job_id, args.attempt_id, args.state_dir)
    print(json.dumps({"job_id": args.job_id, "cancelled": success}, indent=2))
    return 0 if success else 1


def cmd_candidates(args: argparse.Namespace) -> int:
    """Lists potential ORCA installation candidate paths."""
    candidates = detect_orca_candidates()
    print(json.dumps({"candidates": candidates}, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local ORCA Runner and Diagnostics CLI (Chemistry Lab)",
        prog="orca_local_runner",
    )
    parser.add_argument("--state-dir", default=None, help="Path to state storage directory")

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--state-dir", default=None, help="Path to state storage directory")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate
    p_val = subparsers.add_parser("validate", parents=[common_parser], help="Validate Local ORCA configuration")
    p_val.add_argument("--orca", help="Path to ORCA executable")
    p_val.add_argument("--input-dir", help="Canonical input directory")
    p_val.add_argument("--output-dir", help="Canonical output directory")
    p_val.add_argument("--work-dir", help="Optional scratch directory")

    # status
    p_stat = subparsers.add_parser("status", parents=[common_parser], help="Show Local ORCA settings and active process")

    # candidates
    p_cand = subparsers.add_parser("candidates", parents=[common_parser], help="Detect candidate ORCA executable locations")

    # run
    p_run = subparsers.add_parser("run", parents=[common_parser], help="Run a calculation locally")
    p_run.add_argument("--input", required=True, help="Path to .inp file")
    p_run.add_argument("--job-id", help="Safe job identifier (defaults to input file stem)")
    p_run.add_argument("--stage-kind", choices=["OPT", "FREQ", "NUMFREQ", "SP", "OPT_FREQ"], help="Workflow stage kind")
    p_run.add_argument("--timeout", type=int, help="Calculation timeout in seconds")
    p_run.add_argument("--orca", help="Override ORCA executable path")
    p_run.add_argument("--input-dir", help="Override canonical input directory")
    p_run.add_argument("--output-dir", help="Override canonical output directory")
    p_run.add_argument("--work-dir", help="Override optional scratch directory")

    # cancel
    p_canc = subparsers.add_parser("cancel", parents=[common_parser], help="Cancel an active Local ORCA process")
    p_canc.add_argument("--job-id", required=True, help="Job identifier to terminate")
    p_canc.add_argument("--attempt-id", help="Attempt identifier")

    parsed = parser.parse_args(argv)

    if parsed.command == "validate":
        return cmd_validate(parsed)
    elif parsed.command == "status":
        return cmd_status(parsed)
    elif parsed.command == "candidates":
        return cmd_candidates(parsed)
    elif parsed.command == "run":
        return cmd_run(parsed)
    elif parsed.command == "cancel":
        return cmd_cancel(parsed)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
