"""
app.py
=============================================================================
Primary Control Plane: Flask Application & Distributed Pipeline Orchestrator
=============================================================================
Exposes REST endpoints and interactive UI for video creation, monitors
background rendering workers, and serves finished MP4 video streams.
"""

import os
import sys
import uuid
import time
import base64
import copy
import logging
import math
import traceback
import threading
import re
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, render_template, request, jsonify, send_from_directory, url_for, session, g, redirect, abort
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    TEMP_DIR, SECRET_KEY, MAX_CONTENT_LENGTH,
    DEEPGRAM_API_KEY, LLM_PROVIDER,
    KAGGLE_BATCH_AUTOSTART, KAGGLE_KERNEL_ID, KAGGLE_BATCH_IDLE_EXIT_SECONDS,
    KAGGLE_BATCH_MAX_RUNTIME_SECONDS, KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS,
    KAGGLE_BATCH_MAX_STARTS_PER_DAY, GPU_REQUEST_LEASE_SECONDS, GPU_REQUEST_MAX_ATTEMPTS,
    WORKER_CALLBACK_TOKEN,
    DEFAULT_LLM_MODEL, DEFAULT_TTS_VOICE, DEFAULT_FPS, RESOLUTION_PROFILES,
    AURA_VOICES, STYLE_PRESETS, VOICE_PREVIEW_TEXT, get_resolution
)
from services import (
    plan_narrative,
    generate_speech,
    fetch_scene_image,
    assemble_video
)
from services.image_client import WorkerUnavailableError, WorkerRequestError
from services.image_client import configure_batch_request_manager
from services.auth_store import AuthStore
from services.kaggle_batch_dispatcher import KaggleBatchDispatcher
from services.gpu_request_client import KaggleBatchGpuClient
from services.object_storage import create_signed_url, delete_files, download_file, upload_file
from PIL import Image, UnidentifiedImageError
import io

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ControlPlane")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)

auth_store = AuthStore()
gpu_dispatcher = KaggleBatchDispatcher(auth_store)
gpu_client = KaggleBatchGpuClient(auth_store, gpu_dispatcher)
configure_batch_request_manager(gpu_client)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32), method="scrypt:32768:8:1")

# PostgreSQL stores queued tasks. This in-memory event only wakes the consumer.
MAX_PENDING_JOBS = 100
JOBS_LOCK = threading.Lock()
JOBS: Dict[str, Dict[str, Any]] = {}
QUEUE_WAKE = threading.Event()
CONSUMER_START_LOCK = threading.Lock()
CONSUMER_THREAD: threading.Thread | None = None
CONSUMER_LOCK_HANDLE = None
WORKER_LEASE_SECONDS = 180
MAX_JOB_ATTEMPTS = 3


def _restore_projects() -> None:
    """Load durable task records into the compatibility cache."""
    for job in auth_store.load_jobs():
        job_id = job.get("task_id") or job.get("job_id")
        if job_id:
            JOBS[job_id] = job


_restore_projects()


def _persist_job_locked(job_id: str) -> None:
    job = JOBS.get(job_id)
    if job and job.get("owner_id"):
        try:
            auth_store.save_job_state(job_id, job)
        except Exception:
            logger.exception("Could not persist project %s", job_id)


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_auth_context():
    return {"current_user": getattr(g, "current_user", None), "csrf_token": _csrf_token}


@app.before_request
def load_authenticated_user_and_check_csrf():
    user_id = session.get("user_id")
    g.current_user = auth_store.get_user_by_id(user_id) if user_id else None
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.path.startswith("/internal/gpu/"):
            return None
        expected = session.get("csrf_token", "")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({"error": "Your session token expired. Refresh the page and try again."}), 400


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.current_user:
            if request.path.startswith("/api/") or request.path.startswith("/status/") or request.path == "/generate":
                return jsonify({"error": "Sign in to access your workspace."}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _safe_next_path(path: str | None) -> str:
    parsed = urlsplit(path or "")
    if path and "\\" not in path and parsed.scheme == "" and parsed.netloc == "" and parsed.path.startswith("/") and not parsed.path.startswith("//"):
        return path
    return url_for("index")


def _login_identity_hash(email: str) -> str:
    source = f"{request.remote_addr or 'unknown'}\\0{email}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def _start_user_session(user_id: int) -> None:
    session.clear()
    session["user_id"] = int(user_id)
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.current_user:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        signup_identity = _login_identity_hash("signup")
        now = time.time()
        if auth_store.recent_login_attempts(signup_identity, now - 900) >= 10:
            return render_template("auth.html", auth_mode="signup", error="Too many account requests. Wait 15 minutes and try again."), 429
        auth_store.record_login_attempt(signup_identity, now)
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirmation", "")
        if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Enter a valid email address."
        elif len(password) < 12 or len(password) > 128:
            error = "Use a password between 12 and 128 characters."
        elif password != confirmation:
            error = "The passwords do not match."
        else:
            user_id = auth_store.create_user(email, generate_password_hash(password, method="scrypt:32768:8:1"))
            if user_id is None:
                error = "If this address already has an account, sign in instead."
            else:
                _start_user_session(user_id)
                return redirect(url_for("index"))
    return render_template("auth.html", auth_mode="signup", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.current_user:
        return redirect(url_for("index"))
    error = None
    next_path = _safe_next_path(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        identity_hash = _login_identity_hash(email)
        now = time.time()
        if auth_store.recent_login_attempts(identity_hash, now - 900) >= 10:
            error = "Too many sign-in attempts. Wait 15 minutes and try again."
        else:
            user = auth_store.get_user_by_email(email)
            password_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
            password_valid = check_password_hash(password_hash, request.form.get("password", ""))
            valid = bool(user and password_valid)
            if not valid:
                auth_store.record_login_attempt(identity_hash, now)
                error = "Email or password is incorrect."
            else:
                auth_store.clear_login_attempts(identity_hash)
                _start_user_session(user["id"])
                return redirect(_safe_next_path(request.form.get("next")))
    return render_template("auth.html", auth_mode="login", error=error, next_path=next_path)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


def log_job_message(job_id: str, message: str) -> None:
    """Appends a timestamped log entry to the job execution record."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            ts = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{ts}] {message}")
            job["updated_at"] = time.time()
            _persist_job_locked(job_id)
            logger.info("[%s] %s", job_id[:8], message)


def update_job_state(job_id: str, **kwargs) -> None:
    """Thread-safe state updater for job progress tracking."""
    with JOBS_LOCK:
        job = auth_store.get_job(job_id) or JOBS.get(job_id)
        if job is not None:
            job.update(kwargs)
            job["updated_at"] = time.time()
            if kwargs.get("status") in {"COMPLETED", "FAILED"}:
                job["finished_at"] = job["updated_at"]
            JOBS[job_id] = job
            _persist_job_locked(job_id)


def run_video_generation_pipeline(job_id: str, params: Dict[str, Any]) -> None:
    """
    Main asynchronous pipeline worker executing stages 1 through 5.
    Guarantees isolated directory storage and resilient error handling.
    """
    start_time = time.time()
    update_job_state(
        job_id,
        status="RUNNING",
        progress_percent=2,
        stage="Initializing Environment",
        step_description="Provisioning isolated workspace directories..."
    )
    log_job_message(job_id, "Starting automated media synthesis pipeline...")

    # Create job-specific temporary directories
    job_dir = TEMP_DIR / job_id
    audio_dir = job_dir / "audio"
    images_dir = job_dir / "images"
    audio_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    try:
        story_text = params["story_text"]
        duration_minutes = float(params.get("duration_minutes", 1.0))
        target_scene_count = int(params.get("image_count", min(60, max(2, round(duration_minutes * 60 / 7)))))
        aspect_ratio = params.get("aspect_ratio", "16:9")
        deepgram_key = DEEPGRAM_API_KEY
        tts_voice = params.get("tts_voice") or DEFAULT_TTS_VOICE
        style_preset = params.get("style_preset", "cinematic")
        resolution = get_resolution(aspect_ratio)

        log_job_message(job_id, f"Target duration: {duration_minutes:.2f}m | Resolution: {resolution[0]}x{resolution[1]} ({aspect_ratio})")
        log_job_message(job_id, f"Voice model: {tts_voice} | Style preset: {style_preset}")

        # Resume from persisted stages where possible. Scene prompts are planned
        # once, and already completed scene audio/images are reused after recovery.
        saved_state = auth_store.get_job(job_id) or {}
        scenes = saved_state.get("scenes") or []
        if not scenes:
            update_job_state(job_id, progress_percent=10, stage="1. Narrative Decomposition",
                             step_description="Director LLM decomposing script into timed scene manifests...")
            log_job_message(job_id, "Stage 1: Deconstructing narrative with LLM Director...")
            scenes = plan_narrative(
                story_text=story_text,
                duration_minutes=duration_minutes,
                api_key=None,
                model=DEFAULT_LLM_MODEL,
                style_preset=style_preset,
                provider=LLM_PROVIDER,
                target_scene_count=target_scene_count
            )
            for sc in scenes:
                sc.update(status="pending", audio_ready=False, image_ready=False,
                          image_url=None, audio_url=None)
            update_job_state(job_id, progress_percent=20, scenes=scenes,
                             step_description=f"Planned {len(scenes)} scenes. Starting speech synthesis...")
            log_job_message(job_id, f"Stage 1 Complete: Generated {len(scenes)} structured scene manifests.")
        else:
            log_job_message(job_id, "Resuming from saved scene manifest.")
        num_scenes = len(scenes)

        # =====================================================================
        # STAGE 2: VOICEOVER SYNTHESIS (DEEPGRAM AURA TTS)
        # =====================================================================
        update_job_state(
            job_id,
            stage="2. Voiceover Synthesis",
            step_description="Synthesizing narration with Deepgram Aura..."
        )
        log_job_message(job_id, "Stage 2: Synthesizing scene voiceovers via Deepgram Aura...")

        total_audio_duration = 0.0
        for i, scene in enumerate(scenes):
            sc_id = scene["scene_id"]
            audio_file = audio_dir / f"scene_{sc_id:02d}.mp3"
            if scene.get("audio_path") and Path(scene["audio_path"]).is_file() and scene.get("audio_duration"):
                total_audio_duration += float(scene["audio_duration"])
                continue
            if scene.get("audio_storage_key"):
                download_file(scene["audio_storage_key"], audio_file)
                scene["audio_path"] = str(audio_file)
                scene["audio_ready"] = True
                total_audio_duration += float(scene["audio_duration"])
                update_job_state(job_id, scenes=scenes)
                continue
            log_job_message(job_id, f"Synthesizing audio for Scene {sc_id}/{num_scenes} ({len(scene['narration_text'])} chars)...")

            aud_dur = generate_speech(
                text=scene["narration_text"],
                api_key=deepgram_key,
                output_path=audio_file,
                model=tts_voice
            )

            scene["audio_path"] = str(audio_file)
            scene["audio_duration"] = aud_dur
            scene["audio_ready"] = True
            scene["audio_url"] = f"/temp/{job_id}/audio/{audio_file.name}"
            owner_id = (auth_store.get_job(job_id) or {}).get("owner_id")
            if not owner_id:
                raise RuntimeError("Could not resolve media owner for Supabase Storage.")
            scene["audio_storage_key"] = f"media/{int(owner_id)}/{job_id}/audio/{audio_file.name}"
            upload_file(scene["audio_storage_key"], audio_file, "audio/mpeg")
            total_audio_duration += aud_dur

            progress = 20 + int((i + 1) / num_scenes * 20)
            update_job_state(
                job_id,
                progress_percent=progress,
                scenes=scenes,
                step_description=f"Voiceover {i + 1}/{num_scenes} synthesized ({aud_dur:.1f}s)."
            )

        log_job_message(job_id, f"Stage 2 Complete: Master narration duration is {total_audio_duration:.2f} seconds.")

        # =====================================================================
        # STAGE 3: GENERATIVE VISUALS (KAGGLE FLUX WORKER)
        # =====================================================================
        update_job_state(
            job_id,
            stage="3. Generative Visuals",
            step_description="Dispatching visual diffusion prompts to Kaggle Worker..."
        )
        log_job_message(job_id, "Stage 3: Queueing scene images for the on-demand Kaggle batch worker...")

        base_seed = int(params.get("seed", 42))

        image_batch = []
        image_scenes = []
        for i, scene in enumerate(scenes):
            sc_id = scene["scene_id"]
            img_file = images_dir / f"scene_{sc_id:02d}.png"
            scene_seed = base_seed + (sc_id * 101)
            if scene.get("image_storage_key") and not img_file.is_file():
                download_file(scene["image_storage_key"], img_file)
                scene["image_path"] = str(img_file)
                scene["image_ready"] = True
                scene["image_url"] = f"/temp/{job_id}/images/{img_file.name}"
                continue
            if scene.get("image_path") and Path(scene["image_path"]).is_file():
                scene["image_ready"] = True
                continue
            if img_file.is_file():
                scene.update(image_path=str(img_file), image_ready=True,
                             image_url=f"/temp/{job_id}/images/{img_file.name}", status="rendered")
                update_job_state(job_id, scenes=scenes)
                continue
            image_batch.append({"prompt": scene["visual_prompt"], "width": resolution[0],
                "height": resolution[1], "save_path": img_file, "seed": scene_seed})
            image_scenes.append((i, scene, img_file))

        if image_batch:
            job_record = auth_store.get_job(job_id) or {}
            queued_gpu = gpu_client.enqueue_batch(job_id, job_record.get("owner_id"), image_batch)
            update_job_state(job_id, stage="3. Generative Visuals",
                step_description=f"Waiting for {len(queued_gpu)} scene images in the GPU batch queue.")
            for n, ((scene_index, scene, img_file), gpu_request) in enumerate(zip(image_scenes, queued_gpu), 1):
                gpu_client.wait_for(gpu_request["request_id"])
                scene["image_path"] = str(img_file)
                scene["image_ready"] = True
                scene["image_url"] = f"/temp/{job_id}/images/{img_file.name}"
                scene["status"] = "rendered"
                gpu_record = auth_store.get_gpu_request(gpu_request["request_id"])
                if gpu_record and str(gpu_record.get("image_path") or "").startswith("supabase://"):
                    scene["image_storage_key"] = gpu_record["image_path"][len("supabase://"):]
                else:
                    raise RuntimeError("Kaggle worker completed an image without storing it in Supabase.")
                progress = 40 + int((sum(1 for s in scenes if s.get("image_ready")) / num_scenes) * 30)
                update_job_state(job_id, progress_percent=progress, scenes=scenes,
                    step_description=f"Rendered generative frame {n}/{len(image_batch)} in the batch.")

        log_job_message(job_id, f"Stage 3 Complete: All {num_scenes} generative frames acquired.")

        # =====================================================================
        # STAGE 4 & 5: MOTION, TRANSITIONS & FINAL RENDERING (MOVIEPY)
        # =====================================================================
        update_job_state(
            job_id,
            stage="4. Motion & Video Compositing",
            step_description="Applying sub-pixel Ken Burns motion and crossfades..."
        )
        log_job_message(job_id, "Stage 4 & 5: Applying Ken Burns transforms, crossfade transitions, and H.264 encode...")

        output_dir = TEMP_DIR / job_id / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_filename = output_dir / f"video_{job_id}.mp4"

        def video_progress_cb(pct: float, desc: str) -> None:
            # Map composer progress (0-100) to overall pipeline progress (70-98)
            overall_pct = 70 + int(pct * 0.28)
            update_job_state(job_id, progress_percent=overall_pct, step_description=desc)
            if pct in (25.0, 50.0, 75.0, 100.0):
                log_job_message(job_id, f"[Compositor] {desc}")

        rendered_mp4 = assemble_video(
            scene_manifest=scenes,
            output_filename=output_filename,
            resolution=resolution,
            fps=DEFAULT_FPS,
            progress_callback=video_progress_cb
        )

        total_elapsed = round(time.time() - start_time, 1)
        video_rel_url = f"/exports/{job_id}/{Path(rendered_mp4).name}"
        owner_id = (auth_store.get_job(job_id) or {}).get("owner_id")
        if not owner_id:
            raise RuntimeError("Could not resolve media owner for Supabase Storage.")
        video_storage_key = f"videos/{int(owner_id)}/{job_id}/{Path(rendered_mp4).name}"
        upload_file(video_storage_key, rendered_mp4, "video/mp4")

        update_job_state(
            job_id,
            status="COMPLETED",
            progress_percent=100,
            stage="Completed",
            step_description="Video generation completed successfully!",
            video_url=video_rel_url,
            video_storage_key=video_storage_key,
            elapsed_seconds=total_elapsed,
            total_duration_seconds=round(total_audio_duration, 1),
            scenes=scenes
        )

        log_job_message(job_id, f"PIPELINE SUCCESS! Video produced in {total_elapsed}s. Output: {video_rel_url}")

    except WorkerUnavailableError:
        raise
    except Exception as exc:
        err_trace = traceback.format_exc()
        logger.error("[%s] Pipeline execution failed: %s\n%s", job_id, exc, err_trace)
        log_job_message(job_id, "Generation failed; see server log for diagnostic details.")
        update_job_state(
            job_id,
            status="FAILED",
            stage="Failed",
            step_description="Video generation failed. Please try again.",
            error="Video generation failed. Please try again."
        )


def run_image_generation_task(job_id: str, params: Dict[str, Any]) -> None:
    """Generate one PNG through the configured Kaggle image worker."""
    image_path = TEMP_DIR / job_id / "images" / "result.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    update_job_state(job_id, status="RUNNING", progress_percent=5,
                     stage="Generating image", step_description="Sending prompt to image worker...")
    job = auth_store.get_job(job_id) or {}
    fetch_scene_image(
        prompt=params["prompt"],
        width=params["width"],
        height=params["height"],
        save_path=image_path,
        seed=params["seed"],
        parent_task_id=job_id,
        user_id=job.get("owner_id"),
    )
    from PIL import Image
    with Image.open(image_path) as image:
        actual_width, actual_height = image.size
    image_storage_key = None
    image_storage_key = f"media/{int(job['owner_id'])}/{job_id}/images/result.png"
    upload_file(image_storage_key, image_path, "image/png")
    update_job_state(job_id, status="COMPLETED", progress_percent=100,
                     stage="Completed", step_description="Image generation completed.",
                     image_path=str(image_path), width=actual_width,
                     height=actual_height, image_storage_key=image_storage_key)


def _acquire_consumer_lock():
    """Acquire a Linux file lock so only one queue consumer runs at a time."""
    lock_path = TEMP_DIR / ".job-consumer.lock"
    handle = open(lock_path, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, BlockingIOError):
        handle.close()
        return None


def _heartbeat_job(task_id: str, worker_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(WORKER_LEASE_SECONDS / 3):
        try:
            if not auth_store.heartbeat_job(task_id, worker_id, WORKER_LEASE_SECONDS):
                return
        except Exception:
            logger.exception("Could not renew lease for task %s", task_id[:8])


def _claim_job_if_worker_ready(worker_id: str):
    """Do not start expensive LLM/TTS stages while batch GPU service is disabled."""
    if not gpu_dispatcher.can_accept_user_jobs():
        return None
    return auth_store.claim_next_job(worker_id, WORKER_LEASE_SECONDS)


def _durable_queue_consumer() -> None:
    global CONSUMER_LOCK_HANDLE
    lock_handle = None
    while lock_handle is None:
        lock_handle = _acquire_consumer_lock()
        if lock_handle is None:
            # During a graceful Gunicorn reload the old consumer may still own
            # the lock briefly. Wait so the replacement can take over after exit.
            time.sleep(0.5)
    CONSUMER_LOCK_HANDLE = lock_handle
    worker_id = f"{os.getpid()}-{uuid.uuid4()}"
    try:
        auth_store.recover_abandoned_jobs(MAX_JOB_ATTEMPTS)
        auth_store.recover_gpu_leases(GPU_REQUEST_MAX_ATTEMPTS)
        for job in auth_store.load_jobs():
            with JOBS_LOCK:
                JOBS[job.get("task_id") or job.get("job_id")] = job
    except Exception:
        logger.exception("Could not recover durable generation jobs")

    health_backoff = 1.0
    health_retry_at = 0.0
    while True:
        try:
            stats = auth_store.queue_stats()
            if stats["queued"] == 0:
                QUEUE_WAKE.wait(1.0)
                QUEUE_WAKE.clear()
                continue

            available_delay = auth_store.next_available_delay(default=1.0)
            if available_delay > 0.15:
                QUEUE_WAKE.wait(min(available_delay, 1.0))
                QUEUE_WAKE.clear()
                continue

            remaining_health_delay = health_retry_at - time.monotonic()
            if remaining_health_delay > 0:
                QUEUE_WAKE.wait(min(remaining_health_delay, 1.0))
                QUEUE_WAKE.clear()
                continue

            if not KAGGLE_BATCH_AUTOSTART:
                QUEUE_WAKE.wait(30.0)
                QUEUE_WAKE.clear()
                continue

            job = _claim_job_if_worker_ready(worker_id)
            if not job:
                logger.warning("GPU worker unavailable; user jobs remain queued.")
                health_retry_at = time.monotonic() + health_backoff
                health_backoff = min(60.0, health_backoff * 2)
                continue
            health_backoff = 1.0
            health_retry_at = 0.0
            task_id = job.get("task_id") or job["job_id"]
            with JOBS_LOCK:
                JOBS[task_id] = job
            stop_heartbeat = threading.Event()
            heartbeat = threading.Thread(target=_heartbeat_job,
                                         args=(task_id, worker_id, stop_heartbeat), daemon=True)
            heartbeat.start()
            try:
                task_type = job.get("task_type") or ("image" if job.get("params", {}).get("_task_type") == "image" else "video")
                if task_type == "image":
                    run_image_generation_task(task_id, job.get("params") or {})
                else:
                    options = dict(job.get("params") or {})
                    options["story_text"] = job.get("story_text", options.get("story_text", ""))
                    run_video_generation_pipeline(task_id, options)
            except WorkerUnavailableError:
                logger.warning("Remote image worker failed during task %s; returning it to the queue.", task_id[:8])
                delay = min(60.0, 2 ** int(job.get("attempt_count", 1)))
                auth_store.release_job(task_id, worker_id, "remote image worker unavailable", delay, MAX_JOB_ATTEMPTS)
            except Exception:
                logger.exception("Generation failed for task %s", task_id[:8])
                update_job_state(task_id, status="FAILED", stage="Failed",
                                 step_description="Generation failed. Please try again.",
                                 error="Generation failed. Please try again.")
            finally:
                stop_heartbeat.set()
                heartbeat.join(timeout=1.0)
                refreshed = auth_store.get_job(task_id)
                if refreshed:
                    with JOBS_LOCK:
                        JOBS[task_id] = refreshed
        except Exception:
            logger.exception("Durable queue consumer loop failed; retrying shortly")
            QUEUE_WAKE.wait(1.0)
            QUEUE_WAKE.clear()


def _start_queue_worker() -> bool:
    """Start one PostgreSQL-backed queue consumer in this web process."""
    global CONSUMER_THREAD
    with CONSUMER_START_LOCK:
        if CONSUMER_THREAD and CONSUMER_THREAD.is_alive():
            return True
        CONSUMER_THREAD = threading.Thread(target=_durable_queue_consumer,
                                           name="PostgresJobConsumer", daemon=True)
        CONSUMER_THREAD.start()
        return True


# =============================================================================
# FLASK WEB CONTROLLER ROUTES
# =============================================================================

@app.route("/")
@login_required
def index():
    """Renders the workspace overview."""
    with JOBS_LOCK:
        jobs = _job_summaries(int(g.current_user["id"]))
    return render_template("index.html", active_page="home", jobs=jobs[:5])


@app.route("/create")
@login_required
def create_page():
    """Renders the video creation workspace."""
    return render_template(
        "create.html", active_page="create", resolution_profiles=RESOLUTION_PROFILES,
        voices=AURA_VOICES, style_presets=STYLE_PRESETS, default_voice=DEFAULT_TTS_VOICE,
        voice_preview_text=VOICE_PREVIEW_TEXT
    )


@app.route("/projects")
@login_required
def projects_page():
    """Renders the project library."""
    with JOBS_LOCK:
        jobs = _job_summaries(int(g.current_user["id"]))
    return render_template("projects.html", active_page="projects", jobs=jobs)


@app.route("/projects/<job_id>")
@login_required
def project_detail_page(job_id: str):
    """Show the signed-in owner's live project details."""
    job = auth_store.owned_job(job_id, int(g.current_user["id"]))
    if not job:
        abort(404)
    created_at = job.get("created_at")
    created_at_label = (datetime.fromtimestamp(float(created_at)).astimezone().strftime("%b %d, %Y at %I:%M %p")
                        if created_at else "Unknown")
    return render_template("project_detail.html", active_page="projects", job=job,
                           job_id=job_id, created_at_label=created_at_label)


@app.route("/api/projects/<job_id>", methods=["DELETE"])
@login_required
def delete_project_api(job_id: str):
    """Delete an owned project and its durable queue records."""
    deleted_job = auth_store.delete_project(job_id, int(g.current_user["id"]))
    if not deleted_job:
        return jsonify({"error": "Project not found."}), 404

    with JOBS_LOCK:
        JOBS.pop(job_id, None)

    # Database rows are removed in one transaction. Storage is a separate
    # Supabase service, so clean its referenced objects after that commit.
    owner_id = int(g.current_user["id"])
    allowed_prefixes = (f"videos/{owner_id}/{job_id}/", f"media/{owner_id}/{job_id}/")
    object_keys = []
    candidates = [deleted_job.get("video_storage_key"), deleted_job.get("image_storage_key"),
                  deleted_job.get("audio_storage_key")]
    for scene in deleted_job.get("scenes") or []:
        candidates.extend((scene.get("image_storage_key"), scene.get("audio_storage_key")))
    for key in candidates:
        if isinstance(key, str) and key.startswith(allowed_prefixes):
            object_keys.append(key)
    cleanup_pending = False
    try:
        delete_files(object_keys)
    except Exception:
        cleanup_pending = True
        logger.exception("Project %s was deleted but Supabase media cleanup failed", job_id)
    return jsonify({"ok": True, "media_cleanup_pending": cleanup_pending}), 200


def _job_summaries(user_id: int):
    return [
        {"job_id": job.get("task_id") or job.get("job_id"), "title": job.get("title") or f"Job {(job.get('task_id') or job.get('job_id'))[:8]}", "status": job["status"], "stage": job["stage"],
         "progress_percent": job["progress_percent"], "created_at": job["created_at"],
         "video_url": job.get("video_url")}
        for job in auth_store.list_jobs_for_user(user_id)
    ]


@app.route("/api/create_video", methods=["POST"])
@login_required
def create_video():
    """
    Initiates asynchronous video synthesis pipeline.
    Accepts JSON or multipart form data.
    """
    if request.is_json:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
    else:
        payload = request.form.to_dict()

    raw_story_text = payload.get("story_text", "")
    if not isinstance(raw_story_text, str):
        return jsonify({"error": "Story text must be a string."}), 400
    story_text = raw_story_text.strip()
    if not story_text or len(story_text) > 20000:
        return jsonify({"error": "Story text is required."}), 400

    try:
        duration_minutes = float(payload.get("duration_minutes", 1.0))
        if not math.isfinite(duration_minutes) or duration_minutes < 0.2 or duration_minutes > 15.0:
            return jsonify({"error": "Duration must be between 0.2 and 15.0 minutes."}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid duration value."}), 400

    try:
        image_count_value = float(payload.get("image_count", min(60, max(2, round(duration_minutes * 60 / 7)))))
        if not image_count_value.is_integer():
            return jsonify({"error": "Image count must be a whole number between 2 and 60."}), 400
        image_count = int(image_count_value)
        if image_count < 2 or image_count > 60:
            return jsonify({"error": "Image count must be between 2 and 60."}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Image count must be a whole number between 2 and 60."}), 400

    aspect_ratio = str(payload.get("aspect_ratio", "16:9"))
    if aspect_ratio not in RESOLUTION_PROFILES:
        return jsonify({"error": "Unsupported aspect ratio."}), 400
    style_preset = str(payload.get("style_preset", "cinematic"))
    if style_preset not in STYLE_PRESETS:
        return jsonify({"error": "Unsupported visual style."}), 400
    tts_voice = str(payload.get("tts_voice", DEFAULT_TTS_VOICE))
    allowed_voices = {voice["id"] for voice in AURA_VOICES}
    if tts_voice not in allowed_voices:
        return jsonify({"error": "Unsupported voice selection."}), 400
    try:
        seed_value = payload.get("seed", 42)
        seed = int(seed_value)
        if isinstance(seed_value, float) and not seed_value.is_integer():
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "Seed must be an integer."}), 400

    job_id = str(uuid.uuid4())
    now = time.time()
    options = {"story_text": story_text, "duration_minutes": duration_minutes,
               "image_count": image_count, "aspect_ratio": aspect_ratio,
               "tts_voice": tts_voice, "style_preset": style_preset, "seed": seed}
    job_record = {
        "job_id": job_id, "task_id": job_id, "task_type": "video",
        "owner_id": int(g.current_user["id"]),
        "title": " ".join(story_text.split())[:72] or f"Video {job_id[:8]}",
        "story_text": story_text, "status": "QUEUED", "progress_percent": 0,
        "stage": "Queued", "step_description": "Job placed in background execution queue...",
        "created_at": now, "updated_at": now, "attempt_count": 0,
        "params": options, "scenes": [], "logs": [], "video_url": None, "error": None,
    }
    try:
        position = auth_store.enqueue_job(job_id, int(g.current_user["id"]), "video",
                                          options, job_record, MAX_PENDING_JOBS)
    except Exception:
        logger.exception("Could not create durable video task")
        return jsonify({"error": "The project could not be saved. Please try again."}), 503
    if position is None:
        response = jsonify({"error": "The generation queue is full. Please try again shortly."})
        response.headers["Retry-After"] = "5"
        return response, 429
    with JOBS_LOCK:
        JOBS[job_id] = job_record
    QUEUE_WAKE.set()
    logger.info("Queued new video generation job: %s", job_id)

    return jsonify({
        "status": "queued",
        "task_id": job_id,
        "job_id": job_id,
        "position": position,
        "poll_url": f"/api/status/{job_id}"
    }), 202


@app.route("/generate", methods=["POST"])
@login_required
def generate_image():
    """Queue a single PNG image generation request."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    prompt_value = payload.get("prompt", "")
    if not isinstance(prompt_value, str):
        return jsonify({"error": "Prompt must be a string."}), 400
    prompt = prompt_value.strip()
    if not prompt or len(prompt) > 10000:
        return jsonify({"error": "Prompt is required and must be 10,000 characters or fewer."}), 400
    try:
        width_value = payload.get("width", 1024)
        height_value = payload.get("height", 576)
        seed_value = payload.get("seed", 42)
        width = int(width_value)
        height = int(height_value)
        seed = int(seed_value)
        for original, converted in ((width_value, width), (height_value, height), (seed_value, seed)):
            if isinstance(original, float) and not original.is_integer():
                raise ValueError
            if isinstance(original, str) and str(converted) != original.strip():
                raise ValueError
        if not (64 <= width <= 2048 and 64 <= height <= 2048) or width % 16 or height % 16:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "Width and height must be integers from 64 to 2048; seed must be an integer."}), 400

    task_id = str(uuid.uuid4())
    now = time.time()
    params = {"_task_type": "image", "prompt": prompt, "width": width,
              "height": height, "seed": seed}
    job_record = {
        "job_id": task_id, "task_id": task_id, "task_type": "image",
        "owner_id": int(g.current_user["id"]),
        "title": "Image: " + " ".join(prompt.split())[:60],
        "status": "QUEUED", "progress_percent": 0, "stage": "Queued",
        "step_description": "Image task placed in the generation queue.",
        "created_at": now, "updated_at": now, "attempt_count": 0, "params": params,
        "scenes": [], "logs": [], "video_url": None, "error": None,
        "width": width, "height": height,
    }
    try:
        position = auth_store.enqueue_job(task_id, int(g.current_user["id"]), "image",
                                          params, job_record, MAX_PENDING_JOBS)
    except Exception:
        logger.exception("Could not persist image task")
        return jsonify({"error": "The task could not be saved. Please try again."}), 503
    if position is None:
        response = jsonify({"error": "The generation queue is full. Please try again shortly."})
        response.headers["Retry-After"] = "5"
        return response, 429
    with JOBS_LOCK:
        JOBS[task_id] = job_record
    QUEUE_WAKE.set()

    return jsonify({"task_id": task_id, "status": "queued", "position": position,
                    "message": f"Generation request accepted. Poll /status/{task_id} for updates.",
                    "poll_url": f"/status/{task_id}"}), 202


_GPU_CALLBACK_TIMES = {}
_GPU_CALLBACK_RATE_LOCK = threading.Lock()


def _worker_payload():
    if not WORKER_CALLBACK_TOKEN or len(WORKER_CALLBACK_TOKEN) < 32:
        return None, (jsonify({"error": "GPU worker callback authentication is not configured."}), 503)
    supplied = request.headers.get("X-Worker-Token", "")
    if not supplied or not hmac.compare_digest(supplied, WORKER_CALLBACK_TOKEN):
        return None, (jsonify({"error": "Unauthorized."}), 401)
    if request.content_length is not None and request.content_length > 48 * 1024 * 1024:
        return None, (jsonify({"error": "Request body too large."}), 413)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, (jsonify({"error": "A JSON object is required."}), 400)
    run_id = payload.get("worker_run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", run_id):
        return None, (jsonify({"error": "Invalid worker run ID."}), 400)
    endpoint = request.endpoint or "worker"
    now = time.monotonic()
    with _GPU_CALLBACK_RATE_LOCK:
        key = (run_id, endpoint)
        previous = _GPU_CALLBACK_TIMES.get(key, 0)
        minimum = 0.25 if endpoint.endswith("gpu_claim") else (0.5 if endpoint.endswith("gpu_heartbeat") else 0.0)
        if now - previous < minimum:
            return None, (jsonify({"error": "Rate limit exceeded."}), 429)
        _GPU_CALLBACK_TIMES[key] = now
    return payload, None


@app.route("/internal/gpu/worker/started", methods=["POST"])
def internal_gpu_worker_started():
    payload, error = _worker_payload()
    if error:
        return error
    if payload.get("kernel_id") != KAGGLE_KERNEL_ID:
        return jsonify({"error": "Kernel does not match active dispatch."}), 409
    try:
        started_at = float(payload.get("started_at"))
        idle_exit = int(payload.get("idle_exit_seconds"))
        max_runtime = int(payload.get("max_runtime_seconds"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid worker start configuration."}), 400
    if not 10 <= idle_exit <= 600 or not 300 <= max_runtime <= 14400:
        return jsonify({"error": "Worker runtime settings are outside allowed bounds."}), 400
    if started_at > time.time() + 60 or time.time() - started_at > 900:
        return jsonify({"error": "Stale worker start callback."}), 409
    if not auth_store.gpu_callback_started(payload["worker_run_id"], KAGGLE_KERNEL_ID, started_at):
        return jsonify({"error": "No matching active batch dispatch."}), 409
    return jsonify({"ok": True, "state": "running"}), 200


@app.route("/internal/gpu/worker/heartbeat", methods=["POST"])
def internal_gpu_worker_heartbeat():
    payload, error = _worker_payload()
    if error:
        return error
    if payload.get("state") not in {"idle", "processing"} or not isinstance(payload.get("gpu_available"), bool) or not isinstance(payload.get("engine_loaded"), bool):
        return jsonify({"error": "Invalid heartbeat."}), 400
    try:
        heartbeat_at = float(payload.get("heartbeat_at"))
        elapsed = float(payload.get("elapsed_seconds"))
        if abs(time.time() - heartbeat_at) > 900 or not 0 <= elapsed <= 86400:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid heartbeat timing."}), 400
    if not auth_store.gpu_callback_heartbeat(payload["worker_run_id"]):
        return jsonify({"error": "Stale worker run."}), 409
    return jsonify({"ok": True}), 200


@app.route("/internal/gpu/claim", methods=["POST"])
def internal_gpu_claim():
    payload, error = _worker_payload()
    if error:
        return error
    if payload.get("wait_for_request") is not False:
        return jsonify({"error": "wait_for_request must be false."}), 400
    supervisor = auth_store.gpu_supervisor_snapshot()
    if supervisor.get("worker_run_id") != payload["worker_run_id"] or supervisor.get("state") != "running":
        return jsonify({"error": "Stale worker run."}), 409
    row = auth_store.claim_gpu_request(payload["worker_run_id"], GPU_REQUEST_LEASE_SECONDS)
    if not row:
        return jsonify({"request": None, "queue_depth": auth_store.gpu_queue_depth()}), 200
    image_b64 = None
    reference_path = row.get("reference_image_path")
    if reference_path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(row["parent_task_id"])):
            return jsonify({"error": "Invalid task storage key."}), 409
        temp_root = TEMP_DIR.resolve()
        reference = Path(reference_path).resolve()
        allowed_dir = (temp_root / row["parent_task_id"] / "images").resolve()
        if temp_root not in allowed_dir.parents or allowed_dir not in reference.parents or not reference.is_file() or reference.stat().st_size > 24 * 1024 * 1024:
            return jsonify({"error": "Stored reference image is unavailable."}), 409
        image_b64 = base64.b64encode(reference.read_bytes()).decode("ascii")
    return jsonify({"request": {"request_id": row["request_id"], "prompt": row["prompt"],
        "width": row["width"], "height": row["height"], "seed": row["seed"],
        "strength": row["strength"], "image_b64": image_b64, "save_name": row["request_id"]},
        "queue_depth": auth_store.gpu_queue_depth()}), 200


@app.route("/internal/gpu/requests/<request_id>/complete", methods=["POST"])
def internal_gpu_complete(request_id):
    payload, error = _worker_payload()
    if error:
        return error
    if not re.fullmatch(r"[a-f0-9-]{36}", request_id):
        return jsonify({"error": "Invalid request ID."}), 400
    encoded = payload.get("image_b64")
    if not isinstance(encoded, str) or len(encoded) > (32 * 1024 * 1024 * 4 // 3 + 16):
        return jsonify({"error": "Invalid PNG payload."}), 400
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > 32 * 1024 * 1024:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.width > 2048 or image.height > 2048 or image.width * image.height > 4_194_304:
                raise ValueError()
            width, height = image.size
            image.verify()
    except (ValueError, OSError, UnidentifiedImageError, base64.binascii.Error):
        return jsonify({"error": "Invalid PNG payload."}), 400
    row = auth_store.get_gpu_request(request_id)
    if not row:
        return jsonify({"error": "GPU request not found."}), 404
    if row.get("worker_run_id") != payload["worker_run_id"]:
        return jsonify({"error": "Stale worker run."}), 409
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(row["parent_task_id"])):
        return jsonify({"error": "Invalid task storage key."}), 400
    temp_root = TEMP_DIR.resolve()
    target = Path(row["output_key"]).resolve()
    safe_root = (temp_root / row["parent_task_id"] / "images").resolve()
    if temp_root not in safe_root.parents or safe_root not in target.parents or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}\.png", target.name, re.IGNORECASE):
        return jsonify({"error": "Invalid output location."}), 400
    if width != int(row["width"]) or height != int(row["height"]) or payload.get("width") != width or payload.get("height") != height:
        return jsonify({"error": "PNG dimensions did not match request."}), 400
    if row["status"] == "completed":
        return jsonify({"ok": True, "status": "completed"}), 200
    supervisor = auth_store.gpu_supervisor_snapshot()
    if supervisor.get("worker_run_id") != payload["worker_run_id"]:
        return jsonify({"error": "Stale worker run."}), 409
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    try:
        partial.write_bytes(raw)
        partial.replace(target)
        storage_key = f"gpu/{int(row['user_id'])}/{row['parent_task_id']}/{request_id}.png"
        upload_file(storage_key, target, "image/png")
        image_path_for_db = f"supabase://{storage_key}"
        try:
            elapsed = max(0.0, min(86400.0, float(payload.get("elapsed_seconds", 0))))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid elapsed time."}), 400
        if not auth_store.complete_gpu_request(request_id, payload["worker_run_id"], image_path_for_db, width, height, elapsed):
            target.unlink(missing_ok=True)
            return jsonify({"error": "GPU request is not leased to this run."}), 409
    finally:
        partial.unlink(missing_ok=True)
    return jsonify({"ok": True, "status": "completed"}), 200


@app.route("/internal/gpu/requests/<request_id>/fail", methods=["POST"])
def internal_gpu_fail(request_id):
    payload, error = _worker_payload()
    if error:
        return error
    if not re.fullmatch(r"[a-f0-9-]{36}", request_id):
        return jsonify({"error": "Invalid request ID."}), 400
    supervisor = auth_store.gpu_supervisor_snapshot()
    if supervisor.get("worker_run_id") != payload["worker_run_id"]:
        return jsonify({"error": "Stale worker run."}), 409
    row = auth_store.get_gpu_request(request_id)
    if not row:
        return jsonify({"error": "GPU request not found."}), 404
    if row.get("worker_run_id") != payload["worker_run_id"]:
        return jsonify({"error": "Stale worker run."}), 409
    state = auth_store.fail_gpu_request(request_id, payload["worker_run_id"], "Image generation failed in the GPU worker.", GPU_REQUEST_MAX_ATTEMPTS)
    return jsonify({"ok": True, "status": state or row["status"]}), 200


@app.route("/internal/gpu/worker/exiting", methods=["POST"])
def internal_gpu_worker_exiting():
    payload, error = _worker_payload()
    if error:
        return error
    reason = payload.get("reason")
    if reason not in {"idle", "max_runtime", "flask_unreachable", "worker_error", "unknown"}:
        return jsonify({"error": "Invalid exit reason."}), 400
    try:
        elapsed = max(0.0, min(86400.0, float(payload.get("elapsed_seconds", 0))))
        finished_at = float(payload.get("finished_at"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid elapsed time."}), 400
    if abs(time.time() - finished_at) > 900:
        return jsonify({"error": "Stale exit callback."}), 409
    if not auth_store.gpu_callback_exiting(payload["worker_run_id"], reason, elapsed, GPU_REQUEST_MAX_ATTEMPTS):
        return jsonify({"error": "Stale worker run."}), 409
    gpu_dispatcher.notify()
    return jsonify({"ok": True}), 200


@app.route("/api/status/<job_id>", methods=["GET"])
@app.route("/status/<job_id>", methods=["GET"])
@login_required
def get_job_status(job_id: str):
    """Return only the signed-in owner's durable task state."""
    job = auth_store.owned_job(job_id, int(g.current_user["id"]))
    if not job:
        return jsonify({"error": f"Job ID {job_id} not found."}), 404

    result = copy.deepcopy(job)
    result["task_id"] = job_id
    result["position"] = _queue_position(job_id)
    result.pop("error_traceback", None)
    result.pop("image_path", None)
    result.pop("image_storage_key", None)
    result.pop("video_storage_key", None)
    if result.get("error"):
        result["error"] = "Generation failed. Please try again."
    if result.get("status") in {"FAILED", "failed"}:
        result["step_description"] = "Generation failed. Please try again."
    for scene in result.get("scenes", []):
        scene.pop("image_path", None)
        scene.pop("audio_path", None)
        scene.pop("image_storage_key", None)
        scene.pop("audio_storage_key", None)
    if WORKER_CALLBACK_TOKEN:
        result["logs"] = [str(line).replace(WORKER_CALLBACK_TOKEN, "[redacted]") for line in result.get("logs", [])]
    if request.path.startswith("/status/"):
        result["status"] = {
            "QUEUED": "queued",
            "RUNNING": "processing",
            "COMPLETED": "completed",
            "FAILED": "failed",
        }.get(job.get("status"), str(job.get("status", "unknown")).lower())
    if job.get("status") == "COMPLETED":
        # Video projects contain several rendered scene PNGs. Return the first
        # scene as the completed image preview alongside the full video_url.
        image_sources = [(job.get("image_path"), job.get("image_storage_key"))]
        image_sources.extend((scene.get("image_path"), scene.get("image_storage_key"))
                             for scene in job.get("scenes", []))
        for image_path, storage_key in image_sources:
            local_path = Path(image_path) if image_path else None
            if storage_key and (not local_path or not local_path.is_file()):
                scene_id = next((scene.get("scene_id") for scene in job.get("scenes", [])
                                 if scene.get("image_storage_key") == storage_key), "result")
                local_path = TEMP_DIR / job_id / "images" / f"scene_{int(scene_id):02d}.png" if isinstance(scene_id, int) else TEMP_DIR / job_id / "images" / "result.png"
                try:
                    download_file(storage_key, local_path)
                except Exception:
                    logger.exception("Could not restore stored image for %s", job_id)
                    continue
            if local_path and local_path.is_file():
                try:
                    from PIL import Image
                    with Image.open(local_path) as image:
                        result["width"], result["height"] = image.size
                    result["image_b64"] = base64.b64encode(local_path.read_bytes()).decode("ascii")
                    break
                except Exception:
                    logger.exception("Could not encode completed scene preview for %s", job_id)
    return jsonify(result), 200


def _queue_position(job_id: str) -> int | None:
    return auth_store.queue_position(job_id)


@app.route("/status/", methods=["GET"])
def queue_status():
    """Expose redacted service and queue health, never prompts or credentials."""
    stats = auth_store.queue_stats()
    gpu = auth_store.gpu_supervisor_snapshot()
    return jsonify({"status": "ok", **stats,
                    "worker_alive": bool(CONSUMER_THREAD and CONSUMER_THREAD.is_alive()),
                    "queue_limit": MAX_PENDING_JOBS,
                    "gpu_worker": {"state": gpu["state"], "queue_depth": gpu["queue_depth"],
                        "active_requests": gpu["active_requests"],
                        "unavailable": gpu["unavailable_reason"] or ("Autostart is disabled." if not KAGGLE_BATCH_AUTOSTART else None)}}), 200


@app.route("/healthz", methods=["GET"])
def health_check():
    """Render liveness check backed by the durable database connection."""
    auth_store.queue_stats()
    return jsonify({"status": "ok"}), 200


@app.route("/api/gpu/status", methods=["GET"])
@login_required
def api_gpu_status():
    """Operational metrics for signed-in workspace operators."""
    gpu = auth_store.gpu_supervisor_snapshot()
    return jsonify({"enabled": KAGGLE_BATCH_AUTOSTART, "state": gpu["state"],
        "active_run": gpu["active_dispatch_id"], "last_result": gpu["last_result"],
        "last_run": gpu["last_run"], "queue_depth": gpu["queue_depth"],
        "active_requests": gpu["active_requests"], "launches_24h": gpu["launches_24h"],
        "average_request_seconds": gpu["average_request_seconds"],
        "estimated_worker_seconds": gpu["estimated_worker_seconds"],
        "max_starts_per_day": KAGGLE_BATCH_MAX_STARTS_PER_DAY,
        "failed_runs_7d": gpu["failed_runs_7d"],
        "runtime_7d_seconds": gpu["runtime_7d_seconds"],
        "weekly_runtime_budget_seconds": KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS,
        "idle_exit_seconds": KAGGLE_BATCH_IDLE_EXIT_SECONDS,
        "max_runtime_seconds": KAGGLE_BATCH_MAX_RUNTIME_SECONDS,
        "unavailable_reason": gpu["unavailable_reason"]}), 200


@app.route("/api/check_worker", methods=["POST"])
@login_required
def api_check_worker():
    """Preserve the frontend check route with batch dispatcher health."""
    gpu = auth_store.gpu_supervisor_snapshot()
    ready = gpu["state"] in {"starting", "running", "exiting"} or gpu_dispatcher.can_accept_user_jobs()
    return jsonify({"online": bool(ready), "status": gpu["state"],
                    "error": gpu["unavailable_reason"] or (None if ready else "GPU worker unavailable.")}), 200


@app.route("/api/jobs", methods=["GET"])
@login_required
def list_jobs():
    """Lists summary of recent jobs."""
    job_summaries = _job_summaries(int(g.current_user["id"]))[:10]
    return jsonify({"jobs": job_summaries}), 200


@app.route("/exports/<job_id>/<path:filename>")
@login_required
def serve_export(job_id: str, filename: str):
    """Serves a finished MP4 only to the project owner."""
    job = auth_store.owned_job(job_id, int(g.current_user["id"]))
    if not job:
        return jsonify({"error": "Project not found."}), 404
    if job.get("video_url") != f"/exports/{job_id}/{filename}" or filename != f"video_{job_id}.mp4":
        return jsonify({"error": "Project file not found."}), 404
    if job.get("video_storage_key"):
        try:
            return redirect(create_signed_url(job["video_storage_key"], expires_in=3600), code=302)
        except Exception:
            logger.exception("Could not create private video URL for job %s", job_id)
            return jsonify({"error": "Video is temporarily unavailable."}), 503
    return jsonify({"error": "Video is not available in Supabase Storage."}), 404


@app.route("/temp/<job_id>/images/<path:filename>")
@login_required
def serve_temp_image(job_id: str, filename: str):
    """Serves real-time preview images during rendering."""
    job = auth_store.owned_job(job_id, int(g.current_user["id"]))
    if not job:
        return jsonify({"error": "Project not found."}), 404
    if not re.fullmatch(r"(?:scene_\d{2}|result)\.png", filename):
        return jsonify({"error": "Project file not found."}), 404
    target_dir = TEMP_DIR / job_id / "images"
    target = target_dir / filename
    if not target.is_file():
        scene = next((item for item in job.get("scenes", [])
                      if Path(str(item.get("image_path") or "")).name == filename), None)
        storage_key = (scene.get("image_storage_key") if scene else
                       job.get("image_storage_key") if filename == "result.png" else None)
        if storage_key:
            try:
                download_file(storage_key, target)
            except Exception:
                logger.exception("Could not restore stored preview image for %s", job_id)
    return send_from_directory(target_dir, filename)


@app.route("/temp/<job_id>/audio/<path:filename>")
@login_required
def serve_temp_audio(job_id: str, filename: str):
    """Serves scene audio files for preview."""
    job = auth_store.owned_job(job_id, int(g.current_user["id"]))
    if not job:
        return jsonify({"error": "Project not found."}), 404
    if not re.fullmatch(r"scene_\d{2}\.mp3", filename):
        return jsonify({"error": "Project file not found."}), 404
    target_dir = TEMP_DIR / job_id / "audio"
    target = target_dir / filename
    if not target.is_file():
        scene = next((item for item in job.get("scenes", [])
                      if Path(str(item.get("audio_path") or "")).name == filename), None)
        if scene and scene.get("audio_storage_key"):
            try:
                download_file(scene["audio_storage_key"], target)
            except Exception:
                logger.exception("Could not restore stored preview audio for %s", job_id)
    return send_from_directory(target_dir, filename, mimetype="audio/mpeg")


gpu_dispatcher.start()
_start_queue_worker()
