# -*- coding: utf-8 -*-
"""Chemistry Lab Local Companion Agent - Standalone CLI Daemon.

CRITICAL INVARIANTS:
1. Single active process per installation enforced via ProcessLock.
2. Every start generates a NEW ephemeral Connection API (CLA_...).
3. Prominently prints the new API in English and Arabic immediately on startup.
4. Does NOT persist any authentication secrets to disk, keyring, or database.
5. In-memory runtime session secret obtained via /runtime/finalize upon user claim.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests
import psutil

from local_agent.config import AgentConfig
from local_agent.protocol import MessageType, create_envelope
from local_agent.security import (
    load_or_create_installation_credentials,
    AgentAlreadyRunningError,
    ProcessLock,
    generate_process_tokens,
    hash_token_verifier,
    load_or_create_installation_id,
    redact_token,
    remove_pairing_txt,
    write_pairing_txt,
)

LOGGER = logging.getLogger("ChemistryLabAgent")
_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _is_safe_server_url(raw_url: str) -> bool:
    """Allow HTTPS remotely and plain HTTP only on the local loopback."""
    try:
        parsed = urlsplit((raw_url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    return True


def _safe_job_id(value: Any) -> str:
    candidate = str(value or "")
    if not _SAFE_JOB_ID_RE.fullmatch(candidate):
        raise ValueError("UNSAFE_JOB_ID")
    return candidate


def _safe_job_basename(value: Any) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "calculation")).strip("_-")
    if not candidate:
        candidate = "calculation"
    return candidate[:96]


def _contained_child(root: Path, *parts: str) -> Path:
    root_resolved = root.resolve()
    target = root_resolved.joinpath(*parts).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("JOB_PATH_ESCAPE") from exc
    return target


def _read_bounded_text(path: Path, *, full_limit: int = 10_000_000, edge: int = 1_000_000) -> str:
    """Read a result without allocating an arbitrarily large ORCA log."""
    size = path.stat().st_size
    with open(path, "rb") as source:
        if size <= full_limit:
            data = source.read(full_limit + 1)
            return data.decode("utf-8", errors="replace")
        head = source.read(edge)
        source.seek(max(0, size - edge))
        tail = source.read(edge)
    return (head.decode("utf-8", errors="replace")
            + "\n... [LOG TRUNCATED BY LOCAL AGENT] ...\n"
            + tail.decode("utf-8", errors="replace"))


def print_startup_banner(
    tokens: List[str],
    device_name: str,
    installation_id: str,
    agent_session_id: str,
) -> None:
    primary_api = tokens[0]

    banner = f"""
===============================================================
        CHEMISTRY LAB LOCAL COMPANION AGENT
===============================================================

Device Name:     {device_name}
Installation ID: {installation_id}
Session ID:      {agent_session_id}

This Agent process generated a NEW Connection API.

CONNECTION API:

{primary_api}
"""
    if len(tokens) > 1:
        banner += "\nADDITIONAL CONNECTION APIS (Total %d):\n" % len(tokens)
        for i, t in enumerate(tokens[1:], start=2):
            banner += f"  #{i}: {t}\n"

    banner += f"""
COPY THIS API AND ENTER IT IN THE CHEMISTRY LAB WEBSITE:

    Local & HPC Execution
        ->
    Connect Computer
        ->
    Enter Connection API

IMPORTANT:
This API is valid only while THIS Agent process is running.
If you close or restart this program:
    this API becomes invalid
and a NEW API will be generated.

===============================================================
"""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        print(banner, flush=True)
    except Exception:
        safe_banner = banner.encode(getattr(sys.stdout, "encoding", "utf-8") or "utf-8", errors="replace").decode(getattr(sys.stdout, "encoding", "utf-8") or "utf-8", errors="replace")
        print(safe_banner, flush=True)


class LocalCompanionAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.completion_outbox_dir = self.data_dir / "completion_outbox"
        self.completion_outbox_dir.mkdir(parents=True, exist_ok=True)
        self.process_registry_path = self.data_dir / "active_orca_process.json"
        self.pairing_txt_path = self.data_dir / "chemistry_lab_pairing.txt"
        self.process_lock = ProcessLock(self.data_dir)

        if not self.process_lock.acquire():
            raise AgentAlreadyRunningError(
                f"AGENT_ALREADY_RUNNING: Another Chemistry Lab Agent process is already active for {self.data_dir}"
            )

        self.installation_id, self.installation_secret = load_or_create_installation_credentials(self.data_dir)
        self.agent_session_id, self.tokens = generate_process_tokens(config.token_count)
        self.primary_api = self.tokens[0]

        self.runtime_session_secret: Optional[str] = None
        self.owner_id: Optional[str] = None
        self.paired = False
        self.server_initialized = False
        self.running = False
        self._process_guard = threading.Lock()
        self._current_process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._recovery_checked = False

    def init_server_session(self) -> bool:
        verifiers = [hash_token_verifier(t) for t in self.tokens]
        payload = {
            "installation_id": self.installation_id,
            "installation_secret": self.installation_secret,
            "agent_session_id": self.agent_session_id,
            "device_name": self.config.device_name,
            "token_verifiers": verifiers,
            "platform": platform.system().lower(),
            "backend_kind": self.config.backend_kind,
            "scheduler_type": self.config.scheduler_type,
            "agent_version": "1.0.4",
            "protocol_version": 1,
            "capabilities": {
                "cpu_count": os.cpu_count() or 1,
                "platform": platform.platform(),
            },
        }

        # Installation proof and one-time connection tokens are sensitive.
        # Sending them to a list of guessed localhost ports allowed any local
        # process listening first to impersonate the web app and steal them.
        # Probe only the explicitly configured endpoint; users choose another
        # port with --server / CHEMISTRY_LAB_SERVER_URL.
        base = (self.config.server_url or "").strip().rstrip("/")
        if not _is_safe_server_url(base):
            LOGGER.error(
                "Refusing Local Agent handshake: configure HTTPS for remote servers "
                "or HTTP on localhost/127.0.0.1 only."
            )
            return False
        url = f"{base}/api/v1/local-agent/runtime/init"
        try:
            resp = requests.post(url, json=payload, timeout=2)
            if resp.status_code == 200 and resp.json().get("ok"):
                self.server_initialized = True
                return True
        except Exception as exc:
            LOGGER.debug("Local Agent handshake failed: %s", type(exc).__name__)

        LOGGER.warning("Could not establish initial handshake with Chemistry Lab server (will retry in background).")
        return False

    def poll_finalize_claim(self) -> bool:
        """Polls server finalize endpoint to retrieve in-memory runtime secret once claimed."""
        url = f"{self.config.server_url.rstrip('/')}/api/v1/local-agent/runtime/finalize"
        payload = {
            "agent_session_id": self.agent_session_id,
            "connection_api": self.primary_api,
        }
        try:
            resp = requests.post(url, json=payload, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("runtime_session_secret"):
                    self.runtime_session_secret = data["runtime_session_secret"]
                    self.owner_id = data.get("owner_id")
                    self.paired = True
                    # CRITICAL: Pairing TXT must disappear immediately after successful pairing
                    remove_pairing_txt(self.pairing_txt_path)
                    LOGGER.info("Pairing finalized successfully. Removed temporary pairing TXT.")
                    return True
        except Exception:
            pass
        return False

    def find_orca_executable(self) -> Optional[str]:
        if self.config.orca_executable and os.path.isfile(self.config.orca_executable):
            return self.config.orca_executable

        cand = shutil.which("orca") or shutil.which("orca.exe")
        if cand:
            return cand

        common = [
            r"C:\Orca\orca.exe",
            r"C:\Program Files\ORCA\orca.exe",
            r"C:\orca_6_0_0\orca.exe",
            r"C:\orca_5_0_4\orca.exe",
            "/usr/bin/orca",
            "/usr/local/bin/orca",
            "/opt/orca/orca",
        ]
        for c in common:
            if os.path.isfile(c):
                return c
        return None

    def poll_and_execute_jobs(self) -> None:
        if not self.runtime_session_secret:
            return
        if not self._recovery_checked:
            self._recovery_checked = True
            if self._recover_registered_process():
                return
        self._flush_completion_outbox()
        url = f"{self.config.server_url.rstrip('/')}/api/v1/local-agent/jobs/poll"
        headers = {
            "X-Agent-Session-Id": self.agent_session_id,
            "X-Runtime-Secret": self.runtime_session_secret,
        }
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                job = data.get("job")
                if job:
                    self._run_job(job)
        except Exception as e:
            LOGGER.debug(f"Job poll exception: {e}")

    def _completion_path(self, job_id: str) -> Path:
        safe = _safe_job_id(job_id)
        return self.completion_outbox_dir / f"{safe}.json"

    def _set_current_process(self, process: Optional[subprocess.Popen]) -> None:
        with self._process_guard:
            self._current_process = process

    @staticmethod
    def _command_fingerprint(command: List[str], workspace: Path) -> str:
        material = json.dumps(
            {"command": [str(part) for part in command], "workspace": str(workspace.resolve())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _write_process_registry(self, record: Dict[str, Any]) -> None:
        """Atomically persist the single active ORCA identity with private permissions."""
        path = self.process_registry_path
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(tmp), flags, 0o600)
        try:
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _load_process_registry(self) -> Optional[Dict[str, Any]]:
        if not self.process_registry_path.exists():
            return None
        if self.process_registry_path.is_symlink() or not self.process_registry_path.is_file():
            raise RuntimeError("Unsafe Local Agent process registry path")
        data = json.loads(self.process_registry_path.read_text(encoding="utf-8"))
        required = {"job_id", "pid", "process_start_time", "command", "command_fingerprint", "workspace", "output_path"}
        if not required.issubset(data):
            raise RuntimeError("Corrupt Local Agent process registry")
        return data

    def _register_process_with_server(self, record: Dict[str, Any], *, recovering: bool) -> bool:
        if not self.runtime_session_secret:
            return False
        url = f"{self.config.server_url.rstrip('/')}/api/v1/local-agent/jobs/{record['job_id']}/process"
        headers = {
            "X-Agent-Session-Id": self.agent_session_id,
            "X-Runtime-Secret": self.runtime_session_secret,
        }
        payload = {
            "pid": record["pid"],
            "process_start_time": record["process_start_time"],
            "command_fingerprint": record["command_fingerprint"],
            "workspace": record["workspace"],
            "recovering": recovering,
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=8)
            return response.status_code == 200 and bool(response.json().get("ok"))
        except Exception as exc:
            LOGGER.warning("Could not persist process identity for %s: %s", record["job_id"], exc)
            return False

    @staticmethod
    def _verified_process(record: Dict[str, Any]) -> Optional[psutil.Process]:
        """Return a process only when PID, create-time, command, and cwd match."""
        try:
            process = psutil.Process(int(record["pid"]))
            try:
                if process.status() == psutil.STATUS_ZOMBIE:
                    return process
            except (psutil.Error, OSError):
                pass
            if abs(float(process.create_time()) - float(record["process_start_time"])) > 1.0:
                return None
            command = [str(part) for part in process.cmdline()]
            workspace = Path(str(record["workspace"]))
            if LocalCompanionAgent._command_fingerprint(command, workspace) != record["command_fingerprint"]:
                return None
            try:
                if Path(process.cwd()).resolve() != workspace.resolve():
                    return None
            except (psutil.AccessDenied, OSError):
                pass
            return process
        except (psutil.Error, OSError, ValueError, TypeError):
            return None

    def _tail_progress(self, record: Dict[str, Any], *, force: bool = False) -> bool:
        path = Path(str(record["output_path"]))
        if not path.is_file():
            return False
        offset = max(0, int(record.get("last_read_offset", 0)))
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if not force and size == offset:
            return True
        with open(path, "rb") as stream:
            stream.seek(offset)
            chunk = stream.read(min(65536, max(0, size - offset)))
            record["last_read_offset"] = stream.tell()
        self._write_process_registry(record)
        if chunk and self.runtime_session_secret:
            url = f"{self.config.server_url.rstrip('/')}/api/v1/local-agent/jobs/{record['job_id']}/progress"
            headers = {
                "X-Agent-Session-Id": self.agent_session_id,
                "X-Runtime-Secret": self.runtime_session_secret,
            }
            try:
                response = requests.post(
                    url,
                    json={"stdout_chunk": chunk.decode("utf-8", errors="replace")},
                    headers=headers,
                    timeout=5,
                )
                if response.status_code == 200 and response.json().get("cancel_requested"):
                    process = self._verified_process(record)
                    if process is not None:
                        self._terminate_process_tree(process)
            except Exception as exc:
                LOGGER.debug("Recovered progress heartbeat failed for %s: %s", record["job_id"], exc)
        return True

    def _finalize_registered_process(self, record: Dict[str, Any]) -> None:
        output_path = Path(str(record["output_path"]))
        output = _read_bounded_text(output_path) if output_path.is_file() else ""
        normal = "ORCA TERMINATED NORMALLY" in output.upper()
        payload = {
            "_job_id": record["job_id"],
            "exit_code": 0 if normal else 1,
            "stdout_tail": output[-50000:],
            "parsed_results": {},
            "error_message": None if normal else "Recovered ORCA process ended without the normal termination marker.",
            "output_text": output,
            "xyz_structure": "",
        }
        self._queue_completion(record["job_id"], payload)
        self.process_registry_path.unlink(missing_ok=True)
        self._flush_completion_outbox()

    def _recover_registered_process(self) -> bool:
        """Rebind and monitor an ORCA process detached by a prior Agent crash."""
        try:
            record = self._load_process_registry()
        except Exception as exc:
            LOGGER.error("Local ORCA recovery is blocked by invalid registry data: %s", exc)
            return False
        if not record:
            return False
        verified = self._verified_process(record)
        if verified is not None and (not verified.is_running() or verified.status() == psutil.STATUS_ZOMBIE):
            self._tail_progress(record, force=True)
            self._finalize_registered_process(record)
            return True
        if verified is None and psutil.pid_exists(int(record["pid"])):
            # Check if PID is actually a dead or zombie process on Linux
            try:
                probe = psutil.Process(int(record["pid"]))
                if probe.status() == psutil.STATUS_ZOMBIE:
                    verified = probe
            except (psutil.Error, OSError):
                pass
        if verified is not None and (not verified.is_running() or verified.status() == psutil.STATUS_ZOMBIE):
            self._tail_progress(record, force=True)
            self._finalize_registered_process(record)
            return True
        if verified is None and psutil.pid_exists(int(record["pid"])):
            # A live PID with mismatching create-time/command may be PID reuse
            # or tampering. Never adopt it and never infer that ORCA ended.
            LOGGER.error(
                "Refusing recovery of job %s: PID exists but identity proof does not match",
                record["job_id"],
            )
            self._recovery_checked = False
            return True
        if not self._register_process_with_server(record, recovering=True):
            LOGGER.warning("Server has not authorized recovery of job %s; leaving registry intact", record["job_id"])
            self._recovery_checked = False
            return True
        LOGGER.info("Recovered Local Agent job %s after companion restart", record["job_id"])
        interval = max(1, int(self.config.heartbeat_interval_seconds or 15))
        while self.running and not self._stop_event.is_set():
            process = self._verified_process(record)
            self._tail_progress(record)
            if process is None and psutil.pid_exists(int(record["pid"])):
                try:
                    probe = psutil.Process(int(record["pid"]))
                    if probe.status() == psutil.STATUS_ZOMBIE:
                        process = probe
                except (psutil.Error, OSError):
                    pass
            if process is None or not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                self._tail_progress(record, force=True)
                self._finalize_registered_process(record)
                return True
            if process is None and psutil.pid_exists(int(record["pid"])):
                LOGGER.error("Recovered PID identity changed for job %s; manual recovery is required", record["job_id"])
                return True
            time.sleep(interval)
        return True

    def _terminate_process_tree(self, process: Any, *, grace_seconds: float = 8.0) -> None:
        """Terminate ORCA and MPI descendants without invoking a shell."""
        if isinstance(process, subprocess.Popen) and process.poll() is not None:
            return
        pid = int(process.pid)
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=max(2.0, grace_seconds),
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
                process.wait(timeout=grace_seconds)
            except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
                os.killpg(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except OSError:
                    pass

    def _queue_completion(self, job_id: str, payload: Dict[str, Any]) -> None:
        """Durably spool the terminal result before attempting HTTP delivery."""
        path = self._completion_path(job_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _deliver_completion(self, job_id: str, payload: Dict[str, Any]) -> bool:
        if not self.runtime_session_secret:
            return False
        url = f"{self.config.server_url.rstrip('/')}/api/v1/local-agent/jobs/{job_id}/complete"
        headers = {
            "X-Agent-Session-Id": self.agent_session_id,
            "X-Runtime-Secret": self.runtime_session_secret,
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            if response.status_code == 200 and response.json().get("ok"):
                self._completion_path(job_id).unlink(missing_ok=True)
                return True
            LOGGER.warning("Completion delivery for %s returned HTTP %s; keeping outbox entry",
                           job_id, response.status_code)
        except Exception as exc:
            LOGGER.warning("Completion delivery for %s failed; keeping outbox entry: %s",
                           job_id, exc)
        return False

    def _flush_completion_outbox(self) -> None:
        for path in sorted(self.completion_outbox_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                job_id = str(payload.pop("_job_id"))
            except (OSError, ValueError, KeyError, TypeError):
                LOGGER.error("Ignoring malformed completion outbox entry %s", path)
                continue
            if self._deliver_completion(job_id, payload):
                continue

    def _run_job(self, job: Dict[str, Any]) -> None:
        try:
            job_id = _safe_job_id(job.get("job_id"))
            job_name = _safe_job_basename(job.get("job_name", "calculation"))
        except ValueError as exc:
            LOGGER.error("Rejected unsafe job identity: %s", exc)
            return
        input_text = job.get("input_text", "")
        LOGGER.info(f"Received job {job_id} ({job_name}). Preparing local execution...")

        orca_bin = self.find_orca_executable()
        runs_root = Path(self.config.orca_working_dir or self.config.data_dir) / "runs"
        scratch_dir = _contained_child(runs_root, job_id)
        scratch_dir.mkdir(parents=True, exist_ok=True)

        inp_path = _contained_child(scratch_dir, f"{job_name}.inp")
        out_path = _contained_child(scratch_dir, f"{job_name}.out")
        inp_path.write_text(input_text, encoding="utf-8")

        headers = {
            "X-Agent-Session-Id": self.agent_session_id,
            "X-Runtime-Secret": self.runtime_session_secret,
            "Content-Type": "application/json",
        }

        if not orca_bin:
            err_msg = "ORCA executable not found on host machine. Please configure orca_executable or ensure 'orca' is in PATH."
            LOGGER.error(err_msg)
            payload = {"_job_id": job_id, "exit_code": 1,
                       "stdout_tail": err_msg, "error_message": err_msg}
            self._queue_completion(job_id, payload)
            self._flush_completion_outbox()
            return

        LOGGER.info(f"Executing ORCA binary: {orca_bin} {inp_path.name} in {scratch_dir}")
        exit_code = 1
        termination_reason: Optional[str] = None
        proc: Optional[subprocess.Popen] = None
        try:
            # stdout goes directly to a durable file.  A PIPE would be owned
            # by this Agent process and could kill/block ORCA when the Agent
            # crashes, defeating process adoption after restart.
            with open(out_path, "wb", buffering=0) as out_f:
                process_kwargs: Dict[str, Any] = {}
                if os.name == "nt":
                    process_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                else:
                    process_kwargs["start_new_session"] = True
                command = [orca_bin, inp_path.name]
                proc = subprocess.Popen(
                    command,
                    cwd=str(scratch_dir),
                    stdout=out_f,
                    stderr=subprocess.STDOUT,
                    **process_kwargs,
                )
                self._set_current_process(proc)
                try:
                    observed = psutil.Process(proc.pid)
                    observed_command = [str(part) for part in observed.cmdline()]
                    process_start_time = float(observed.create_time())
                except psutil.Error as exc:
                    self._terminate_process_tree(proc)
                    raise RuntimeError("Could not establish durable ORCA process identity") from exc
                process_record: Dict[str, Any] = {
                    "job_id": job_id,
                    "job_name": job_name,
                    "pid": proc.pid,
                    "process_start_time": process_start_time,
                    "command": observed_command,
                    "command_fingerprint": self._command_fingerprint(observed_command, scratch_dir),
                    "workspace": str(scratch_dir.resolve()),
                    "input_path": str(inp_path.resolve()),
                    "output_path": str(out_path.resolve()),
                    "agent_session_id": self.agent_session_id,
                    "started_at": time.time(),
                    "last_read_offset": 0,
                }
                self._write_process_registry(process_record)
                self._register_process_with_server(process_record, recovering=False)
                last_progress_t = time.time()
                started_t = last_progress_t
                timeout_s = max(1, int(job.get("timeout_seconds") or self.config.max_job_runtime_seconds))
                quiet_timeout_s = int(job.get("quiet_timeout_seconds") or getattr(self.config, "quiet_timeout_seconds", 0) or 0)
                last_output_t = started_t
                last_size = 0

                while True:
                    now_t = time.time()
                    try:
                        current_size = out_path.stat().st_size
                    except OSError:
                        current_size = last_size
                    if current_size != last_size:
                        last_output_t = now_t
                        last_size = current_size
                    if now_t - started_t > timeout_s:
                        termination_reason = "Agent job exceeded configured runtime limit (%s seconds)." % timeout_s
                        self._terminate_process_tree(proc)
                    elif quiet_timeout_s > 0 and now_t - last_output_t > quiet_timeout_s:
                        termination_reason = "Agent job terminated after %s seconds of silence (no output produced)." % quiet_timeout_s
                        self._terminate_process_tree(proc)
                    elif self._stop_event.is_set():
                        LOGGER.info("Agent stopping; detached ORCA job %s remains registered for recovery", job_id)
                        return

                    interval = max(1, int(self.config.heartbeat_interval_seconds or 15))
                    if current_size - int(process_record.get("last_read_offset", 0)) >= 65536 or now_t - last_progress_t >= interval:
                        last_progress_t = now_t
                        self._tail_progress(process_record, force=True)
                        self._register_process_with_server(process_record, recovering=False)

                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)

                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._terminate_process_tree(proc)
                    proc.wait(timeout=5)
                exit_code = proc.returncode
                self._tail_progress(process_record, force=True)

        except Exception as exc:
            if proc is not None and proc.poll() is None:
                LOGGER.exception("Agent monitor failed for %s; ORCA remains detached for recovery", job_id)
                return
            LOGGER.exception("Error running ORCA process for %s", job_id)
            exit_code = 1
            termination_reason = f"Execution error: {exc}"
        finally:
            self._set_current_process(None)

        parsed_results = {}
        out_content = ""
        xyz_content = ""
        if out_path.is_file():
            try:
                out_content = _read_bounded_text(out_path)
                for line in out_content.splitlines():
                    if "FINAL SINGLE POINT ENERGY" in line:
                        parsed_results["final_single_point_energy"] = line.split()[-1]
                    elif "Total Run Time" in line:
                        parsed_results["total_run_time"] = line.strip()
            except Exception as exc:
                LOGGER.warning("Could not parse ORCA output for %s: %s", job_id, exc)

        try:
            xyz_files = list(scratch_dir.glob("*.xyz"))
            if xyz_files:
                xyz_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                xyz_content = xyz_files[0].read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

        tail = out_content[-50000:]
        LOGGER.info(f"Job {job_id} finished with exit code {exit_code}.")
        payload = {
            "_job_id": job_id,
            "exit_code": exit_code,
            "stdout_tail": tail,
            "parsed_results": parsed_results,
            "error_message": termination_reason or (None if exit_code == 0 else f"Process exited with code {exit_code}"),
            "output_text": out_content,
            "xyz_structure": xyz_content,
        }
        self._queue_completion(job_id, payload)
        self.process_registry_path.unlink(missing_ok=True)
        self._flush_completion_outbox()

    def start(self) -> None:
        self.running = True
        self._stop_event.clear()
        atexit.register(self.cleanup)

        write_pairing_txt(
            file_path=self.pairing_txt_path,
            device_name=self.config.device_name,
            installation_id=self.installation_id,
            agent_session_id=self.agent_session_id,
            connection_api=self.primary_api,
        )

        self.init_server_session()

        print_startup_banner(
            tokens=self.tokens,
            device_name=self.config.device_name,
            installation_id=self.installation_id,
            agent_session_id=self.agent_session_id,
        )

        LOGGER.info("Agent process running. Waiting for website pairing...")
        retry_init_counter = 0
        try:
            while self.running:
                if not self.server_initialized:
                    retry_init_counter += 1
                    if retry_init_counter >= 3:
                        retry_init_counter = 0
                        self.init_server_session()
                elif not self.paired:
                    self.poll_finalize_claim()
                else:
                    self.poll_and_execute_jobs()
                time.sleep(1)
        except KeyboardInterrupt:
            LOGGER.info("Agent process stopping...")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        self.running = False
        self._stop_event.set()
        with self._process_guard:
            process = self._current_process
        if process is not None:
            LOGGER.info("Agent shutdown leaves registered ORCA PID %s running for recovery", process.pid)
        remove_pairing_txt(self.pairing_txt_path)
        self.process_lock.release()
        try:
            url = f"{self.config.server_url.rstrip('/')}/api/v1/local-agent/runtime/end?agent_session_id={self.agent_session_id}"
            headers = {"Authorization": f"Bearer {self.runtime_session_secret}"} if self.runtime_session_secret else {}
            requests.post(url, headers=headers, timeout=3)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Chemistry Lab Local Companion Agent")
    parser.add_argument("command", nargs="?", default="start", help="Agent action: start (default)")
    parser.add_argument("--server", default=None, help="Chemistry Lab server URL (e.g. http://localhost:7860)")
    parser.add_argument("--data-dir", default=None, help="Custom data directory")
    parser.add_argument("--device-name", default=None, help="Device display name")
    parser.add_argument("--token-count", "--tokens", dest="token_count", type=int, default=1, help="Number of ephemeral Connection APIs (1..10)")
    parser.add_argument("--backend", default="local", choices=["local", "hpc"], help="Execution mode")
    parser.add_argument("--scheduler", default=None, choices=["slurm", "pbs", "lsf"], help="HPC scheduler type")
    args = parser.parse_args()

    cfg = AgentConfig.load()
    if args.data_dir:
        cfg.data_dir = args.data_dir
    if args.device_name:
        cfg.device_name = args.device_name
    if args.token_count:
        cfg.token_count = args.token_count
    if args.backend:
        cfg.backend_kind = args.backend
    if args.scheduler:
        cfg.scheduler_type = args.scheduler

    if args.server:
        cfg.server_url = args.server
    elif sys.stdin and sys.stdin.isatty():
        default_srv = cfg.server_url or "http://localhost:7860"
        print("===============================================================")
        print(" Chemistry Lab Server Connection Setup")
        print("===============================================================")
        print("Enter the Chemistry Lab website URL (e.g. http://localhost:7860 or your remote space URL):")
        try:
            user_url = input(f"Website URL [{default_srv}]: ").strip()
            if user_url:
                if not user_url.startswith(("http://", "https://")):
                    user_url = "http://" + user_url
                cfg.server_url = user_url
            else:
                cfg.server_url = default_srv
        except (EOFError, KeyboardInterrupt):
            cfg.server_url = default_srv
        print(f"[*] Target Website URL: {cfg.server_url}\n")
        try:
            cfg.save()
        except Exception:
            pass

    try:
        agent = LocalCompanionAgent(cfg)
        agent.start()
    except AgentAlreadyRunningError as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
