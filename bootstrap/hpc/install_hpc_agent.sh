#!/usr/bin/env bash
set -euo pipefail

echo "==============================================================="
echo " Chemistry Lab HPC / Supercomputer Agent Setup"
echo "==============================================================="

# INVARIANT: Supercomputer login nodes DO NOT permit root/sudo.
# Never execute sudo, apt, yum, or dnf on an HPC cluster.

AGENT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/chemistry-lab-agent"
VENV_DIR="$AGENT_DIR/venv"
mkdir -p "$AGENT_DIR"
chmod 700 "$AGENT_DIR"

# 1. Check existing venv
if [ -f "$VENV_DIR/bin/python" ]; then
    if "$VENV_DIR/bin/python" -c "import sys; v=sys.version_info; sys.exit(0 if (3, 11) <= v < (3, 14) else 1)" >/dev/null 2>&1; then
        echo "[*] Using existing virtual environment in $VENV_DIR"
    else
        echo "[!] Existing virtual environment Python does not satisfy >=3.11, <3.14. Recreating..."
        rm -rf "$VENV_DIR"
    fi
fi

# 2. Discover supported Python interpreter in user environment / modules / conda
if [ ! -f "$VENV_DIR/bin/python" ]; then
    SUPPORTED_PY=""

    # Check conda / venv / module python on PATH first
    for cand in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c "import sys; v=sys.version_info; sys.exit(0 if (3, 11) <= v < (3, 14) else 1)" >/dev/null 2>&1; then
                SUPPORTED_PY="$cand"
                break
            fi
        fi
    done

    # If not found, attempt environment module detection if 'module' is available
    if [ -z "$SUPPORTED_PY" ] && command -v module >/dev/null 2>&1; then
        for mod in python/3.12 python/3.11 python/3.13 python3/3.12 python3/3.11; do
            if module is-avail "$mod" >/dev/null 2>&1 || module avail "$mod" >/dev/null 2>&1; then
                echo "[*] Loading module: $mod..."
                module load "$mod" 2>/dev/null || true
                for cand in python3.13 python3.12 python3.11 python3; do
                    if command -v "$cand" >/dev/null 2>&1; then
                        if "$cand" -c "import sys; v=sys.version_info; sys.exit(0 if (3, 11) <= v < (3, 14) else 1)" >/dev/null 2>&1; then
                            SUPPORTED_PY="$cand"
                            break 2
                        fi
                    fi
                done
            fi
        done
    fi

    if [ -z "$SUPPORTED_PY" ]; then
        echo "==============================================================="
        echo " [ERROR] No supported Python interpreter (>=3.11, <3.14) found."
        echo " Chemistry Lab requires Python 3.11, 3.12, or 3.13."
        echo ""
        echo " Supercomputer / HPC environments require user-space Python:"
        echo "   1. Environment module: module load python/3.12 (or 3.11/3.13)"
        echo "   2. Conda / Mamba:      conda activate <python_312_env>"
        echo "   3. Spack:              spack load python@3.12"
        echo ""
        echo " Note: Root/sudo package managers (apt, yum, dnf) are prohibited"
        echo " on HPC login nodes."
        echo "==============================================================="
        exit 1
    fi

    echo "[*] Creating dedicated virtual environment in $VENV_DIR using $SUPPORTED_PY..."
    "$SUPPORTED_PY" -m venv "$VENV_DIR"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REQS_FILE="$SCRIPT_DIR/requirements-local-agent-hpc.txt"
if [ ! -f "$REQS_FILE" ]; then
    REQS_FILE="$SCRIPT_DIR/../../requirements-local-agent-hpc.txt"
fi

if [ -d "$SCRIPT_DIR/local_agent" ]; then
    export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
elif [ -d "$SCRIPT_DIR/../../local_agent" ]; then
    export PYTHONPATH="$(cd "$SCRIPT_DIR/../.." && pwd):${PYTHONPATH:-}"
fi

echo "[*] Installing HPC dependencies..."
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
if [ -f "$REQS_FILE" ]; then
    "$VENV_DIR/bin/python" -m pip install --quiet -r "$REQS_FILE"
fi

echo "[*] Starting HPC Agent on Login Node..."
"$VENV_DIR/bin/python" -m local_agent.agent start
AGENT_EXIT_CODE=$?

echo ""
echo "==============================================================="
echo " Chemistry Lab HPC Agent has stopped (exit code $AGENT_EXIT_CODE)."
echo "==============================================================="
if [ -t 0 ]; then
    read -r -p "Press [Enter] to exit..." || true
fi
exit $AGENT_EXIT_CODE

