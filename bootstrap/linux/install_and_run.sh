#!/usr/bin/env bash
set -euo pipefail

echo "==============================================================="
echo " Chemistry Lab Local Agent (Linux)"
echo "==============================================================="

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

# 2. Discover supported Python interpreter
if [ ! -f "$VENV_DIR/bin/python" ]; then
    SUPPORTED_PY=""
    for cand in python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c "import sys; v=sys.version_info; sys.exit(0 if (3, 11) <= v < (3, 14) else 1)" >/dev/null 2>&1; then
                SUPPORTED_PY="$cand"
                break
            fi
        fi
    done

    if [ -z "$SUPPORTED_PY" ]; then
        echo "==============================================================="
        echo " [ERROR] Supported Python version (>=3.11, <3.14) not found."
        echo " Chemistry Lab requires Python 3.11, 3.12, or 3.13."
        echo "==============================================================="
        read -r -p "Install Python 3.12 using system package manager? [y/N]: " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            if command -v apt >/dev/null 2>&1; then
                sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip || sudo apt install -y python3 python3-venv python3-pip
            elif command -v dnf >/dev/null 2>&1; then
                sudo dnf install -y python3.12 python3-pip || sudo dnf install -y python3 python3-pip
            elif command -v pacman >/dev/null 2>&1; then
                sudo pacman -S --noconfirm python python-pip
            else
                echo "Could not identify supported package manager (apt/dnf/pacman). Please install Python 3.11-3.13 manually."
                exit 1
            fi
            for cand in python3.13 python3.12 python3.11 python3; do
                if command -v "$cand" >/dev/null 2>&1; then
                    if "$cand" -c "import sys; v=sys.version_info; sys.exit(0 if (3, 11) <= v < (3, 14) else 1)" >/dev/null 2>&1; then
                        SUPPORTED_PY="$cand"
                        break
                    fi
                fi
            done
            if [ -z "$SUPPORTED_PY" ]; then
                echo "Failed to locate supported Python after installation. Exiting."
                exit 1
            fi
        else
            echo "Python >=3.11, <3.14 is required. Exiting."
            exit 1
        fi
    fi

    echo "[*] Creating virtual environment in $VENV_DIR using $SUPPORTED_PY..."
    "$SUPPORTED_PY" -m venv "$VENV_DIR"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REQS_FILE="$SCRIPT_DIR/requirements-local-agent.txt"
if [ ! -f "$REQS_FILE" ]; then
    REQS_FILE="$SCRIPT_DIR/../../requirements-local-agent.txt"
fi

if [ -d "$SCRIPT_DIR/local_agent" ]; then
    export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
elif [ -d "$SCRIPT_DIR/../../local_agent" ]; then
    export PYTHONPATH="$(cd "$SCRIPT_DIR/../.." && pwd):${PYTHONPATH:-}"
fi

echo "[*] Installing dependencies..."
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
if [ -f "$REQS_FILE" ]; then
    "$VENV_DIR/bin/python" -m pip install --quiet -r "$REQS_FILE"
fi

echo "[*] Starting Chemistry Lab Local Agent..."
"$VENV_DIR/bin/python" -m local_agent.agent start
AGENT_EXIT_CODE=$?

echo ""
echo "==============================================================="
echo " Chemistry Lab Local Agent has stopped (exit code $AGENT_EXIT_CODE)."
echo "==============================================================="
if [ -t 0 ]; then
    read -r -p "Press [Enter] to exit..." || true
fi
exit $AGENT_EXIT_CODE

