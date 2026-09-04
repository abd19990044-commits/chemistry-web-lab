# -*- coding: utf-8 -*-
"""Local ORCA Execution Service for Chemistry Lab.

Provides:
- Strict explicit 3-path user configuration (orca_executable, input_directory, output_directory).
- Readiness gates with structured machine-readable error codes.
- Zero automatic executable selection / silent path fabrication.
- Fail-closed cross-platform cross-process file locking (CrossProcessFileLock) with strict timeout cleanup.
- OS-level process identity signature capture and verification (PID reuse protection).
- Concurrency limiting: strictly serial (max 1) for primary Local ORCA backend.
- Scientific output validation with OrcaParser and job_to_dict.
- Safe subprocess execution with shell=False and argument arrays.
- Cancellation and restart durability with process identity verification.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
import sys
from typing import Any, Dict, List, Optional, Tuple

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ORCA_ENGINE_SRC = os.path.join(_BASE_DIR, "orca_engine", "src")
if os.path.isdir(_ORCA_ENGINE_SRC) and _ORCA_ENGINE_SRC not in sys.path:
    sys.path.insert(0, _ORCA_ENGINE_SRC)

from orca_engine.parser import OrcaParser
from orca_engine.reporting import job_to_dict

DEFAULT_LOCAL_ORCA_TIMEOUT_S = 3600
DEFAULT_LOCAL_ORCA_CONCURRENCY = 1
DEFAULT_KAGGLE_CONCURRENCY = 5

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "orca_executable": "",
    "input_directory": "",
    "output_directory": "",
    "working_directory": "",
    "timeout_seconds": DEFAULT_LOCAL_ORCA_TIMEOUT_S,
    "concurrency": DEFAULT_LOCAL_ORCA_CONCURRENCY,
    "execution_backend": "local",
    "kaggle_enabled": False,
}

_THREAD_LOCK = threading.Lock()
_ACTIVE_LOCAL_PROCESS_MEM: Dict[str, Any] = {}


class LockAcquisitionError(TimeoutError):
    """Raised when cross-process file lock cannot be acquired within timeout."""
    pass


class CrossProcessFileLock:
    """Cross-platform cross-process file lock combining OS file locking with thread locking.
    
    Guarantees fail-closed semantics:
    - Never leaves thread lock held or file descriptor open on timeout.
    - __enter__ raises LockAcquisitionError if lock cannot be acquired.
    - acquire() returns False or raises, cleaning up internal resources completely on timeout.
    """

    def __init__(self, lock_path: str, timeout: float = 30.0):
        self.lock_path = lock_path
        self.timeout = timeout
        self._thread_lock = threading.Lock()
        self._fd: Optional[Any] = None
        self._is_locked: bool = False

    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        eff_timeout = self.timeout if timeout is None else timeout
        acquired_thread = self._thread_lock.acquire(blocking=blocking, timeout=eff_timeout if blocking else 0)
        if not acquired_thread:
            return False

        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.lock_path)), exist_ok=True)
            self._fd = open(self.lock_path, "a+b")
        except Exception:
            self._thread_lock.release()
            self._fd = None
            return False

        start = time.time()
        while True:
            try:
                if platform.system() == "Windows":
                    import msvcrt
                    self._fd.seek(0)
                    if os.path.getsize(self.lock_path) == 0:
                        self._fd.write(b"0")
                        self._fd.flush()
                    self._fd.seek(0)
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._is_locked = True
                return True
            except (BlockingIOError, OSError, IOError):
                if not blocking or (time.time() - start >= eff_timeout):
                    # TIMEOUT or non-blocking: clean up everything immediately!
                    try:
                        self._fd.close()
                    except Exception:
                        pass
                    self._fd = None
                    self._is_locked = False
                    self._thread_lock.release()
                    return False
                time.sleep(0.01)

    def release(self) -> None:
        if self._fd is not None:
            try:
                if platform.system() == "Windows":
                    import msvcrt
                    self._fd.seek(0)
                    try:
                        msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
                self._fd.close()
            except Exception:
                pass
            self._fd = None
        self._is_locked = False
        if self._thread_lock.locked():
            try:
                self._thread_lock.release()
            except RuntimeError:
                pass

    def __enter__(self) -> CrossProcessFileLock:
        if not self.acquire():
            raise LockAcquisitionError(f"Failed to acquire lock on {self.lock_path} within {self.timeout}s")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


def _get_process_lock(state_dir: Optional[str] = None, timeout: float = 30.0) -> CrossProcessFileLock:
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    return CrossProcessFileLock(os.path.join(base, ".local_orca_process.lock"), timeout=timeout)


def _get_settings_lock(state_dir: Optional[str] = None, timeout: float = 30.0) -> CrossProcessFileLock:
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    return CrossProcessFileLock(os.path.join(base, ".local_orca_settings.lock"), timeout=timeout)


def detect_orca_candidates() -> List[str]:
    """Helper for optional UI discovery assistance only.
    
    This function is NEVER called automatically to select an executable.
    The user must explicitly select and save their preferred binary path.
    """
    candidates: List[str] = []
    env_paths = [os.environ.get("ORCA_BIN"), os.environ.get("ORCA_PATH")]
    for p in env_paths:
        if p and os.path.isfile(p) and p not in candidates:
            candidates.append(os.path.abspath(p))

    is_win = platform.system() == "Windows"
    standard_candidates = [
        r"C:\Orca\orca.exe" if is_win else "/usr/bin/orca",
        r"C:\Program Files\ORCA\orca.exe" if is_win else "/usr/local/bin/orca",
        r"C:\orca_6_0_0\orca.exe" if is_win else "/opt/orca/orca",
    ]
    for c in standard_candidates:
        if os.path.isfile(c) and c not in candidates:
            candidates.append(os.path.abspath(c))
    return candidates


def get_local_orca_settings(state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Loads persisted local ORCA configuration without automatic path fabrication."""
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    cfg_file = os.path.join(base, "local_orca_settings.json")
    try:
        with _get_settings_lock(base):
            if os.path.isfile(cfg_file):
                try:
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        res = dict(_DEFAULT_SETTINGS)
                        res.update(data)
                        if "orca_bin" in res and not res.get("orca_executable"):
                            res["orca_executable"] = res["orca_bin"]
                        return res
                except Exception:
                    pass
            return dict(_DEFAULT_SETTINGS)
    except (LockAcquisitionError, TimeoutError):
        if os.path.isfile(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    res = dict(_DEFAULT_SETTINGS)
                    res.update(data)
                    return res
            except Exception:
                pass
        return dict(_DEFAULT_SETTINGS)


def save_local_orca_settings(settings: Dict[str, Any], state_dir: Optional[str] = None) -> Dict[str, Any]:
    """Saves local ORCA settings atomically with fail-closed lock semantics."""
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    os.makedirs(base, exist_ok=True)
    cfg_file = os.path.join(base, "local_orca_settings.json")
    with _get_settings_lock(base):
        current = dict(_DEFAULT_SETTINGS)
        if os.path.isfile(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    current.update(json.load(f))
            except Exception:
                pass
        current.update(settings)
        # Ensure hard limit of 1 on local concurrency
        current["concurrency"] = DEFAULT_LOCAL_ORCA_CONCURRENCY
        tmp = cfg_file + ".tmp." + uuid.uuid4().hex
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        os.replace(tmp, cfg_file)
        return current



FORBIDDEN_EXECUTABLE_NAMES = {
    "cmd.exe", "cmd", "powershell.exe", "powershell", "pwsh.exe", "pwsh",
    "bash", "sh", "zsh", "csh", "tcsh", "python.exe", "python", "python3",
    "curl", "curl.exe", "wget", "wget.exe", "nc", "netcat", "rm", "del",
}


def is_safe_executable_path(exe_path: str) -> Tuple[bool, Optional[str]]:
    """Validates that the executable is an actual ORCA executable and not a dangerous system utility."""
    if not exe_path or not exe_path.strip():
        return False, "Executable path is empty."

    norm_path = exe_path.strip().replace("/", "\\")
    parts = norm_path.split("\\")
    if ".." in parts or any(p == ".." for p in parts):
        return False, "Path traversal ('..') detected in executable path."

    base_name = os.path.basename(norm_path).lower()

    if base_name in FORBIDDEN_EXECUTABLE_NAMES:
        return False, f"Executable '{base_name}' is a forbidden shell/system utility."

    if not (base_name.startswith("orca") or base_name == "orca"):
        return False, f"Executable '{base_name}' does not match expected ORCA binary naming pattern."

    return True, None

def validate_local_orca_config(
    settings: Optional[Dict[str, Any]] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Validates local ORCA executable and required directories."""
    cfg = settings if settings is not None else get_local_orca_settings(state_dir)

    exe_path = str(cfg.get("orca_executable") or "").strip()
    inp_dir = str(cfg.get("input_directory") or "").strip()
    out_dir = str(cfg.get("output_directory") or "").strip()
    work_dir = str(cfg.get("working_directory") or "").strip()

    if not exe_path and not inp_dir and not out_dir:
        return {
            "configured": False,
            "ready": False,
            "status": "NOT_CONFIGURED",
            "errors": ["LOCAL_ORCA_NOT_CONFIGURED: executable and directories are not configured"],
            "details": {
                "executable_valid": False,
                "input_dir_valid": False,
                "output_dir_valid": False,
                "working_dir_valid": True,
            },
        }

    errors: List[str] = []
    details = {
        "executable_valid": False,
        "input_dir_valid": False,
        "output_dir_valid": False,
        "working_dir_valid": True,
    }

    # Validate Executable
    if not exe_path:
        errors.append("ORCA_EXECUTABLE_NOT_FOUND: executable path is empty")
    elif not os.path.isfile(exe_path):
        errors.append("ORCA_EXECUTABLE_NOT_FOUND: executable file does not exist")
    else:
        is_win = platform.system() == "Windows"
        if not is_win and not os.access(exe_path, os.X_OK):
            errors.append("ORCA_EXECUTABLE_NOT_LAUNCHABLE: file is not executable")
        else:
            details["executable_valid"] = True

    # Validate Input Directory
    if not inp_dir:
        errors.append("INPUT_DIRECTORY_INVALID: input directory path is empty")
    elif not os.path.isdir(inp_dir):
        errors.append("INPUT_DIRECTORY_INVALID: directory does not exist")
    elif not os.access(inp_dir, os.W_OK):
        errors.append("INPUT_DIRECTORY_INVALID: directory is not writable")
    else:
        details["input_dir_valid"] = True

    # Validate Output Directory
    if not out_dir:
        errors.append("OUTPUT_DIRECTORY_INVALID: output directory path is empty")
    elif not os.path.isdir(out_dir):
        errors.append("OUTPUT_DIRECTORY_INVALID: directory does not exist")
    elif not os.access(out_dir, os.W_OK):
        errors.append("OUTPUT_DIRECTORY_INVALID: directory is not writable")
    else:
        details["output_dir_valid"] = True

    # Validate optional Working Directory
    if work_dir:
        if not os.path.isdir(work_dir):
            errors.append("WORKING_DIRECTORY_INVALID: directory does not exist")
            details["working_dir_valid"] = False
        elif not os.access(work_dir, os.W_OK):
            errors.append("WORKING_DIRECTORY_INVALID: directory is not writable")
            details["working_dir_valid"] = False

    is_ready = bool(
        details["executable_valid"]
        and details["input_dir_valid"]
        and details["output_dir_valid"]
        and details["working_dir_valid"]
    )
    status = "READY" if is_ready else "INVALID"

    return {
        "configured": True,
        "ready": is_ready,
        "status": status,
        "errors": errors,
        "details": details,
    }


# =========================================================================
# OS-Level Process Identity & PID Reuse Verification
# =========================================================================
def get_live_process_identity(pid: Optional[int]) -> Dict[str, Any]:
    """Queries real OS process creation timestamp and executable path.
    
    Returns:
        Dict with "status" in ("RUNNING", "NOT_RUNNING", "UNKNOWN"),
        "pid", "creation_time", "executable".
    """
    if not pid or int(pid) <= 0:
        return {"status": "NOT_RUNNING", "pid": pid, "creation_time": None, "executable": None}
    
    pid = int(pid)
    sys_name = platform.system()

    if sys_name == "Windows":
        try:
            import ctypes
            import ctypes.wintypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            PROCESS_QUERY_INFORMATION = 0x0400

            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
            if not h_proc:
                h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h_proc:
                err = kernel32.GetLastError()
                return {"status": "NOT_RUNNING" if err == 87 else "UNKNOWN", "pid": pid, "creation_time": None, "executable": None}

            try:
                exit_code = ctypes.wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                    return {"status": "UNKNOWN", "pid": pid, "creation_time": None, "executable": None}
                if exit_code.value != 259:  # 259 = STILL_ACTIVE
                    return {"status": "NOT_RUNNING", "pid": pid, "creation_time": None, "executable": None}

                class FILETIME(ctypes.Structure):
                    _fields_ = [("dwLowDateTime", ctypes.wintypes.DWORD), ("dwHighDateTime", ctypes.wintypes.DWORD)]

                creation_time = FILETIME()
                exit_time = FILETIME()
                kernel_time = FILETIME()
                user_time = FILETIME()
                creation_ticks = None
                if kernel32.GetProcessTimes(h_proc, ctypes.byref(creation_time), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    creation_ticks = (creation_time.dwHighDateTime << 32) | creation_time.dwLowDateTime

                exe_path = None
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.wintypes.DWORD(1024)
                if hasattr(kernel32, "QueryFullProcessImageNameW"):
                    if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                        exe_path = buf.value

                return {
                    "status": "RUNNING",
                    "pid": pid,
                    "creation_time": creation_ticks,
                    "executable": exe_path,
                }
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            return {"status": "UNKNOWN", "pid": pid, "creation_time": None, "executable": None}

    elif sys_name == "Linux":
        proc_dir = f"/proc/{pid}"
        if not os.path.exists(proc_dir):
            return {"status": "NOT_RUNNING", "pid": pid, "creation_time": None, "executable": None}
        try:
            stat_path = f"/proc/{pid}/stat"
            creation_ticks = None
            if os.path.isfile(stat_path):
                with open(stat_path, "r", encoding="utf-8", errors="replace") as f:
                    stat_content = f.read()
                rparen = stat_content.rfind(")")
                if rparen != -1:
                    fields = stat_content[rparen + 1:].split()
                    if len(fields) >= 20:
                        creation_ticks = int(fields[19])

            exe_path = None
            exe_link = f"/proc/{pid}/exe"
            if os.path.islink(exe_link):
                try:
                    exe_path = os.readlink(exe_link)
                except Exception:
                    pass

            return {
                "status": "RUNNING",
                "pid": pid,
                "creation_time": creation_ticks,
                "executable": exe_path,
            }
        except Exception:
            return {"status": "UNKNOWN", "pid": pid, "creation_time": None, "executable": None}

    else:
        # macOS / other POSIX
        try:
            os.kill(pid, 0)
            return {"status": "UNKNOWN", "pid": pid, "creation_time": None, "executable": None}
        except OSError:
            return {"status": "NOT_RUNNING", "pid": pid, "creation_time": None, "executable": None}


def verify_process_identity(persisted: Optional[Dict[str, Any]]) -> str:
    """Verifies whether the live OS process on persisted PID matches the persisted identity.
    
    Returns:
        "MATCH": Live process matches persisted creation_time and/or executable.
        "MISMATCH": Live process exists, but creation_time or executable contradicts persisted record (PID reuse!).
        "NOT_RUNNING": Process does not exist or has exited.
        "UNKNOWN": Cannot reliably verify due to permissions or platform limitations.
    """
    if not persisted:
        return "NOT_RUNNING"

    state = persisted.get("state")
    start_t = persisted.get("start_time", 0)
    pid = persisted.get("pid")

    # If in STARTING state before Popen, within recent window
    if state == "STARTING" and (not pid or int(pid) <= 0):
        if time.time() - start_t < 15.0:
            return "MATCH"
        return "NOT_RUNNING"

    if not pid:
        return "NOT_RUNNING"

    live = get_live_process_identity(int(pid))
    live_status = live.get("status")

    if live_status == "NOT_RUNNING":
        return "NOT_RUNNING"
    if live_status == "UNKNOWN":
        return "UNKNOWN"

    persisted_creation = persisted.get("creation_time")
    live_creation = live.get("creation_time")
    persisted_exe = persisted.get("executable")
    live_exe = live.get("executable")

    # 1. Compare creation timestamp if available on both
    if persisted_creation is not None and live_creation is not None:
        if persisted_creation == live_creation:
            return "MATCH"
        else:
            return "MISMATCH"

    # 2. Compare executable paths if available on both
    if persisted_exe and live_exe:
        def norm(p: str) -> str:
            return os.path.normcase(os.path.normpath(str(p)))
        p_n = norm(persisted_exe)
        l_n = norm(live_exe)
        if p_n == l_n or p_n in l_n or l_n in p_n:
            return "MATCH"
        else:
            return "MISMATCH"

    # If neither could be compared
    if persisted_creation is None and not persisted_exe:
        return "UNKNOWN"

    return "MATCH"


def is_process_alive_and_matched(info: Dict[str, Any]) -> bool:
    """Returns True ONLY when process identity is verified as MATCH."""
    return verify_process_identity(info) == "MATCH"


def _read_active_process_file(state_dir: str) -> Optional[Dict[str, Any]]:
    active_path = os.path.join(state_dir, "local_orca_active.json")
    if os.path.isfile(active_path):
        try:
            with open(active_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _write_active_process_file(data: Dict[str, Any], state_dir: str) -> None:
    os.makedirs(state_dir, exist_ok=True)
    active_path = os.path.join(state_dir, "local_orca_active.json")
    tmp = active_path + ".tmp." + uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, active_path)


def _clear_active_process_file(state_dir: str) -> None:
    active_path = os.path.join(state_dir, "local_orca_active.json")
    try:
        if os.path.isfile(active_path):
            os.remove(active_path)
    except Exception:
        pass


def cancel_local_orca_job(job_id: str, attempt_id: Optional[str] = None, state_dir: Optional[str] = None) -> bool:
    """Terminates an actively executing local ORCA process ONLY if process identity matches."""
    global _ACTIVE_LOCAL_PROCESS_MEM
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    with _get_process_lock(base):
        active = _read_active_process_file(base) or dict(_ACTIVE_LOCAL_PROCESS_MEM)
        if not active:
            return False
        active_job = active.get("job_id")
        active_attempt = active.get("attempt_id")
        if active_job == job_id and (attempt_id is None or active_attempt == attempt_id):
            id_status = verify_process_identity(active)
            
            if id_status == "MATCH":
                # Safe to terminate!
                proc: Optional[subprocess.Popen] = _ACTIVE_LOCAL_PROCESS_MEM.get("proc")
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        time.sleep(0.3)
                        if proc.poll() is None:
                            proc.kill()
                            proc.wait(timeout=2.0)
                    except Exception:
                        pass
                elif active.get("pid"):
                    pid = active.get("pid")
                    try:
                        if platform.system() == "Windows":
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                        else:
                            os.kill(int(pid), 9)
                    except Exception:
                        pass
                _ACTIVE_LOCAL_PROCESS_MEM.clear()
                _clear_active_process_file(base)
                return True

            elif id_status == "MISMATCH":
                # DO NOT KILL FOREIGN PROCESS!
                # PID was reused by an unrelated OS process.
                _clear_active_process_file(base)
                _ACTIVE_LOCAL_PROCESS_MEM.clear()
                return False

            elif id_status == "UNKNOWN":
                # FAIL SAFE: Do not terminate unknown process
                return False

            elif id_status == "NOT_RUNNING":
                # Process already exited
                _clear_active_process_file(base)
                _ACTIVE_LOCAL_PROCESS_MEM.clear()
                return True
    return False


def get_active_local_process_info(state_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Returns metadata for the currently running local ORCA process, if any."""
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    with _get_process_lock(base):
        active = _read_active_process_file(base) or dict(_ACTIVE_LOCAL_PROCESS_MEM)
        if active:
            id_status = verify_process_identity(active)
            if id_status == "MATCH":
                return active
            elif id_status in ("NOT_RUNNING", "MISMATCH"):
                _clear_active_process_file(base)
                _ACTIVE_LOCAL_PROCESS_MEM.clear()
    return None


def execute_local_orca_job(
    job_id: str,
    input_text: str,
    attempt_id: Optional[str] = None,
    stage_kind: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    state_dir: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Safely executes an ORCA calculation locally with strict concurrency, process identity, and scientific validation.
    
    Returns a structured dictionary with process_ok, parse_ok, and scientific_ok.
    """
    global _ACTIVE_LOCAL_PROCESS_MEM
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    cfg = settings if settings is not None else get_local_orca_settings(base)
    validation = validate_local_orca_config(cfg, base)

    if not validation["ready"]:
        error_code = validation["errors"][0] if validation["errors"] else "LOCAL_ORCA_NOT_CONFIGURED"
        return {
            "ok": False,
            "process_ok": False,
            "parse_ok": False,
            "scientific_ok": False,
            "error": "; ".join(validation["errors"]) or "Local ORCA is not configured or invalid.",
            "error_code": error_code,
            "exit_code": -1,
            "output_text": "",
            "parsed": {},
            "stage_result": None,
            "duration_seconds": 0.0,
            "input_file": "",
            "output_file": "",
            "artifacts": [],
        }

    safe_job_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", job_id)
    attempt_id = attempt_id or uuid.uuid4().hex

    # Enforce strictly 1 active local process atomically across threads and processes
    try:
        with _get_process_lock(base):
            active_disk = _read_active_process_file(base)
            if active_disk:
                id_status = verify_process_identity(active_disk)
                if id_status == "MATCH":
                    return {
                        "ok": False,
                        "process_ok": False,
                        "parse_ok": False,
                        "scientific_ok": False,
                        "error": "Local ORCA concurrency limit (1) exceeded. A calculation is already running.",
                        "error_code": "LOCAL_CONCURRENCY_LIMIT_EXCEEDED",
                        "exit_code": -1,
                        "output_text": "",
                        "parsed": {},
                        "stage_result": None,
                        "duration_seconds": 0.0,
                        "input_file": "",
                        "output_file": "",
                        "artifacts": [],
                    }
                elif id_status in ("NOT_RUNNING", "MISMATCH"):
                    _clear_active_process_file(base)
                    _ACTIVE_LOCAL_PROCESS_MEM.clear()

            if _ACTIVE_LOCAL_PROCESS_MEM.get("running"):
                if _ACTIVE_LOCAL_PROCESS_MEM.get("proc") and _ACTIVE_LOCAL_PROCESS_MEM["proc"].poll() is not None:
                    _ACTIVE_LOCAL_PROCESS_MEM.clear()
                else:
                    return {
                        "ok": False,
                        "process_ok": False,
                        "parse_ok": False,
                        "scientific_ok": False,
                        "error": "Local ORCA concurrency limit (1) exceeded. A calculation is already running.",
                        "error_code": "LOCAL_CONCURRENCY_LIMIT_EXCEEDED",
                        "exit_code": -1,
                        "output_text": "",
                        "parsed": {},
                        "stage_result": None,
                        "duration_seconds": 0.0,
                        "input_file": "",
                        "output_file": "",
                        "artifacts": [],
                    }

            # Atomically mark slot as STARTING before Popen
            reserve_data = {
                "running": True,
                "state": "STARTING",
                "job_id": job_id,
                "attempt_id": attempt_id,
                "backend": "local",
                "start_time": time.time(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "executable": cfg["orca_executable"],
                "metadata": metadata or {},
            }
            _write_active_process_file(reserve_data, base)
            _ACTIVE_LOCAL_PROCESS_MEM = dict(reserve_data)
    except (LockAcquisitionError, TimeoutError):
        return {
            "ok": False,
            "process_ok": False,
            "parse_ok": False,
            "scientific_ok": False,
            "error": "Failed to acquire local process lock (lock timeout).",
            "error_code": "LOCAL_LOCK_TIMEOUT",
            "exit_code": -1,
            "output_text": "",
            "parsed": {},
            "stage_result": None,
            "duration_seconds": 0.0,
            "input_file": "",
            "output_file": "",
            "artifacts": [],
        }

    input_dir = pathlib.Path(cfg["input_directory"])
    output_base = pathlib.Path(cfg["output_directory"])
    job_output_dir = output_base / safe_job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Canonical input file written to user-selected input directory
    canonical_inp_file = input_dir / f"{safe_job_id}.inp"
    canonical_inp_file.write_text(input_text, encoding="utf-8")

    # 2. Execution working directory (optional scratch or job output dir)
    work_dir = (pathlib.Path(cfg["working_directory"]) / safe_job_id) if cfg.get("working_directory") else job_output_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    work_inp = work_dir / f"{safe_job_id}.inp"
    if work_inp != canonical_inp_file:
        shutil.copy2(canonical_inp_file, work_inp)
    work_out = work_dir / f"{safe_job_id}.out"

    orca_executable = cfg["orca_executable"]
    timeout = timeout_seconds or cfg.get("timeout_seconds", DEFAULT_LOCAL_ORCA_TIMEOUT_S)
    start_time = time.time()

    cmd = [str(orca_executable), f"{safe_job_id}.inp"]
    if str(orca_executable).endswith(".py"):
        cmd = [sys.executable, str(orca_executable), f"{safe_job_id}.inp"]

    proc = None
    try:
        with open(work_out, "w", encoding="utf-8", errors="replace") as out_f:
            proc = subprocess.Popen(
                cmd,
                cwd=str(work_dir),
                stdout=out_f,
                stderr=subprocess.STDOUT,
                shell=False,
                text=True,
            )
            with _get_process_lock(base):
                live_identity = get_live_process_identity(proc.pid)
                proc_info = {
                    "running": True,
                    "state": "RUNNING",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "backend": "local",
                    "pid": proc.pid,
                    "creation_time": live_identity.get("creation_time"),
                    "start_time": start_time,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time)),
                    "input_path": str(canonical_inp_file),
                    "output_path": str(job_output_dir / f"{safe_job_id}.out"),
                    "working_directory": str(work_dir),
                    "executable": live_identity.get("executable") or str(orca_executable),
                    "metadata": metadata or {},
                }
                _write_active_process_file(proc_info, base)
                _ACTIVE_LOCAL_PROCESS_MEM.update(proc_info)
                _ACTIVE_LOCAL_PROCESS_MEM["proc"] = proc

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                with _get_process_lock(base):
                    _clear_active_process_file(base)
                    _ACTIVE_LOCAL_PROCESS_MEM.clear()
                return {
                    "ok": False,
                    "process_ok": False,
                    "parse_ok": False,
                    "scientific_ok": False,
                    "error": f"Local ORCA calculation timed out after {timeout} seconds.",
                    "error_code": "TIMEOUT",
                    "exit_code": -1,
                    "output_text": work_out.read_text(encoding="utf-8", errors="replace") if work_out.exists() else "",
                    "parsed": {},
                    "stage_result": None,
                    "duration_seconds": time.time() - start_time,
                    "input_file": str(canonical_inp_file),
                    "output_file": str(job_output_dir / f"{safe_job_id}.out"),
                    "artifacts": [],
                }
    except Exception as exc:
        with _get_process_lock(base):
            _clear_active_process_file(base)
            _ACTIVE_LOCAL_PROCESS_MEM.clear()
        return {
            "ok": False,
            "process_ok": False,
            "parse_ok": False,
            "scientific_ok": False,
            "error": f"Failed to execute local ORCA process: {exc}",
            "error_code": "EXECUTION_ERROR",
            "exit_code": -1,
            "output_text": "",
            "parsed": {},
            "stage_result": None,
            "duration_seconds": time.time() - start_time,
            "input_file": str(canonical_inp_file),
            "output_file": str(job_output_dir / f"{safe_job_id}.out"),
            "artifacts": [],
        }
    finally:
        with _get_process_lock(base):
            _clear_active_process_file(base)
            _ACTIVE_LOCAL_PROCESS_MEM.clear()

    duration = time.time() - start_time
    exit_code = proc.returncode if proc else -1
    process_ok = (exit_code == 0)

    # 3. Synchronize calculation artifacts from work_dir to canonical job_output_dir
    artifacts: List[str] = []
    for item in work_dir.iterdir():
        if item.is_file():
            dest = job_output_dir / item.name
            if item != dest:
                shutil.copy2(item, dest)
            artifacts.append(item.name)

    canonical_out_file = job_output_dir / f"{safe_job_id}.out"
    output_text = canonical_out_file.read_text(encoding="utf-8", errors="replace") if canonical_out_file.is_file() else ""

    if not process_ok:
        return {
            "ok": False,
            "process_ok": False,
            "parse_ok": False,
            "scientific_ok": False,
            "error": f"ORCA process exited with non-zero return code: {exit_code}",
            "error_code": "PROCESS_FAILED",
            "exit_code": exit_code,
            "output_text": output_text,
            "parsed": {},
            "stage_result": None,
            "duration_seconds": duration,
            "input_file": str(canonical_inp_file),
            "output_file": str(canonical_out_file),
            "artifacts": artifacts,
        }

    # 4. Authoritative Scientific Parsing & Validation
    parser = OrcaParser(output_text)
    parsed_jobs = parser.parse()

    parsed: Dict[str, Any] = {}
    if parsed_jobs:
        last_job = parsed_jobs[-1]
        parsed = job_to_dict(1, last_job)
        parsed["energy_hartree"] = parsed.get("electronic_energy_hartree") or parsed.get("scf_energy_hartree")
        parsed["final_energy_hartree"] = parsed["energy_hartree"]
        parsed["frequencies"] = getattr(last_job, "vibrational_frequencies_cm", []) or []
        parsed["coordinates"] = getattr(last_job, "coords", [])
        parsed["converged"] = getattr(last_job, "converged", False)

    parse_ok = bool(parsed.get("energy_hartree") is not None or parsed.get("final_energy_hartree") is not None or parsed.get("frequencies"))
    
    # Stage-level validation check
    scientific_ok = False
    stage_result = None

    if stage_kind in ("OPT", "OPT_FREQ"):
        has_opt = bool(parsed.get("converged") or "OPTIMIZATION RUN DONE" in output_text or parsed.get("coordinates"))
        has_energy = bool(parsed.get("energy_hartree") is not None or parsed.get("final_energy_hartree") is not None)
        scientific_ok = has_opt and has_energy
    elif stage_kind in ("FREQ", "NUMFREQ"):
        has_freq = bool(parsed.get("frequencies") or "VIBRATIONAL FREQUENCIES" in output_text)
        has_energy = bool(parsed.get("energy_hartree") is not None or parsed.get("final_energy_hartree") is not None)
        scientific_ok = has_freq and has_energy
    elif stage_kind == "SP":
        scientific_ok = bool(parsed.get("energy_hartree") is not None or parsed.get("final_energy_hartree") is not None)
    else:
        # Default generic scientific validation
        scientific_ok = parse_ok and ("ORCA TERMINATED NORMALLY" in output_text or parsed.get("energy_hartree") is not None)

    ok = process_ok and parse_ok and scientific_ok

    error_msg = ""
    error_code = None
    if not parse_ok:
        error_msg = "Failed to parse required chemical observables from ORCA output."
        error_code = "PARSE_FAILED"
    elif not scientific_ok:
        error_msg = f"Scientific validation failed for stage kind {stage_kind}: output is incomplete or malformed."
        error_code = "SCIENTIFIC_VALIDATION_FAILED"

    return {
        "ok": ok,
        "process_ok": process_ok,
        "parse_ok": parse_ok,
        "scientific_ok": scientific_ok,
        "error": error_msg,
        "error_code": error_code,
        "exit_code": exit_code,
        "output_text": output_text,
        "parsed": parsed,
        "stage_result": parsed,
        "duration_seconds": duration,
        "input_file": str(canonical_inp_file),
        "output_file": str(canonical_out_file),
        "artifacts": artifacts,
    }
