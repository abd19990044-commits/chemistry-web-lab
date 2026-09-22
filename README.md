---
title: "Chemistry Lab: Computational Chemistry Platform"
emoji: ⚛️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Chemistry Lab: Computational Chemistry & Quantum Analysis Suite ⚛️🧪
### Production-Grade Quantum Chemistry Web Platform & Interactive ORCA 6 Studio

[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![ORCA 6.x Compatible](https://img.shields.io/badge/ORCA-6.0%20%7C%206.1-orange.svg)](https://www.faccts.de/orca/)
[![License: Academic & Non-Commercial v1.1](https://img.shields.io/badge/License-Academic%20%26%20Non--Commercial%20v1.1-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22119038.svg)](https://doi.org/10.5281/zenodo.22119038)
[![RDKit Chemistry](https://img.shields.io/badge/RDKit-2025.03-teal.svg)](https://www.rdkit.org/)
[![CI Build Status](https://github.com/abd19990044-commits/chemistry-web-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/abd19990044-commits/chemistry-web-lab/actions/workflows/ci.yml)

---

## 📖 Table of Contents
- [Executive Overview](#-executive-overview)
- [Key Features & Capabilities](#-key-features--capabilities)
- [Primary Execution: Local & HPC Companion Agent](#-primary-execution-local--hpc-companion-agent)
- [Quantum Chemistry Engine (`orca_engine`)](#-quantum-chemistry-engine-orca_engine)
- [AI & Machine Learning Ready Dataset Pipeline](#-ai--machine-learning-ready-dataset-pipeline)
- [Optional Secondary Cloud: Kaggle Orchestrator & Policy Compliance](#️-optional-secondary-cloud-kaggle-orchestrator--policy-compliance)
- [Installation & Local Quickstart](#-installation--local-quickstart)
- [Comprehensive API Endpoints](#-comprehensive-api-endpoints)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Production Deployment (Docker & Hugging Face Spaces)](#-production-deployment)
- [License, Commercial Terms & Citation](#-license-commercial-terms--citation)

---

## 🔬 Executive Overview

**Chemistry Lab** is a modern computational chemistry web platform for molecular structure exploration, high-performance ORCA quantum-chemistry workflows, scientific result analysis, spectroscopy visualization, reaction thermochemistry, and AI/ML scientific dataset generation. It seamlessly unifies:
1. **Interactive Chemical Studios**: 2D molecule drawing, reaction stoichiometry, and PubChem 3D coordinate resolution.
2. **Primary High-Performance Execution**: Standalone **Local & HPC Companion Agent** packages for Windows, Linux, macOS, and supercomputing clusters (Slurm, PBS, LSF) with ephemeral random pairing tokens (`CLA_...`) and full local privacy.
3. **Quantum Chemistry Engine (`orca_engine`)**: WebGL 3D rendering, frontier orbital diagrams (HOMO/LUMO), conceptual DFT descriptors, Gaussian-convoluted UV-Vis/IR/NMR spectra, and composite thermochemistry.
4. **AI & Machine Learning Ready Dataset Pipeline**: One-click generation and export of canonical, tensor-ready quantum datasets (`${name}_canonical_ai_dataset.json`) conforming to Draft 2020-12 schema, complete with GNN graphs (nodes, edges, adjacency/distance matrices), quantum target tensors, invariant physical features, and scaffold-based leak-free cross-validation keys.
5. **Optional Secondary Cloud Fallback**: Fault-tolerant multi-session Kaggle cloud runner for users without local ORCA installations, operating under direct user control in strict compliance with Kaggle Acceptable Use Policies.

---

## 🚀 Key Features & Capabilities

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            CHEMISTRY LAB ARCHITECTURE                           │
├────────────────────────┬──────────────────────────┬─────────────────────────────┤
│   MOLECULAR STUDIO     │   LOCAL & HPC RUNNER     │    ORCA RESULTS ANALYZER    │
│  • 2D Molecule Drawer  │  • Multi-OS Agent Hub    │  • WebGL 3D Molecular View  │
│  • SMILES / Name Query │  • Windows/Linux/macOS   │  • HOMO-LUMO Gap Diagram    │
│  • PubChem 3D Resolver │  • HPC Slurm/PBS/LSF     │  • Conceptual DFT (CDFT)    │
│  • MDL RXN Reaction 2D │  • Ephemeral Pairing API │  • TD-DFT UV-Vis Spectrum   │
│  • Stoichiometry Check │  • 100% Local Privacy    │  • IR & NMR Spectroscopy    │
│  • One-Click Thermo    │  • Full Native Speed     │  • Reaction Thermochemistry │
├────────────────────────┴──────────────────────────┴─────────────────────────────┤
│                    CANONICAL AI & MACHINE LEARNING PIPELINE                     │
│  • 100% Canonical Schema Conformance (Draft 2020-12, Zero Raw Text Dumps)      │
│  • GNN-Ready Graphs: Atoms (Nodes), Bonds (Edges), Adjacency & Distance Tensors │
│  • Quantum Targets: Energies, Orbitals, Dipoles, CDFT, Partial Charges, TD-DFT  │
│  • Invariant Features: 3D Coords, Masses, Center of Mass, Radius of Gyration    │
│  • Deterministic Scaffold Splitting (InChIKey Hashes) for Leak-Free Training    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                    OPTIONAL SECONDARY CLOUD COMPUTATION                         │
│  • Optional Kaggle Cloud Runner with Multi-Session Auto-Continuation            │
│  • Strict Kaggle Policy Compliance: Direct User Operation, Sole User Liability  │
│  • Protected Cloud Passcode Gate & Encrypted Credential Vault (AES-256-GCM)     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 💻 Primary Execution: Local & HPC Companion Agent

The **primary and recommended execution pathway** for Chemistry Lab is direct execution on your own hardware using the lightweight, standalone **Chemistry Lab Companion Agent**. This eliminates cloud timeouts, keeps your research data 100% private, and leverages your workstation's or cluster's full hardware potential (multi-threading, GPU acceleration, and AVX-512 optimizations).

### 1. Workflow & Connection Model
1. **Choose Execution Platform**: Windows, Linux, macOS, or HPC Cluster.
2. **Download Package**: From the topbar or homepage, click **📥 Download Local Runner** to retrieve the pre-packaged ZIP archive (`ChemistryLabAgent-Windows.zip`, `ChemistryLabAgent-Linux.zip`, `ChemistryLabAgent-macOS.zip`, `ChemistryLabAgent-HPC.zip`).
3. **Extract Archive**: Extract to any directory (e.g. `C:\ChemistryLabAgent` or `~/ChemistryLabAgent`).
4. **Run Launcher**:
   - **Windows**: Run `install_and_run.bat` (or PowerShell `bootstrap_windows.ps1`).
   - **Linux / macOS**: Run `bash install_and_run.sh`.
   - **HPC Cluster**: Run `bash install_hpc_agent.sh --scheduler slurm`.
5. **Ephemeral Connection API**: Every time the Agent script starts, it generates a **NEW** cryptographically random Connection API (`CLA_...` with >= 256 bits of entropy) and displays it in the terminal.
6. **Copy API**: Copy the `CLA_...` token from the terminal.
7. **Website Connection**: On the Chemistry Lab website, navigate to **Connect Computer** (or click the device icon in the topbar).
8. **Paste API**: Enter the Connection API (and optional custom computer name) and click **Connect**.
9. **Configure ORCA**: Specify your local ORCA installation path and working directories.
10. **Hardware Resources**: Specify CPU cores (`%pal`), RAM (`%maxcore` with 80% safety margin), and Disk limit (`%scf MaxDisk`).
11. **Select Device**: In the Quantum Calculator or Reaction Workflow, choose your connected computer from the device dropdown.
12. **Run Calculation**: Submit the calculation. The job is securely dispatched to your machine and results return automatically.
13. **Resilient Reconnection**: Temporary network drops automatically reconnect without requiring re-pairing or browser prompts.
14. **Process Restart Model**: If you stop or restart the Agent process, the old API becomes permanently invalid and a new API must be entered.

### 2. HPC Supercomputer Integration
For university and enterprise clusters, the Agent runs on a login or service node and interfaces directly with the cluster workload manager:
- **Architecture**: `Chemistry Lab Server -> (Secure WSS) -> HPC Agent on Login Node -> (Scheduler) -> Compute Nodes -> ORCA`.
- **Supported Adapters**: SLURM (`sbatch`), PBS Pro / Torque (`qsub`), and IBM LSF (`bsub`).
- **Profile-Aware**: Directives are structured and site-profile aware based on institutional MPI configurations.

---

## 🔬 Quantum Chemistry Engine (`orca_engine`)

The integrated quantum engine (`orca_engine/`) provides robust parsing, numerical post-processing, and interactive visualization:

### 1. WebGL 3D Molecule Visualizer
- Powered by `3Dmol.js` with hardware-accelerated WebGL rendering.
- Multiple representations: **Ball & Stick**, **Sticks**, **Spacefill (CPK van der Waals)**, **Wireframe**.
- Real-time rotation, automated spin, atom element labeling, and coordinate export (`.xyz`).

### 2. Frontier Orbitals & HOMO-LUMO Energy Gap
- Automatically detects occupied and virtual canonical/Kohn-Sham orbitals.
- Computes orbital energies in both **Hartree ($E_h$)** and **electron-volts ($\text{eV}$)**.
- Renders a stylized energy level diagram with clear gap metrics.

### 3. Conceptual DFT (CDFT) Reactivity Descriptors
Calculates fundamental reactivity parameters from frontier orbital eigenvalues:
- **Ionization Potential ($I \approx -E_{\text{HOMO}}$)**
- **Electron Affinity ($A \approx -E_{\text{LUMO}}$)**
- **Chemical Hardness ($\eta = \frac{I - A}{2}$)**
- **Chemical Softness ($S = \frac{1}{2\eta}$)**
- **Electronegativity ($\chi = \frac{I + A}{2}$)**
- **Electronic Chemical Potential ($\mu = -\chi$)**
- **Electrophilicity Index ($\omega = \frac{\mu^2}{2\eta}$)**

### 4. TD-DFT UV-Vis Spectrum & Gaussian Convolution
- Extracts singlet/triplet excitation energies (wavenumber $\text{cm}^{-1}$, wavelength $\text{nm}$) and oscillator strengths ($f_{\text{osc}}$).
- Performs real-time Gaussian spectral convolution:
  $$I(\lambda) = \sum_{i} f_i \cdot \exp\left( -\frac{(\lambda - \lambda_i - \Delta\lambda)^2}{2\sigma^2} \right)$$
- Interactive sliders for standard deviation broadening ($\sigma \in [5, 50]\,\text{nm}$) and wavelength offset ($\Delta\lambda \in [-50, +50]\,\text{nm}$).
- Direct export of spectral curves to CSV and high-resolution chart images.

### 5. Reaction Thermochemistry & Equilibrium Constant
- Balances multi-species reaction equations (e.g. `A + 2 B -> C`).
- Computes:
  - $\Delta E_{\text{electronic}}$
  - $\Delta H^\circ$ (Enthalpy change in $\text{kcal/mol}$ and $\text{kJ/mol}$)
  - $\Delta G^\circ$ (Gibbs free energy change in $\text{kcal/mol}$ and $\text{kJ/mol}$)
  - $\Delta S^\circ$ (Entropy change in $\text{cal/(mol}\cdot\text{K)}$ and $\text{J/(mol}\cdot\text{K)}$)
  - Equilibrium constant $K_{\text{eq}} = \exp\left(-\frac{\Delta G^\circ}{RT}\right)$
- Validates level of theory consistency across all participating species.

---

## 🤖 AI & Machine Learning Ready Dataset Pipeline

Chemistry Lab transforms raw quantum chemical calculation outputs into standardized, canonical, and tensor-ready scientific datasets specifically engineered for training modern Artificial Intelligence and Machine Learning models, including Graph Neural Networks (GNNs) and Equivariant Transformers (e.g. SchNet, PaiNN, DimeNet, EGNN, Equiformer, Graphormer).

### 1. Canonical Schema Standardization & Noise Elimination
- **Strict Schema Compliance**: Every exported record validates 100% against `schema/canonical_scientific.schema.json` (Draft 2020-12) with zero validation warnings.
- **Zero Raw Terminal Dumps**: Unstructured terminal logs (`raw_text`), temporary upload folder paths, ephemeral session identifiers, and debug keys are completely purged from the dataset payload.
- **Normalized Field Structure**: Quantities are represented with explicit units, standardized naming, and unambiguous numeric scales.

### 2. Quantum Ground-Truth Target Tensors (`targets`)
The `targets` dictionary aggregates high-precision electronic and spectroscopic properties ready for regression and multi-task learning:
- **Ground-State & Thermal Energetics**:
  - `electronic_energy_hartree`, `electronic_energy_ev`: Single-point electronic ground-state energy.
  - `zero_point_energy_hartree`, `zero_point_energy_ev`: Zero-point vibrational energy (ZPE).
  - `e0_energy_hartree`, `e0_energy_ev`: Total ground-state energy at 0 K ($E_0 = E_{\text{elec}} + \text{ZPE}$).
  - `enthalpy_hartree`, `enthalpy_kcal_mol`: Total molecular enthalpy ($H^\circ$).
  - `gibbs_free_energy_hartree`, `gibbs_free_energy_kcal_mol`: Total Gibbs free energy ($G^\circ$).
  - `entropy_cal_mol_k`: Total molecular entropy ($S^\circ$).
- **Frontier Orbitals & Reactivity**:
  - `homo_energy_ev`, `lumo_energy_ev`, `homo_lumo_gap_ev`: Highest occupied and lowest unoccupied molecular orbital energies and the fundamental gap.
  - `cdft_indices`: Conceptual DFT global reactivity descriptors including chemical potential ($\mu$), electronegativity ($\chi$), chemical hardness ($\eta$), softness ($S$), and electrophilicity index ($\omega$).
  - `dipole_moment_debye`, `dipole_moment_vector`: Total dipole moment and directional 3D vector $[D_x, D_y, D_z]$ in Debye.
- **Vibrational Spectroscopy & Transition State Classification**:
  - `harmonic_frequencies_cm1`: Array of harmonic vibrational frequencies ($\text{cm}^{-1}$).
  - `ir_intensities_km_mol`: Array of infrared absorption intensities ($\text{km/mol}$).
  - `imaginary_frequencies_count`: Integer count of imaginary vibrational frequencies ($\nu < 0\,\text{cm}^{-1}$).
  - `is_transition_state`: Boolean ground-truth label indicating a first-order saddle point (exactly one imaginary frequency).
- **Atomic Partial Charges & Population Analysis**:
  - `partial_charges`: Atomic partial charges from Mulliken, Loewdin, Hirshfeld, and Mayer bond-order population analyses.
- **Excited-State Photochemistry (TD-DFT)**:
  - `excited_states`: Multi-state excitation energies (eV), transition wavelengths (nm), oscillator strengths ($f_{\text{osc}}$), and electronic configurations for photochemical prediction.

### 3. Physical Invariant Features (`derived_features`)
Geometric and stoichiometric descriptors calculated from standard IUPAC tables and 3D atomic coordinates:
- `num_atoms`: Total number of atoms $N$.
- `atomic_numbers`: Integer nuclear charges $Z \in \mathbb{Z}^N$.
- `elements`: Ordered list of IUPAC element symbols.
- `cartesian_coordinates_angstrom`: Geometry coordinate tensor of shape $N \times 3$ in Ångströms.
- `stoichiometry_formula`: Standard Hill-system molecular formula string.
- `total_charge`: Formal net charge of the molecular system.
- `spin_multiplicity`: Spin multiplicity ($2S + 1$).
- `molecular_mass_amu`: Accurate molecular mass based on standard IUPAC atomic weights.
- `center_of_mass_angstrom`: Three-dimensional center-of-mass coordinate vector $[X_{\text{cm}}, Y_{\text{cm}}, Z_{\text{cm}}]$.
- `radius_of_gyration_angstrom`: Geometric radius of gyration ($R_g$) measuring spatial molecular mass dispersion.

### 4. Graph Neural Network (GNN) Molecular Graph Representation (`graph`)
Ready for ingestion into graph machine learning frameworks (PyTorch Geometric, DGL, JAX):
- `nodes`: List of atomic node objects containing atom index, atomic number $Z$, element symbol, exact mass, formal charge, and 3D Cartesian coordinates.
- `edges`: List of directional edge pairs with source atom, target atom, interatomic Euclidean distance in Ångströms, and covalent bond order.
- `adjacency_matrix`: Binary topological connectivity matrix ($N \times N$).
- `distance_matrix`: Pairwise interatomic Euclidean distance tensor ($N \times N$) in Ångströms for distance-based spatial convolution.

### 5. Leak-Free Cross-Validation Partitioning & Metadata
- **Deterministic Split Group Keys (`split_group_key`)**: Computes a deterministic InChIKey or canonical scaffold hash. This guarantees that conformational geometries, isotopologues, or identical chemical scaffolds can be grouped consistently to prevent data leakage between training, validation, and testing partitions.
- **Standardized Training Metadata (`training_metadata`)**:
  - `task_type`: `quantum_property_prediction`.
  - `input_modality`: `molecular_graph_and_3d_coordinates`.
  - `target_modality`: `scalar_energies_and_tensors`.
  - `quality_flags`: Captures electronic and geometric SCF convergence states.

### 6. Interactive & Programmatic Export
- **One-Click Web Export**: In the ORCA Results Analyzer, click **⬇ Export AI/ML JSON** to download a clean, production-ready `${name}_canonical_ai_dataset.json` file instantly from memory.
- **REST API Endpoint**: Automated pipelines can fetch canonical records directly via `POST /api/orca/engine/export-ai-dataset`.

---

## ☁️ Optional Secondary Cloud: Kaggle Orchestrator & Policy Compliance

For users who do not have a local ORCA installation or need temporary remote GPU/CPU compute, Chemistry Lab provides an **optional secondary cloud fallback** powered by Kaggle notebooks.

### Direct User Operation & Legal Responsibility
- **Direct Operation Without Third-Party Intermediation**: Chemistry Lab does NOT operate Kaggle as a third-party service provider or broker. The platform is designed for direct personal user operation; all calculations are executed directly within the user's private Kaggle account using their own personal credentials without server retention.
- **Sole User Liability**: When running calculations on Kaggle, the user is solely and entirely responsible for compliance with Kaggle's Terms of Service, Acceptable Use Policy, and community rules, and bears full legal and operational liability for any violation thereof.

### Kaggle Acceptable Use Policy & Ethical Compute Standards
To ensure that all interactions with Kaggle remain fully ethical, lawful, and compliant with Kaggle's platform policies:
1. **Legitimate Scientific & Educational Workloads Only**: Calculations dispatched to Kaggle are strictly confined to legitimate computational chemistry simulations, educational demonstrations, and academic modeling using ORCA. General-purpose arbitrary code execution, bot hosting, and background proxies are not permitted.
2. **Zero Commercial Compute Resale**: Chemistry Lab is an academic, non-commercial software project. It does NOT resell, broker, sub-license, or monetize Kaggle compute resources.
3. **Individual Account Authenticity**: Every job runs under the user's individual Kaggle account using their personal API token. The platform does not provide shared pool accounts, public tokens, or automated multi-account rotation.
4. **Adherence to Platform Quotas & Resource Limits**: Users must strictly respect Kaggle's resource limits (e.g. weekly GPU/TPU hours and 12-hour session limits). Chemistry Lab does not attempt to circumvent quotas, bypass resource restrictions, or run denial-of-service workloads.
5. **Strictly Prohibited Activities**: In accordance with Kaggle's Acceptable Use Policy, users are strictly prohibited from using Chemistry Lab or its Kaggle integration for:
   - Cryptocurrency mining or distributed ledger operations;
   - Denial-of-service (DoS) attacks or abusive network port scanning;
   - Web scraping, bulk crawling, or data harvesting bots;
   - Any activity violating applicable national, international, or institutional export control laws.

### Execution Modes & Passcode Security (Local vs. Cloud / Domain)
When clicking the Kaggle button in the user interface, an access password field is presented:
- **Local Execution**: If Chemistry Lab is run locally on your own machine (where no secret is configured), simply leave the password field empty and press **Enter** (or click Unlock) to immediately open the Kaggle login fields (`Kaggle Username` and `Kaggle API Key`).
- **Cloud Hosting / Custom Domain**: If Chemistry Lab is deployed to a cloud host (e.g. Hugging Face Spaces, Render, AWS, Docker) or configured with a custom domain, an execution passcode must be set in the server environment secrets (`KAGGLE_EXECUTION_PASSCODE`, `KAGGLE_ACCESS_CODE`, or `KAGGLE_PASSCODE`). Entering this secret passcode and pressing **Enter** unlocks the Kaggle login fields; unauthorized access is blocked. This safeguard prevents open, unauthenticated third parties from accessing the Kaggle submission interface on public deployments.
- **Platform Independence**: All other studios and tools (Reaction Thermochemistry, Analyzer, Spectra Studios, 3D Builder, Local/HPC Companion Agent) remain completely unrestricted and operate freely without requiring any passcode.

### Technical Architecture
`orca_orchestrator` implements an enterprise-grade finite state machine for remote batch execution:
- **Deterministic Checkpointing**: Stages `.gbw`, `.xyz`, `.hess`, and temporary restart vectors.
- **Auto-Continuation Chaining**: Seamlessly creates successor kernels upon reaching timeouts or resource warnings (bypasses 12h session limits).
- **Integrity Validation**: Verifies that the calculation actually converged rather than just checking exit codes.
- **Encrypted Credential Persistence (AES-256-GCM AEAD)**: User Kaggle credentials are encrypted using AES-256-GCM with owner-bound associated data and stored in Cloudflare D1. Zero plaintext is stored in Cloudflare or logs. Decryption occurs strictly in backend process RAM on demand.
- **Strict Multi-Tenant Isolation**: Stalled jobs from User A recover in background threads when any user accesses the Space, without leaking credentials or outputs to other sessions.

---

## 💻 Installation & Local Quickstart

### Prerequisites
- Python 3.11, 3.12, or 3.13 (`>=3.11, <3.14`)
- `git`

### 1. Clone the Repository
```bash
git clone https://github.com/abd19990044-commits/chemistry-web-lab.git
cd chemistry-web-lab
```

### 2. Create and Activate a Virtual Environment
```bash
# On Linux / macOS:
python3 -m venv .venv
source .venv/bin/activate

# On Windows (PowerShell):
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Run the Development Server
```bash
python app.py
```
Open your browser and navigate to: **`http://127.0.0.1:7860`**

---

## 📡 Comprehensive API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | `GET` | System health check, ORCA engine status, and Kaggle CLI readiness. |
| `/api/orca/engine/status` | `GET` | Reports quantum engine availability, capabilities, and version. |
| `/api/orca/engine/parse` | `POST` | Parses raw ORCA output text or uploaded `.out`/`.log` files into structured JSON. |
| `/api/orca/engine/export-ai-dataset` | `POST` | Streams clean, canonical AI/ML dataset JSON (`${name}_canonical_ai_dataset.json`) with targets and GNN graphs. |
| `/api/orca/engine/convolute` | `POST` | Computes Gaussian spectral convolution for UV-Vis transitions. |
| `/api/orca/engine/thermochemistry` | `POST` | Evaluates reaction energetics ($\Delta G^\circ, \Delta H^\circ, \Delta S^\circ, K_{eq}$) from multiple species. |
| `/api/orca/engine/samples` | `GET` | Lists and loads built-in calculation outputs for instant demonstration. |
| `/api/orca/engine/analyze-job` | `POST` | Fetches and analyzes calculation outputs directly from a Kaggle job. |
| `/api/orca/coords` | `POST` | Resolves compound names and SMILES to 3D Cartesian coordinates via PubChem. |
| `/api/orca/generate` | `POST` | Generates validated ORCA 6 calculation input blocks. |
| `/api/reaction` | `POST` | Processes and renders 2D reaction diagrams with stoichiometry and MDL RXN file. |
| `/api/kaggle/verify-passcode` | `POST` | Verifies execution passcode for Kaggle submission gate (`KAGGLE_EXECUTION_PASSCODE`). |
| `/api/kaggle/submit` | `POST` | Submits a long-running ORCA calculation to Kaggle supporting Dataset or Direct Link. |
| `/api/kaggle/sync` | `POST` | Synchronizes active and completed calculations for the signed-in user. |
| `/api/kaggle/download` | `GET` | Downloads the complete verified scientific result bundle (`.zip`) containing Molden, `.out`, `.xyz`, and scratch files. |
| `/api/kaggle/credentials` | `GET` / `POST` / `DELETE` | Retrieves metadata, securely stores AES-256-GCM ciphertext, or purges user credentials. |


---

### 5. ORCA Source Provisioning & Result Downloads
- **Two Supported ORCA Sources**:
  1. **Kaggle Dataset**: Supply a private dataset identifier (e.g. `username/dataset` or dummy example `jon534/orca6`) or full URL.
  2. **Google Drive / Direct Link**: Provide a direct HTTP/Drive download link to your licensed Linux ORCA archive.
- **Canonical Full ZIP Result Download**:
  - Single, canonical download button: `📦 Full ZIP`.
  - Automatically generates **Molden format** orbital representations (`orca_2mkl <basename> -molden`) when `.gbw` wavefunctions exist.
  - Manifest provenance tracks all artifacts with SHA-256 hashes (`artifact_type="molden"`).
  - Compatible with all download managers (IDM, FDM) and native browser streams.

---

### 6. Client-Side Archive & File Processing Pipeline (Web Worker)
- Offloads archive decompression (ZIP, TAR, GZ, TGZ) and metadata scanning to a dedicated background Web Worker (`static/js/workers/archive-worker.js`) to keep the main UI thread 100% responsive.
- Configurable limits: 500 MB max archive, 1 GB max extracted, 250 MB max individual file, 2,000 files max, and 100× decompression-bomb defense.
- Path traversal & Zip-Slip protection via path sanitization.
- Web Cryptography SHA-256 deduplication and 6-tier file classification (`ANALYZE`, `KEEP`, `IGNORE`, `DUPLICATE`, `UNKNOWN`, `UNSUPPORTED`).
- `UNKNOWN` is never deleted automatically - it is preserved and highlighted with an inspection badge for user review.
- Direct in-browser switching and loading of individual calculation files into the Quantum Engine.

---

## 🧪 Testing & Quality Assurance

Chemistry Lab includes a comprehensive, 100% automated test suite with full mock harnesses and zero external dependencies:

```bash
# 1. Run full test suite with detailed reporting
pytest -v

# 2. Run release integrity and security verification audit
python tools/verify_release.py

# 3. Run frontend DOM & runtime integration tests (22 tests)
python tests/test_frontend.py

# 4. Run quantum chemistry engine unit tests & scientific benchmarks
pytest orca_engine/tests/ tests/test_scientific_benchmarks.py -v

# 5. Run archive security and quota test suite
pytest tests/test_archive_security.py -v
```

See [SECURITY.md](SECURITY.md) for details on the security architecture, archive validation rules, and vulnerability reporting.

---

## 🐳 Production Deployment

### Docker Deployment
The repository includes a production-ready `Dockerfile`:

```bash
# Build the Docker container
docker build -t chemistry-lab:latest .

# Run the container on port 7860
docker run -p 7860:7860 --name chemistry-lab chemistry-lab:latest
```

### Hugging Face Spaces Deployment
To deploy as a Hugging Face Space:
1. Create a new Space with the **Docker** SDK.
2. Push this repository to the Space remote.
3. Configure Space Secrets:
   - `SECRET_KEY`: Random 64-character hex string.
   - `ORCA_STATE_DIR`: `/data` (for persistent storage).
   - `KAGGLE_EXECUTION_PASSCODE`: Secret passcode required for cloud deployments to unlock Kaggle calculation submission.

---

## 📜 License, Commercial Terms & Citation

### Proprietary Source-Available Software
Chemistry Lab is proprietary software owned solely by **Abdulsalam S. Hasan**.

The software is available free of charge for permitted academic, educational, personal, and other non-commercial uses under the **ORCA Web Lab Academic and Non-Commercial License v1.1**.

- **Permitted Free Use:** Personal learning, classroom teaching, academic coursework, student theses, dissertation research, independent academic research resulting in open scholarly publications, and scientific benchmarking.
- **Nature-and-Purpose Standard:** Eligibility is determined by the non-commercial nature and purpose of the activity, not solely by organizational status.
- **Source Available:** The complete source code is accessible for academic study, peer review, scientific reproducibility, and local non-commercial adaptation.
- **Ownership Retention:** Copyright (c) 2026 Abdulsalam S. Hasan. All rights reserved. The license grants usage permission only and does NOT transfer ownership, title, copyright, commercial rights, or trademark rights.

See the complete terms in the [LICENSE](LICENSE) file.

### Commercial Licensing & Enterprise Use
Commercial use is **strictly prohibited** under the Academic and Non-Commercial License. A separate, written **Commercial License Agreement** is required for:
- Use within or on behalf of for-profit corporations (including internal corporate R&D);
- Commercial SaaS, cloud hosting, or calculation services for paying customers;
- Fee-for-service testing, contract research, or consulting deliverables;
- Sponsored commercial research conducted on behalf of commercial entities;
- Integration into commercial products, hardware appliances, or OEM distributions.

Commercial terms, pricing, and deployment parameters are negotiated separately.

### Future Intellectual Property Assignment
Any assignment or transfer of proprietary intellectual property rights owned or controlled by Abdulsalam S. Hasan must be executed through a separate, written Intellectual Property Assignment Agreement. Third-party software components remain subject to their respective licenses and are not assigned.

### Licensing & Commercial Inquiries
For commercial licensing, Enterprise editions, cloud deployment permissions, or intellectual property inquiries:

- **Author & Rights Holder:** Abdulsalam S. Hasan
- **Email:** [abd.19990044@gmail.com](mailto:abd.19990044@gmail.com)
- **WhatsApp:** [+9647715541279](https://wa.me/9647715541279)
- **Repository:** [https://github.com/abd19990044-commits/chemistry-web-lab](https://github.com/abd19990044-commits/chemistry-web-lab)

### Third-Party Software & ORCA Boundary
- **Third-Party Components:** External libraries (RDKit, Flask, Cryptography, 3Dmol.js, etc.) remain governed by their respective open-source licenses. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the complete dependency inventory.
- **ORCA Notice:** ORCA is a proprietary quantum chemistry package developed by Frank Neese et al. (Max Planck Institute für Kohlenforschung / FACCTs GmbH). Chemistry Lab is an independent software tool that interfaces with ORCA and does NOT bundle or distribute ORCA binaries. Users must independently obtain an authorized license directly from the official ORCA forum (https://orcaforum.kofo.mpg.de/).

### Citation
If you use Chemistry Lab in your academic research, teaching, or computational modeling, please cite:

```bibtex
@software{Hasan2026ChemistryLab,
  author  = {Hasan, Abdulsalam S.},
  title   = {{Chemistry Lab: A Web-Based Computational Chemistry Platform}},
  year    = {2026},
  version = {1.0.3},
  doi     = {10.5281/zenodo.22119038},
  url     = {https://doi.org/10.5281/zenodo.22119038},
  note    = {Academic and Non-Commercial Research Platform}
}
```
