---
title: Chemistry Lab: Computational Chemistry Platform
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
[![RDKit Chemistry](https://img.shields.io/badge/RDKit-2025.03-teal.svg)](https://www.rdkit.org/)
[![CI Build Status](https://github.com/abd19990044-commits/chemistry-web-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/abd19990044-commits/chemistry-web-lab/actions/workflows/ci.yml)

---

## 📖 Table of Contents
- [Executive Overview](#-executive-overview)
- [Key Features & Capabilities](#-key-features--capabilities)
- [System Architecture](#️-system-architecture)
- [Quantum Chemistry Engine (`orca_engine`)](#-quantum-chemistry-engine-orca_engine)
- [Fault-Tolerant Kaggle Orchestrator (`orca_orchestrator`)](#️-fault-tolerant-kaggle-orchestrator)
- [Installation & Local Quickstart](#-installation--local-quickstart)
- [Comprehensive API Endpoints](#-comprehensive-api-endpoints)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Production Deployment (Docker & Hugging Face Spaces)](#-production-deployment)
- [License, Commercial Terms & Citation](#-license-commercial-terms--citation)

---

## 🔬 Executive Overview

**Chemistry Lab** is a web-based computational chemistry platform for molecular structure exploration, ORCA quantum-chemistry workflows, scientific result analysis, spectroscopy visualization, thermochemistry, and fault-tolerant execution of long-running computational jobs. It seamlessly unifies modern interactive molecular visualization, chemistry toolsets, automated ORCA 6 input generation, an embedded quantum analysis engine (`orca_engine`), and a self-healing multi-session Kaggle orchestrator (`orca_orchestrator`).

The platform bridges the gap between raw quantum chemistry output files and interactive scientific insights, allowing researchers to explore geometries in WebGL 3D, inspect frontier orbitals, examine conceptual DFT reactivity indices, generate broadened UV-Vis absorption spectra, and evaluate reaction thermochemistry in a single browser window.

---

## 🚀 Key Features & Capabilities

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                            CHEMISTRY LAB ARCHITECTURE                           │
├────────────────────────┬──────────────────────────┬─────────────────────────────┤
│   MOLECULAR STUDIO     │    QUANTUM CALCULATOR    │    ORCA RESULTS ANALYZER    │
│  • 2D Molecule Drawer  │  • ORCA 6 Input Builder  │  • WebGL 3D Molecular View  │
│  • SMILES / Name Query │  • Level of Theory / DFT │  • HOMO-LUMO Gap Diagram    │
│  • PubChem 3D Resolver │  • Solvation & Dispersion│  • Conceptual DFT (CDFT)    │
│  • MDL RXN Reaction 2D │  • Multi-Core RAM Setup  │  • TD-DFT UV-Vis Spectrum   │
│  • Stoichiometry Check │  • Coordinate Converter  │  • Reaction Thermochemistry │
├────────────────────────┴──────────────────────────┴─────────────────────────────┤
│                        CLOUD COMPUTATION & LIFECYCLE                            │
│  • Ephemeral Kaggle API Execution & Automated Kernel Management                  │
│  • Multi-Session Auto-Continuation (Bypasses 12h Session Limits)                │
│  • State Machine Ledger, Checkpoint Validation & Wavefunction Continuity        │
└─────────────────────────────────────────────────────────────────────────────────┘
```

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

## ☁️ Fault-Tolerant Kaggle Orchestrator

Long-running calculations (high-level coupled cluster, transition-state optimizations, IRCs, molecular dynamics) frequently exceed single-session execution limits on cloud notebook providers.

`orca_orchestrator` implements an enterprise-grade finite state machine:
- **Deterministic Checkpointing**: Stages `.gbw`, `.xyz`, `.hess`, and temporary restart vectors.
- **Auto-Continuation Chaining**: Seamlessly creates successor kernels upon reaching timeouts or resource warnings.
- **Integrity Validation**: Verifies that the calculation actually converged rather than just checking exit codes.
- **Encrypted Credential Persistence (AES-256-GCM AEAD)**: Solves Hugging Face Space sleep/restart state loss. User Kaggle credentials are encrypted using AES-256-GCM with owner-bound associated data and stored in Cloudflare D1. Zero plaintext is stored in Cloudflare or logs. Decryption occurs strictly in backend process RAM on demand.
- **Strict Multi-Tenant Isolation**: Stalled jobs from User A recover in background threads when any user accesses the Space, without leaking credentials or outputs to other sessions.

---

## 💻 Installation & Local Quickstart

### Prerequisites
- Python 3.10, 3.11, or 3.12
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
| `/api/orca/engine/convolute` | `POST` | Computes Gaussian spectral convolution for UV-Vis transitions. |
| `/api/orca/engine/thermochemistry` | `POST` | Evaluates reaction energetics ($\Delta G^\circ, \Delta H^\circ, \Delta S^\circ, K_{eq}$) from multiple species. |
| `/api/orca/engine/samples` | `GET` | Lists and loads built-in calculation outputs for instant demonstration. |
| `/api/orca/engine/analyze-job` | `POST` | Fetches and analyzes calculation outputs directly from a Kaggle job. |
| `/api/orca/coords` | `POST` | Resolves compound names and SMILES to 3D Cartesian coordinates via PubChem. |
| `/api/orca/generate` | `POST` | Generates validated ORCA 6 calculation input blocks. |
| `/api/reaction` | `POST` | Processes and renders 2D reaction diagrams with stoichiometry and MDL RXN file. |
| `/api/orca/upload-archive` | `POST` | Uploads and validates a Linux ORCA `.tar.xz` distribution archive with tar-slip protection. |
| `/api/orca/archive/<id>` | `GET` / `DELETE` | Serves or deletes a stored ORCA `.tar.xz` archive package. |
| `/api/kaggle/submit` | `POST` | Submits a long-running ORCA calculation to Kaggle supporting Dataset, Link, or Uploaded Archive. |
| `/api/kaggle/sync` | `POST` | Synchronizes active and completed calculations for the signed-in user. |
| `/api/kaggle/download` | `GET` | Downloads the complete verified scientific result bundle (`.zip`) containing Molden, `.out`, `.xyz`, and scratch files. |
| `/api/kaggle/credentials` | `GET` / `POST` / `DELETE` | Retrieves metadata, securely stores AES-256-GCM ciphertext, or purges user credentials. |


---

### 5. ORCA Source Provisioning & Result Downloads
- **Three Supported ORCA Sources**:
  1. **Kaggle Dataset**: Supply a private dataset identifier (e.g. `username/dataset` or dummy example `jon534/orca6`) or full URL.
  2. **Google Drive / Direct Link**: Provide a direct HTTP/Drive download link to your licensed Linux ORCA archive.
  3. **Direct Upload (.tar.xz)**: Upload your Linux ORCA `.tar.xz` package directly. Validated with tar-slip / path-traversal protection and stored locally on the device for fast reuse across calculation jobs.
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
  version = {1.0.0},
  url     = {https://github.com/abd19990044-commits/chemistry-web-lab},
  note    = {Academic and Non-Commercial Research Platform}
}
```
