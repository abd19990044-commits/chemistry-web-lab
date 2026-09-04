# -*- coding: utf-8 -*-
"""Tests for ORCA Resource Derivation (%pal, %maxcore, MaxDisk, and Strict Bounds)."""
import pytest
from services.local_agent_service import calculate_orca_resource_directives, inject_orca_resources


def test_resource_derivation_single_core():
    res = calculate_orca_resource_directives(cpu_cores=1, ram_gb=8.0, disk_gb=20.0)
    assert res["ok"] is True
    assert res["nprocs"] == 1
    assert res["pal_block"] == ""
    # 8 GB * 1024 * 0.80 = 6553.6 MB -> 6553 MB
    assert res["maxcore_mb"] == 6553
    assert res["maxdisk_mb"] == 20480
    assert res["nprocs"] * res["maxcore_mb"] <= (8.0 * 1024 * 0.80)


def test_resource_derivation_multi_core():
    res = calculate_orca_resource_directives(cpu_cores=4, ram_gb=16.0, disk_gb=50.0)
    assert res["ok"] is True
    assert res["nprocs"] == 4
    assert "%pal" in res["pal_block"]
    assert "nprocs 4" in res["pal_block"]
    # 16 GB * 1024 * 0.80 = 13107.2 MB / 4 = 3276 MB per core
    assert res["maxcore_mb"] == 3276
    assert res["maxdisk_mb"] == 51200
    assert res["nprocs"] * res["maxcore_mb"] <= (16.0 * 1024 * 0.80)


def test_resource_insufficient_ram_for_core_count_rejected():
    """Hard invariant: If requested RAM cannot support 256 MB per core with 80% reserve, reject."""
    # 1 GB RAM for 8 cores: usable = 819.2 MB / 8 = 102 MB < 256 MB -> MUST REJECT
    res = calculate_orca_resource_directives(cpu_cores=8, ram_gb=1.0, disk_gb=20.0)
    assert res["ok"] is False
    assert res["error_code"] == "INSUFFICIENT_RAM_FOR_CORE_COUNT"
    assert res["min_required_ram_gb"] >= 2.5


def test_resource_insufficient_disk_allocation_rejected():
    """Disk allocation below 1.0 GB (1000 MB) is rejected."""
    res = calculate_orca_resource_directives(cpu_cores=1, ram_gb=4.0, disk_gb=0.5)
    assert res["ok"] is False
    assert res["error_code"] == "INSUFFICIENT_DISK_ALLOCATION"


def test_inject_orca_resources_into_input():
    sample_inp = """! B3LYP def2-SVP Opt

* xyz 0 1
O 0.0 0.0 0.0
H 0.0 0.0 1.0
H 0.0 1.0 0.0
*
"""
    res_spec = {"cpu_cores": 4, "ram_gb": 8.0, "disk_gb": 30.0}
    updated = inject_orca_resources(sample_inp, res_spec)
    assert "%maxcore" in updated
    assert "MaxDisk" in updated
    assert "%pal" in updated
    assert "nprocs 4" in updated
    assert "! B3LYP def2-SVP Opt" in updated
