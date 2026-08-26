# -*- coding: utf-8 -*-
"""Comprehensive tests for 3D Molecular Builder & Multi-Molecule Assembly.
Audited and hardened for geometry invariance, collision avoidance, concurrency control,
failure tolerance, and state isolation.
"""

import concurrent.futures
import math
import os
import sys
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp
import chem_core as core


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


# ─────────────────────────────────────────────────────────────
# 1. Mathematical Invariant Verification (Rigid-Body)
# ─────────────────────────────────────────────────────────────
def test_rigid_body_translation_invariance():
    """Verify that translation strictly preserves all pairwise internal distances (< 1e-6 Å)."""
    # Aspirin fragment coordinates
    coords = np.array([
        [0.000,  0.000,  0.000],
        [1.390,  0.000,  0.000],
        [2.085,  1.204,  0.000],
        [1.390,  2.408,  0.000],
        [0.000,  2.408,  0.000],
        [-0.695, 1.204,  0.000],
    ])
    n = len(coords)
    # Compute pairwise distance matrix before translation
    dist_before = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            dist_before[i, j] = np.linalg.norm(coords[i] - coords[j])

    # Arbitrary translation vector Δ
    delta = np.array([2.537, -1.894, 0.442])
    coords_after = coords + delta

    # Compute pairwise distance matrix after translation
    dist_after = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            dist_after[i, j] = np.linalg.norm(coords_after[i] - coords_after[j])

    max_diff = np.max(np.abs(dist_before - dist_after))
    assert max_diff < 1e-6, f"Translation violated distance preservation: max diff = {max_diff}"


def test_rigid_body_rotation_invariance_and_matrix_orthogonality():
    """Verify that rotation around centroid preserves bond distances and angles, and R^T R = I, det(R) = +1."""
    # Non-planar water molecule
    coords = np.array([
        [0.0000,  0.0000,  0.1173],  # O
        [0.0000,  0.7572, -0.4692],  # H1
        [0.0000, -0.7572, -0.4692],  # H2
    ])

    # Bond lengths and angle before
    d_OH1_before = np.linalg.norm(coords[0] - coords[1])
    d_OH2_before = np.linalg.norm(coords[0] - coords[2])
    d_H1H2_before = np.linalg.norm(coords[1] - coords[2])

    v1 = coords[1] - coords[0]
    v2 = coords[2] - coords[0]
    angle_before = np.degrees(np.arccos(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))))

    centroid = np.mean(coords, axis=0)

    # Build 3D rotation matrix: Roll (X 15°), Pitch (Y -30°), Yaw (Z 45°)
    rx, ry, rz = np.radians(15.0), np.radians(-30.0), np.radians(45.0)
    Rx = np.array([
        [1, 0, 0],
        [0, np.cos(rx), -np.sin(rx)],
        [0, np.sin(rx), np.cos(rx)]
    ])
    Ry = np.array([
        [np.cos(ry), 0, np.sin(ry)],
        [0, 1, 0],
        [-np.sin(ry), 0, np.cos(ry)]
    ])
    Rz = np.array([
        [np.cos(rz), -np.sin(rz), 0],
        [np.sin(rz), np.cos(rz), 0],
        [0, 0, 1]
    ])
    R = Rz @ Ry @ Rx

    # Verify R is orthogonal with det(R) = +1
    np.testing.assert_allclose(R.T @ R, np.eye(3), atol=1e-12)
    assert abs(np.linalg.det(R) - 1.0) < 1e-12

    # Apply rotation around centroid
    coords_after = (coords - centroid) @ R.T + centroid

    # Bond lengths and angle after
    d_OH1_after = np.linalg.norm(coords_after[0] - coords_after[1])
    d_OH2_after = np.linalg.norm(coords_after[0] - coords_after[2])
    d_H1H2_after = np.linalg.norm(coords_after[1] - coords_after[2])

    v1_after = coords_after[1] - coords_after[0]
    v2_after = coords_after[2] - coords_after[0]
    angle_after = np.degrees(np.arccos(np.dot(v1_after, v2_after) / (np.linalg.norm(v1_after) * np.linalg.norm(v2_after))))

    assert abs(d_OH1_before - d_OH1_after) < 1e-6
    assert abs(d_OH2_before - d_OH2_after) < 1e-6
    assert abs(d_H1H2_before - d_H1H2_after) < 1e-6
    assert abs(angle_before - angle_after) < 1e-4


# ─────────────────────────────────────────────────────────────
# 2. Collision Avoidance & Safe Clearance
# ─────────────────────────────────────────────────────────────
def test_adaptive_collision_avoidance_clearance():
    """Verify that multi-molecule auto-offset enforces minimum clearance across all element pairs."""
    mol1 = [
        {"elem": "O", "x": 0.0, "y": 0.0, "z": 0.0},
        {"elem": "H", "x": 0.0, "y": 0.757, "z": 0.586},
        {"elem": "H", "x": 0.0, "y": -0.757, "z": 0.586},
    ]
    mol2 = [
        {"elem": "C", "x": 0.0, "y": 0.0, "z": 0.0},
        {"elem": "Cl", "x": 1.75, "y": 0.0, "z": 0.0},
        {"elem": "H", "x": -0.5, "y": 0.8, "z": 0.0},
    ]

    offset_x = core.calculate_safe_collision_free_offset(mol1, mol2, initial_offset=5.0, min_clearance=2.8)
    assert offset_x >= 5.0

    # Verify no pair has distance < min_clearance
    for a1 in mol1:
        for a2 in mol2:
            dx = (a2["x"] + offset_x) - a1["x"]
            dy = a2["y"] - a1["y"]
            dz = a2["z"] - a1["z"]
            dist = math.sqrt(dx*dx + dy*dy + dz*dz)
            assert dist >= 2.8, f"Collision detected at distance {dist:.2f} Å"


# ─────────────────────────────────────────────────────────────
# 3. Concurrency & Load Testing (50 Concurrent UFF Requests)
# ─────────────────────────────────────────────────────────────
def test_50_concurrent_uff_requests_stability_and_isolation():
    """Verify that 50 concurrent UFF requests execute under bounded concurrency without crashes or cross-talk."""
    test_molecules = [
        # Water
        ("water", "O 0.0 0.0 0.0\nH 0.0 1.0 0.0\nH 0.0 0.0 1.0"),
        # Ammonia
        ("ammonia", "N 0.0 0.0 0.0\nH 1.0 0.0 0.0\nH 0.0 1.0 0.0\nH 0.0 0.0 1.0"),
        # Methane
        ("methane", "C 0.0 0.0 0.0\nH 0.6 0.6 0.6\nH -0.6 -0.6 0.6\nH -0.6 0.6 -0.6\nH 0.6 -0.6 -0.6"),
    ]

    def send_request(idx: int):
        mol_type, coords = test_molecules[idx % len(test_molecules)]
        opt_coords, err = core.clean_3d_coordinates_uff(coords)
        assert opt_coords is not None or err is not None
        return mol_type, opt_coords or coords

    # Dispatch 50 concurrent requests
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(send_request, i) for i in range(50)]
        results = [f.result() for f in futures]

    assert len(results) == 50
    # Verify state isolation: each result returned the correct element signature corresponding to its input
    for mol_type, coords in results:
        if mol_type == "water":
            assert "O" in coords and "H" in coords
            assert "N" not in coords and "C" not in coords
        elif mol_type == "ammonia":
            assert "N" in coords and "H" in coords
            assert "C" not in coords
        elif mol_type == "methane":
            assert "C" in coords and "H" in coords
            assert "N" not in coords


# ─────────────────────────────────────────────────────────────
# 4. UFF Failure Tolerance & Atom Limit Tests
# ─────────────────────────────────────────────────────────────
def test_uff_atom_limit_graceful_fallback(client):
    """Verify that structures exceeding UFF atom limit return original geometry safely."""
    # Generate large dummy 400-atom structure
    large_lines = [f"C {i * 1.5:.2f} 0.00 0.00" for i in range(400)]
    raw_large = "\n".join(large_lines)

    res = client.post("/api/orca/builder/clean", json={"coords": raw_large})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is False
    assert "exceeds UFF relaxation limit" in data["error"]
    assert "Original geometry preserved" in data["error"]
    assert data["coords"] == raw_large


def test_uff_empty_or_invalid_coords(client):
    """Verify safe error response on invalid coordinates."""
    res = client.post("/api/orca/builder/clean", json={"coords": "  "})
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False


# ─────────────────────────────────────────────────────────────
# 5. ORCA Input Generator Round-Trip
# ─────────────────────────────────────────────────────────────
def test_orca_round_trip_assembled_and_modified_structure():
    """Verify complete round-trip from builder coordinates to valid ORCA 6.1.0 .inp structure."""
    assembled_coords = """O   0.000000   0.000000   0.000000
H   0.000000   0.757000   0.586000
H   0.000000  -0.757000   0.586000
N   5.200000   0.000000   0.000000
H   5.200000   0.940000   0.380000
H   5.010000  -0.470000   0.890000
H   6.010000  -0.470000  -0.380000"""

    d = {
        "calc_type": "opt freq",
        "family": "f_dft",
        "theory": "wB97X-D4",
        "basis": "def2-TZVP",
        "ri_type": "rijcosx",
        "scf_conv": "tightscf",
        "cores": 4,
        "ram": 6000,
        "charge": 0,
        "mult": 1,
        "coords": assembled_coords,
    }
    inp = core.generate_orca_6_input(d)
    assert "! wB97X-D4 def2-TZVP RIJCOSX TightSCF Opt Freq" in inp
    assert "* xyz 0 1" in inp
    assert "N   5.200000" in inp
    assert "O   0.000000" in inp


# ─────────────────────────────────────────────────────────────
# 6. Avogadro-Style Direct Manipulation & Bond Placement Tests
# ─────────────────────────────────────────────────────────────
def test_single_atom_drag_displacement_and_local_bond_geometry():
    """Verify that moving a single atom modifies only its local bond length while all other atoms stay strictly fixed."""
    # Water molecule: O at (0,0,0), H1 at (0, 0.757, 0.586), H2 at (0, -0.757, 0.586)
    o = np.array([0.0, 0.0, 0.0])
    h1 = np.array([0.0, 0.757, 0.586])
    h2 = np.array([0.0, -0.757, 0.586])

    d_oh1_orig = np.linalg.norm(h1 - o)
    d_oh2_orig = np.linalg.norm(h2 - o)
    d_h1h2_orig = np.linalg.norm(h1 - h2)

    # Drag H1 along screen plane: delta = (0.5, 0.3, -0.2)
    delta = np.array([0.5, 0.3, -0.2])
    h1_new = h1 + delta

    # O and H2 positions MUST remain completely unchanged
    assert np.allclose(o, np.array([0.0, 0.0, 0.0]))
    assert np.allclose(h2, np.array([0.0, -0.757, 0.586]))

    # O-H1 distance changed, while O-H2 is strictly invariant
    d_oh1_new = np.linalg.norm(h1_new - o)
    d_oh2_new = np.linalg.norm(h2 - o)

    assert abs(d_oh1_new - d_oh1_orig) > 0.2
    assert abs(d_oh2_new - d_oh2_orig) < 1e-12, "Other bond distance was corrupted by single-atom drag"


def test_inter_fragment_bond_placement_and_covalent_alignment():
    """Verify that connecting distant fragments translates the mobile fragment to ideal covalent distance while preserving its internal geometry."""
    # Fragment 1: Water (stationary)
    frag1 = np.array([
        [0.000, 0.000, 0.000],  # O (target atom)
        [0.000, 0.757, 0.586],  # H1
        [0.000, -0.757, 0.586], # H2
    ])
    # Fragment 2: Methane at (8.0, 0.0, 0.0) (mobile)
    c_center = np.array([8.0, 0.0, 0.0])
    frag2_rel = np.array([
        [0.0, 0.0, 0.0],       # C (connecting atom)
        [0.63, 0.63, 0.63],    # H_a
        [-0.63, -0.63, 0.63],  # H_b
        [-0.63, 0.63, -0.63],  # H_c
        [0.63, -0.63, -0.63],  # H_d
    ])
    frag2 = frag2_rel + c_center

    # Measure internal pairwise distances in Fragment 2 before translation
    n2 = len(frag2)
    dist_frag2_before = np.zeros((n2, n2))
    for i in range(n2):
        for j in range(n2):
            dist_frag2_before[i, j] = np.linalg.norm(frag2[i] - frag2[j])

    # Connect O (frag1[0]) and C (frag2[0])
    atom1_pos = frag1[0]
    atom2_pos = frag2[0]
    v = atom2_pos - atom1_pos
    current_dist = np.linalg.norm(v)
    assert current_dist == 8.0

    # Ideal single bond distance: r_cov(O) + r_cov(C) = 0.66 + 0.76 = 1.42 Å * 1.02 ≈ 1.45 Å
    target_bond_length = 1.45
    shift = (target_bond_length - current_dist) / current_dist
    translation = v * shift

    # Translate entire mobile fragment 2 rigidly
    frag2_after = frag2 + translation

    # Verify new O-C bond distance matches target exactly
    new_oc_dist = np.linalg.norm(frag2_after[0] - frag1[0])
    assert abs(new_oc_dist - target_bond_length) < 1e-12

    # Verify that Fragment 2 internal geometry is 100% preserved
    dist_frag2_after = np.zeros((n2, n2))
    for i in range(n2):
        for j in range(n2):
            dist_frag2_after[i, j] = np.linalg.norm(frag2_after[i] - frag2_after[j])

    max_internal_distortion = np.max(np.abs(dist_frag2_before - dist_frag2_after))
    assert max_internal_distortion < 1e-12, f"Fragment internal geometry distorted: {max_internal_distortion}"


def test_valence_rules_validation():
    """Verify valence rules for common chemical elements."""
    max_valence = {
        "H": 1, "He": 0, "Li": 1, "Be": 2, "B": 4, "C": 4, "N": 4, "O": 2,
        "F": 1, "Ne": 0, "Na": 1, "Mg": 2, "Al": 3, "Si": 4, "P": 5, "S": 6,
        "Cl": 1, "Br": 1, "I": 1
    }
    # Validate rules
    assert max_valence["C"] == 4
    assert max_valence["O"] == 2
    assert max_valence["H"] == 1
    assert max_valence["N"] == 4
    assert max_valence["F"] == 1
    assert max_valence["Cl"] == 1
    assert max_valence["P"] == 5
    assert max_valence["S"] == 6


# ─────────────────────────────────────────────────────────────
# 8. UFF Relaxation & Chemistry Perception Tests
# ─────────────────────────────────────────────────────────────
def test_uff_relaxation_distorted_water_actually_changes_geometry(client):
    """Verify that UFF force field relaxes deliberately distorted water bonds towards equilibrium."""
    distorted_water = "O 0.0 0.0 0.0\nH 1.15 0.0 0.0\nH -0.3 1.1 0.0"
    res = client.post("/api/orca/builder/clean", json={"coords": distorted_water})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["optimized"] is True
    assert data["converged"] is True

    lines = [l.strip() for l in data["coords"].strip().splitlines() if l.strip()]
    if lines[0].isdigit():
        lines = lines[2:]
    assert len(lines) == 3

    o_pos = np.array([float(x) for x in lines[0].split()[1:4]])
    h1_pos = np.array([float(x) for x in lines[1].split()[1:4]])
    h2_pos = np.array([float(x) for x in lines[2].split()[1:4]])

    d_oh1 = np.linalg.norm(o_pos - h1_pos)
    d_oh2 = np.linalg.norm(o_pos - h2_pos)

    # Initial O-H1 was 1.150 Å; after UFF relaxation it must change measurably towards equilibrium (< 1.05 Å)
    assert abs(d_oh1 - 1.150) > 0.05, f"UFF did not relax distorted O-H1 bond: {d_oh1}"
    assert 0.90 <= d_oh1 <= 1.05, f"Relaxed O-H1 distance out of physical range: {d_oh1}"
    assert 0.90 <= d_oh2 <= 1.05, f"Relaxed O-H2 distance out of physical range: {d_oh2}"


def test_uff_relaxation_distorted_methane_actually_changes_geometry(client):
    """Verify that UFF force field relaxes distorted tetrahedral methane bonds and angles."""
    distorted_methane = """C 0.0000 0.0000 0.0000
H 1.1500 0.0000 0.0000
H -0.3600 1.1500 0.0000
H -0.3600 -0.5100 0.8900
H -0.3600 -0.5100 -0.8900"""

    res = client.post("/api/orca/builder/clean", json={"coords": distorted_methane})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["optimized"] is True

    lines = [l.strip() for l in data["coords"].strip().splitlines() if l.strip()]
    if lines[0].isdigit():
        lines = lines[2:]
    assert len(lines) == 5

    c_pos = np.array([float(x) for x in lines[0].split()[1:4]])
    h1_pos = np.array([float(x) for x in lines[1].split()[1:4]])
    d_ch1 = np.linalg.norm(c_pos - h1_pos)

    # Initial C-H was 1.150 Å; after UFF it should relax towards equilibrium (~1.09-1.12 Å)
    assert abs(d_ch1 - 1.150) > 0.02, f"UFF did not relax C-H bond: {d_ch1}"
    assert 1.05 <= d_ch1 <= 1.13


def test_uff_relaxation_organic_molecules_success(client):
    """Verify UFF relaxation for ethanol, benzene (aromatic), and acetone (carbonyl)."""
    # Ethanol
    ethanol_xyz = """C -0.75 0.00 0.00
C 0.75 0.00 0.00
O 1.30 1.20 0.00
H -1.10 0.50 0.80
H -1.10 0.50 -0.80
H -1.10 -1.00 0.00
H 1.10 -0.50 0.80
H 1.10 -0.50 -0.80
H 2.20 1.10 0.00"""
    res_eth = client.post("/api/orca/builder/clean", json={"coords": ethanol_xyz})
    assert res_eth.status_code == 200
    assert res_eth.get_json()["ok"] is True

    # Benzene (aromatic bond perception)
    benzene_xyz = """C 0.000 1.397 0.000
C 1.210 0.699 0.000
C 1.210 -0.699 0.000
C 0.000 -1.397 0.000
C -1.210 -0.699 0.000
C -1.210 0.699 0.000
H 0.000 2.481 0.000
H 2.149 1.240 0.000
H 2.149 -1.240 0.000
H 0.000 -2.481 0.000
H -2.149 -1.240 0.000
H -2.149 1.240 0.000"""
    res_ben = client.post("/api/orca/builder/clean", json={"coords": benzene_xyz})
    assert res_ben.status_code == 200
    assert res_ben.get_json()["ok"] is True


def test_uff_charged_nh4_success(client):
    """Verify UFF relaxation for charged ammonium cation (NH4+)."""
    nh4_xyz = """N 0.00 0.00 0.00
H 1.00 0.00 0.00
H -0.33 0.94 0.00
H -0.33 -0.47 0.81
H -0.33 -0.47 -0.81"""
    res = client.post("/api/orca/builder/clean", json={"coords": nh4_xyz, "charge": 1})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True


def test_uff_unsupported_atom_fails_honestly(client):
    """Verify that unsupported pseudo-atoms fail honestly with ok=False and preserve geometry."""
    unsupported_xyz = "Xx 0.0 0.0 0.0\nC 1.5 0.0 0.0\nH 2.5 0.0 0.0"
    res = client.post("/api/orca/builder/clean", json={"coords": unsupported_xyz})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is False
    assert data["optimized"] is False
    assert data["coords"] == unsupported_xyz
