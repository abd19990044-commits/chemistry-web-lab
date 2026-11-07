"""Verify the installed Kaggle CLI without credentials or a remote operation."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile


def check_cli() -> str:
    env = os.environ.copy()
    for name in ("KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_TOKEN", "KAGGLE_TOKEN", "PYTHONPATH"):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="kaggle-cli-probe-") as directory:
        env["KAGGLE_CONFIG_DIR"] = directory
        result = subprocess.run(
            [sys.executable, "-c", "from kaggle.cli import main; main()", "--version"],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    version = re.search(r"(?m)^Kaggle CLI [^\r\n]+$", result.stdout)
    if result.returncode != 0 or version is None:
        detail = (result.stderr or result.stdout or "no CLI output").strip()[-3000:]
        raise RuntimeError(f"Installed Kaggle CLI cannot start (exit {result.returncode}): {detail}")
    return version.group(0)


if __name__ == "__main__":
    print("Kaggle startup verified: " + check_cli())
