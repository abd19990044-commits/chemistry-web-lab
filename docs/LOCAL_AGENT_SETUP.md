# 💻 Chemistry Lab: Local Companion Agent Complete User Guide

The **Chemistry Lab Local Companion Agent** is the primary, high-performance execution engine for the Chemistry Lab platform. It connects your web browser securely to your local machine, workstation, or institutional high-performance computing (HPC) cluster, enabling direct ORCA calculations with native performance, zero data exfiltration, and no cloud runtime restrictions.

---

## 🚀 Why Local Execution is the Primary Choice

| Feature | 💻 Local Companion Agent (Primary) | ☁️ Kaggle Cloud (Optional Fallback) |
| :--- | :--- | :--- |
| **Execution Speed** | **Native Hardware (AVX-512, OpenMPI, Fast NVMe)** | Emulated Virtual CPU / Shared Cloud |
| **Job Runtime Limit** | **Unlimited** (run hours, days, or weeks) | 12 hours max per session |
| **Data Privacy** | **100% On-Premise** (wavefunctions & files stay local) | Uploaded to cloud notebook scratch |
| **Resource Control** | **Exact Core & Memory Limits** (%pal, %maxcore) | Fixed cloud container allocations |
| **HPC & Cluster Support** | **Native Slurm, PBS Pro, and IBM LSF** | Not supported |
| **Dependencies** | Uses your own licensed ORCA binaries | Requires downloading ORCA archive |

---

## 📥 1. Downloading the Package

From the Chemistry Lab web interface, click **📥 Download Local Runner** in the top navigation bar or the homepage hero section. Choose the archive matching your operating system:

1. **Windows (x64)**: `ChemistryLabAgent-Windows.zip` (includes batch scripts & PowerShell bootstrappers).
2. **Linux Workstation (x64)**: `ChemistryLabAgent-Linux.zip` (includes Bash launcher and daemon scripts).
3. **macOS (Apple Silicon M-Series & Intel)**: `ChemistryLabAgent-macOS.zip` (includes double-clickable `.command` launcher).
4. **HPC Supercomputing Cluster**: `ChemistryLabAgent-HPC.zip` (includes Slurm, PBS Pro, and LSF scheduler adapters).

---

## ⚙️ 2. Step-by-Step Installation & Startup

### A. Windows Setup (Windows 10 / 11 / Server)
1. **Extract Archive**: Extract the downloaded ZIP file to a dedicated folder, for example `C:\ChemistryLabAgent`.
2. **Launch Agent**:
   - Double-click **`install_and_run.bat`**
   - *Or open PowerShell and run:*
     ```powershell
     cd C:\ChemistryLabAgent
     .\bootstrap_windows.ps1
     ```
3. **Automatic Environment Creation**: The script automatically checks for Python, creates an isolated virtual environment (`.venv`), installs lightweight requirements, and boots the agent.

---

### B. Linux Workstation Setup (Ubuntu, Debian, Fedora, RHEL, Arch)
1. **Extract Archive**:
   ```bash
   unzip ChemistryLabAgent-Linux.zip -d ~/ChemistryLabAgent
   cd ~/ChemistryLabAgent
   ```
2. **Launch Agent**:
   ```bash
   bash install_and_run.sh
   ```
3. **Requirements**: Python 3.11, 3.12, or 3.13 (`>=3.11, <3.14`) and standard build tools.

---

### C. macOS Setup (Apple Silicon M1/M2/M3/M4 & Intel x86_64)
1. **Extract Archive**: Double-click the ZIP file or unzip via Terminal.
2. **Launch Agent**:
   - Double-click **`install_and_run.command`**
   - *Or in Terminal:*
     ```bash
     cd ~/Downloads/ChemistryLabAgent-macOS
     bash install_and_run.sh
     ```

---

### D. Institutional HPC Supercomputing Cluster (Slurm, PBS Pro, LSF)
The agent runs on a login/head node and interfaces directly with the cluster resource manager:
```bash
unzip ChemistryLabAgent-HPC.zip -d ~/ChemistryLabAgent
cd ~/ChemistryLabAgent
bash install_hpc_agent.sh --scheduler slurm  # Or: --scheduler pbs, --scheduler lsf
```
*The agent generates scheduler submission scripts, monitors job queues, and streams live progress back to your browser session.*

---

## 🔑 3. Connecting to the Web Interface

### Process-Ephemeral Pairing Model
Every time you launch the Companion Agent, it creates a **brand-new cryptographically random Connection API** (`CLA_...` with >= 256 bits of entropy) printed prominently in your terminal:

```text
======================================================================
  CHEMISTRY LAB COMPANION AGENT - READY FOR PAIRING
======================================================================
  Ephemeral Connection API : CLA_e8f2910ab3c491da0182...
  Client Host Machine      : DESKTOP-LAB-ORCA (Windows 11 x64)
  CPU Cores Available      : 16 Cores
  Usable RAM               : 32.0 GB
======================================================================
```

### Pairing Steps:
1. **Copy the Connection API** from the terminal.
2. In the Chemistry Lab browser window, open the **Connect Computer** modal (click the device icon in the topbar).
3. **Paste the Connection API** and optionally provide a friendly name (e.g., `Lab-Workstation-RTX`).
4. Click **Connect Device**.
5. Once connected, your computer appears with a **🟢 Online (Local Agent)** badge and is ready to execute quantum calculations.

> [!NOTE]
> **Ephemeral Security Invariant**: The Connection API is never saved to disk. If you stop or restart the Agent process, the old token is permanently revoked, and a fresh token is generated upon next startup.

---

## 🔬 4. Running Calculations with Local ORCA

1. **Input Generation**: Use the **ORCA 6 Input Generator Wizard** in the website to configure your calculation (DFT, Basis Set, Solvation, Dispersion, TD-DFT, or Freq).
2. **Select Device**: In the Calculation Launcher or Reaction Workflow, choose your connected local machine from the **Target Device** dropdown.
3. **Hardware Allocation**:
   - **CPU Cores (`%pal`)**: Set the desired number of threads/cores.
   - **Memory (`%maxcore`)**: Set memory per core (the agent automatically applies an 80% safety margin to protect your OS).
   - **Disk Allocation (`%scf MaxDisk`)**: Optional scratch disk ceiling.
4. **Click Run**: The calculation begins immediately. Output is parsed in real time and automatically visualized in 3D and spectroscopy canvases upon completion.

---

## 🛠️ 5. Troubleshooting & FAQ

### Q1: The agent says "ORCA executable not found on PATH"
**Solution**: Provide the exact absolute path to your ORCA binary in the agent configuration or add ORCA's directory to your system `PATH` (e.g. `C:\orca_6_0_0` on Windows or `/opt/orca` on Linux).

### Q2: What happens if my internet connection temporarily drops?
**Solution**: The Companion Agent features **Resilient Reconnection**. Temporary network disruptions do not abort your calculation or invalidate pairing; the agent buffers output and resumes streaming as soon as connection is restored.

### Q3: Can I run multiple calculations simultaneously?
**Solution**: Yes! The agent maintains a local job queue and enforces concurrency limits based on your available hardware cores to prevent system thrashing.
