#!/usr/bin/env bash
set -euo pipefail

echo "==============================================================="
echo " Chemistry Lab Local Agent (macOS)"
echo "==============================================================="

AGENT_DIR="$HOME/Library/Application Support/ChemistryLabAgent"
VENV_DIR="$AGENT_DIR/venv"
mkdir -p "$AGENT_DIR"

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
    for cand in python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
        if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
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
        if command -v brew >/dev/null 2>&1; then
            read -r -p "Install Python 3.12 via Homebrew (brew install python@3.12)? [y/N]: " confirm
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                brew install python@3.12
                for cand in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3.12; do
                    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
                        if "$cand" -c "import sys; v=sys.version_info; sys.exit(0 if (3, 11) <= v < (3, 14) else 1)" >/dev/null 2>&1; then
                            SUPPORTED_PY="$cand"
                            break
                        fi
                    fi
                done
            fi
        fi
        if [ -z "$SUPPORTED_PY" ]; then
            echo "Python >=3.11, <3.14 is required. Please install Python from python.org or Homebrew and re-run."
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

