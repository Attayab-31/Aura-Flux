"""Finite Kaggle notebook batch supervisor using the supported Kaggle CLI."""
import os
import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from config import (KAGGLE_API_TOKEN, KAGGLE_BATCH_ACCELERATOR, KAGGLE_BATCH_AUTOSTART,
                    KAGGLE_BATCH_DEBOUNCE_SECONDS, KAGGLE_BATCH_IDLE_EXIT_SECONDS,
                    KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS, KAGGLE_BATCH_MAX_RUNTIME_SECONDS,
                    KAGGLE_BATCH_MAX_STARTS_PER_DAY, KAGGLE_BATCH_POLL_SECONDS,
                    KAGGLE_BATCH_TIMEOUT_SECONDS, KAGGLE_KERNEL_ID, KAGGLE_KERNEL_PATH,
                    KAGGLE_KEY, KAGGLE_USERNAME, WORKER_CALLBACK_TOKEN)


class KaggleBatchDispatcher:
    def __init__(self, store, cli="kaggle", runner=subprocess.run):
        self.store, self.cli, self.runner = store, cli, runner
        self.owner = f"{os.getpid()}-{uuid.uuid4()}"
        self.thread = None
        self.wake = threading.Event()
        self.stop = threading.Event()

    def start(self, enabled=KAGGLE_BATCH_AUTOSTART):
        if not enabled:
            return False
        if os.getenv("FLASK_DEBUG", "false").lower() == "true" and os.getenv("WERKZEUG_RUN_MAIN", "").lower() != "true":
            return False
        if self.thread and self.thread.is_alive():
            return True
        self.thread = threading.Thread(target=self.run_forever, name="KaggleBatchSupervisor", daemon=True)
        self.thread.start()
        return True

    def notify(self):
        self.wake.set()

    def can_accept_user_jobs(self):
        """Cheap readiness gate before starting LLM/TTS work for a user job."""
        if not KAGGLE_BATCH_AUTOSTART:
            return False
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,60}/[A-Za-z0-9_-]{1,100}", KAGGLE_KERNEL_ID)
                or KAGGLE_KERNEL_ID.startswith("owner/")):
            self.store.gpu_supervisor_update(unavailable_reason="Set KAGGLE_KERNEL_ID to your actual Kaggle username/notebook-slug.")
            return False
        if not (KAGGLE_API_TOKEN or (KAGGLE_USERNAME and KAGGLE_KEY)):
            self.store.gpu_supervisor_update(unavailable_reason="Kaggle CLI credentials are not configured.")
            return False
        if len(WORKER_CALLBACK_TOKEN) < 32:
            self.store.gpu_supervisor_update(unavailable_reason="Worker callback token is not configured.")
            return False
        if not (KAGGLE_KERNEL_PATH.is_dir() and (KAGGLE_KERNEL_PATH / "kernel-metadata.json").is_file()):
            self.store.gpu_supervisor_update(unavailable_reason="Kaggle kernel path or metadata is missing.")
            return False
        snapshot = self.store.gpu_supervisor_snapshot()
        reason = (snapshot.get("unavailable_reason") or "").lower()
        if any(marker in reason for marker in ("exceeded its configured runtime", "status is temporarily unavailable", "cli is not installed", "temporarily unavailable")):
            return False
        if snapshot.get("state") in {"starting", "running", "exiting"}:
            if snapshot.get("unavailable_reason"):
                return False
            last_heartbeat = snapshot.get("worker_heartbeat_at")
            heartbeat_age = time.time() - float(last_heartbeat) if last_heartbeat else None
            run_age = time.time() - float(snapshot.get("started_at") or time.time())
            stale = heartbeat_age is None and run_age > max(180, KAGGLE_BATCH_POLL_SECONDS * 4)
            stale = stale or (heartbeat_age is not None and heartbeat_age > max(180, KAGGLE_BATCH_POLL_SECONDS * 4))
            if snapshot.get("state") == "running" and stale:
                self.store.gpu_supervisor_update(unavailable_reason="Kaggle worker heartbeat is stale; waiting for run reconciliation.")
                return False
            return True
        allowed, reason = self.budget_allows_start()
        if not allowed:
            self.store.gpu_supervisor_update(unavailable_reason=reason)
            return False
        try:
            result = self._cli(["kernels", "status", KAGGLE_KERNEL_ID], 20)
            if result.returncode == 0:
                status = self.parse_status((result.stdout or "") + (result.stderr or ""))
                if status in {"RUNNING", "QUEUED", "PENDING"}:
                    self.store.gpu_supervisor_update(state="failed", unavailable_reason="Kaggle reports an active run without matching local dispatch state.")
                    return False
                if status not in {"COMPLETE", "COMPLETED", "ERROR", "FAILED", "CANCELED", "CANCELLED"}:
                    self.store.gpu_supervisor_update(unavailable_reason="Kaggle returned an unrecognized run state; automatic launch paused.")
                    return False
                if snapshot.get("state") in {"failed", "idle"} or snapshot.get("unavailable_reason"):
                    self.store.gpu_supervisor_update(state="idle", unavailable_reason=None)
                return True
            self.store.gpu_supervisor_update(unavailable_reason="Kaggle status is temporarily unavailable.", last_error_at=time.time())
            return False
        except RuntimeError as exc:
            self.store.gpu_supervisor_update(unavailable_reason=str(exc)[:160], last_error_at=time.time())
            return False

    def _env(self):
        env = os.environ.copy()
        if KAGGLE_API_TOKEN:
            env["KAGGLE_API_TOKEN"] = KAGGLE_API_TOKEN
        if KAGGLE_USERNAME:
            env["KAGGLE_USERNAME"] = KAGGLE_USERNAME
        if KAGGLE_KEY:
            env["KAGGLE_KEY"] = KAGGLE_KEY
        return env

    def _cli(self, args, timeout):
        try:
            return self.runner([self.cli, *args], cwd=str(KAGGLE_KERNEL_PATH.parent),
                               env=self._env(), capture_output=True, text=True,
                               timeout=timeout, check=False)
        except FileNotFoundError:
            raise RuntimeError("Kaggle CLI is not installed.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Kaggle CLI operation timed out.")

    @staticmethod
    def _prepare_kernel_files(metadata_path: Path, notebook_path: Path) -> None:
        """Fill public deployment details from Render env before pushing the notebook."""
        kernel_id = KAGGLE_KERNEL_ID.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}/[A-Za-z0-9_-]{1,100}", kernel_id):
            raise RuntimeError("Set KAGGLE_KERNEL_ID to your Kaggle username/notebook-slug value.")
        base_url = (os.getenv("RENDER_EXTERNAL_URL", "").strip() or
                    os.getenv("FLASK_WORKER_BASE_URL", "").strip()).rstrip("/")
        if not base_url.startswith("https://"):
            raise RuntimeError("Render public HTTPS URL is unavailable; set FLASK_WORKER_BASE_URL.")

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
            cells = notebook["cells"]
            source = "".join(cells[3]["source"])
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise RuntimeError("Kaggle metadata or worker notebook is invalid.") from None

        metadata["id"] = kernel_id
        source, url_replacements = re.subn(
            r'^FLASK_WORKER_BASE_URL\s*=.*$',
            f"FLASK_WORKER_BASE_URL = {json.dumps(base_url)}",
            source, count=1, flags=re.MULTILINE,
        )
        source, id_replacements = re.subn(
            r'_config_value\("KAGGLE_KERNEL_ID",\s*"[^"]*"\)',
            json.dumps(kernel_id), source, count=1,
        )
        if url_replacements != 1 or id_replacements != 1:
            raise RuntimeError("Worker notebook config markers could not be updated.")
        cells[3]["source"] = source.splitlines(keepends=True)

        for path, payload in ((metadata_path, metadata), (notebook_path, notebook)):
            temporary = path.with_name(path.name + ".partial")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            temporary.replace(path)

    def budget_allows_start(self):
        snap = self.store.gpu_supervisor_snapshot()
        if KAGGLE_BATCH_MAX_STARTS_PER_DAY <= 0 or snap["launches_24h"] >= KAGGLE_BATCH_MAX_STARTS_PER_DAY:
            return False, "Daily Kaggle batch start cap reached. GPU worker unavailable until the cap window resets."
        runtime_total = snap["runtime_7d_seconds"]
        if snap.get("active_dispatch_id") and snap.get("started_at"):
            runtime_total += max(0, time.time() - float(snap["started_at"]))
        if (KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS and
                runtime_total >= KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS):
            return False, "Weekly Kaggle runtime budget reached. GPU worker paused."
        return True, None

    def launch(self):
        if not (KAGGLE_API_TOKEN or (KAGGLE_USERNAME and KAGGLE_KEY)):
            raise RuntimeError("Kaggle CLI credentials are not configured.")
        if len(WORKER_CALLBACK_TOKEN) < 32:
            raise RuntimeError("WORKER_CALLBACK_TOKEN must be a high-entropy secret of at least 32 characters.")
        meta = KAGGLE_KERNEL_PATH / "kernel-metadata.json"
        notebook = KAGGLE_KERNEL_PATH / "fluxstory_kaggle_auto_batch_worker.ipynb"
        if not meta.is_file() or not notebook.is_file():
            raise RuntimeError("Kaggle kernel path must contain kernel-metadata.json and the finite worker notebook.")
        allowed, reason = self.budget_allows_start()
        if not allowed:
            self.store.gpu_supervisor_update(state="idle", unavailable_reason=reason, last_error_at=time.time())
            return False
        try:
            status_result = self._cli(["kernels", "status", KAGGLE_KERNEL_ID], 30)
        except RuntimeError as exc:
            self.store.gpu_supervisor_update(state="idle", unavailable_reason=str(exc)[:160], last_error_at=time.time())
            return False
        if status_result.returncode != 0:
            raise RuntimeError("Kaggle status is temporarily unavailable; batch launch paused.")
        observed = self.parse_status((status_result.stdout or "") + (status_result.stderr or ""))
        if observed in {"RUNNING", "QUEUED", "PENDING"}:
            self.store.gpu_supervisor_update(state="failed", unavailable_reason="Kaggle reports an active run without matching local dispatch state.")
            return False
        if observed not in {"COMPLETE", "COMPLETED", "ERROR", "FAILED", "CANCELED", "CANCELLED"}:
            self.store.gpu_supervisor_update(state="idle", unavailable_reason="Kaggle returned an unrecognized run state; automatic launch paused.")
            return False
        try:
            self._prepare_kernel_files(meta, notebook)
        except RuntimeError as exc:
            self.store.gpu_supervisor_update(state="idle", unavailable_reason=str(exc)[:160], last_error_at=time.time())
            return False
        dispatch_id = str(uuid.uuid4())
        self.store.gpu_supervisor_update(state="starting", active_dispatch_id=dispatch_id,
                                         worker_run_id=None, kernel_id=KAGGLE_KERNEL_ID,
                                         started_at=time.time(), unavailable_reason=None)
        self.store.gpu_run_update(dispatch_id, "starting")
        try:
            result = self._cli(["kernels", "push", "-p", str(KAGGLE_KERNEL_PATH),
                                "--timeout", str(KAGGLE_BATCH_TIMEOUT_SECONDS),
                                "--accelerator", KAGGLE_BATCH_ACCELERATOR], 180)
        except RuntimeError as exc:
            self.store.gpu_run_update(dispatch_id, "failed", finished_at=time.time(), result="Kaggle CLI launch unavailable")
            self.store.gpu_supervisor_update(state="failed", active_dispatch_id=None,
                unavailable_reason=str(exc)[:160], last_error_at=time.time())
            return False
        # Never persist raw CLI output: some CLI errors include account metadata.
        if result.returncode != 0:
            self.store.gpu_run_update(dispatch_id, "failed", finished_at=time.time(), result="Kaggle CLI launch failed")
            self.store.gpu_supervisor_update(state="failed", active_dispatch_id=None,
                unavailable_reason="Kaggle batch launch failed. Check private CLI diagnostics on the host.", last_error_at=time.time())
            return False
        return True

    @staticmethod
    def parse_status(text):
        value = (text or "").strip().upper()
        known = ("COMPLETE", "COMPLETED", "RUNNING", "QUEUED", "PENDING", "ERROR", "FAILED", "CANCELED", "CANCELLED")
        for state in known:
            if re.search(rf"\b{state}\b", value):
                return state
        return "UNKNOWN"

    def poll(self):
        result = self._cli(["kernels", "status", KAGGLE_KERNEL_ID], 30)
        snapshot = self.store.gpu_supervisor_snapshot()
        dispatch_id = snapshot.get("active_dispatch_id")
        if result.returncode != 0:
            self.store.gpu_supervisor_update(unavailable_reason="Kaggle status is temporarily unavailable.", last_error_at=time.time())
            return "UNKNOWN"
        state = self.parse_status((result.stdout or "") + "\n" + (result.stderr or ""))
        now = time.time()
        self.store.gpu_supervisor_update(last_poll_at=now)
        if state in {"COMPLETE", "COMPLETED"}:
            if snapshot.get("worker_run_id"):
                self.store.recover_gpu_run_requests(snapshot["worker_run_id"])
            self.store.recover_gpu_leases()
            if dispatch_id:
                run = snapshot.get("last_run", {})
                elapsed = max(0, now - float(run.get("started_at") or now))
                self.store.gpu_run_update(dispatch_id, "completed", finished_at=now,
                                          last_poll_at=now, result="Kaggle run completed", elapsed_seconds=elapsed)
            self.store.gpu_supervisor_update(state="idle", active_dispatch_id=None, worker_run_id=None,
                                             last_result="completed", unavailable_reason=None)
        elif state in {"ERROR", "FAILED", "CANCELED", "CANCELLED"}:
            if snapshot.get("worker_run_id"):
                self.store.recover_gpu_run_requests(snapshot["worker_run_id"])
            self.store.recover_gpu_leases()
            if dispatch_id:
                self.store.gpu_run_update(dispatch_id, "failed", finished_at=now, last_poll_at=now,
                                          result="Kaggle run failed")
            self.store.gpu_supervisor_update(state="failed", active_dispatch_id=None, worker_run_id=None,
                last_result="failed", unavailable_reason="Kaggle batch failed; recoverable requests remain queued.", last_error_at=now)
        elif state in {"RUNNING", "QUEUED", "PENDING"}:
            timed_out = snapshot.get("started_at") and now - float(snapshot["started_at"]) > KAGGLE_BATCH_MAX_RUNTIME_SECONDS + 300
            reason = "Kaggle batch exceeded its configured runtime; stop it in Kaggle UI." if timed_out else None
            self.store.gpu_supervisor_update(state="exiting" if snapshot.get("state") == "exiting" else "running", unavailable_reason=reason)
        else:
            # Unknown states are treated as active. Never risk a duplicate version.
            self.store.gpu_supervisor_update(unavailable_reason="Kaggle returned an unrecognized run state; automatic launch paused.")
        return state

    def run_once(self):
        if not self.store.claim_gpu_supervisor(self.owner, lease_seconds=300):
            return
        snap = self.store.gpu_supervisor_snapshot()
        self.store.gpu_supervisor_update(heartbeat_at=time.time())
        if snap.get("active_dispatch_id"):
            try:
                self.poll()
            except RuntimeError as exc:
                self.store.gpu_supervisor_update(unavailable_reason=str(exc)[:160], last_error_at=time.time())
            return
        if not snap["queue_depth"]:
            return
        self.wake.clear()
        self.stop.wait(KAGGLE_BATCH_DEBOUNCE_SECONDS)
        if not self.store.gpu_queue_depth():
            return
        try:
            self.launch()
        except RuntimeError as exc:
            self.store.gpu_supervisor_update(state="idle", unavailable_reason=str(exc)[:160], last_error_at=time.time())

    def run_forever(self):
        backoff = 5
        while not self.stop.is_set():
            try:
                self.run_once()
                backoff = KAGGLE_BATCH_POLL_SECONDS
            except Exception:
                # No exception text is logged because it could contain secrets.
                backoff = min(300, max(5, backoff * 2))
                self.store.gpu_supervisor_update(unavailable_reason="Kaggle supervisor error; retrying with backoff.", last_error_at=time.time())
            self.wake.wait(backoff)
            self.wake.clear()
