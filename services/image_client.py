"""Flask-local durable GPU client with an optional synchronous debug transport."""

import base64
import binascii
import io
import logging
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from config import (
    IMAGE_WORKER_CONNECT_TIMEOUT,
    IMAGE_WORKER_MAX_RETRIES,
    IMAGE_WORKER_READ_TIMEOUT,
    IMAGE_WORKER_RETRY_BASE_SECONDS,
    KAGGLE_WORKER_TOKEN,
    KAGGLE_WORKER_URL,
)

logger = logging.getLogger(__name__)
TRANSIENT_HTTP_STATUSES = {429, 502, 503, 504}
MAX_IMAGE_BYTES = 32 * 1024 * 1024
_batch_request_manager = None


def configure_batch_request_manager(manager) -> None:
    """Bind Flask's local durable request manager; no network server is needed."""
    global _batch_request_manager
    _batch_request_manager = manager


class WorkerUnavailableError(RuntimeError):
    """A transient worker connectivity or availability failure."""


class WorkerRequestError(RuntimeError):
    """A permanent worker protocol, authorization, or validation failure."""


def _headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "FlaskVideoControlPlane/2.0",
    }
    if KAGGLE_WORKER_TOKEN:
        headers["X-Worker-Token"] = KAGGLE_WORKER_TOKEN
    return headers


def check_worker_health(worker_url: str | None = None, timeout: int | None = None) -> dict[str, Any]:
    """Check the protected Kaggle /health endpoint without returning secrets."""
    target = (worker_url if worker_url is not None else KAGGLE_WORKER_URL).strip().rstrip("/")
    if not target:
        return {"online": False, "error": "Image worker URL is not configured."}
    if not KAGGLE_WORKER_TOKEN:
        return {"online": False, "error": "Image worker token is not configured."}

    connect_timeout = min(float(timeout or IMAGE_WORKER_CONNECT_TIMEOUT), 10.0)
    read_timeout = min(float(timeout or IMAGE_WORKER_CONNECT_TIMEOUT), 10.0)
    started = time.monotonic()
    try:
        response = requests.get(f"{target}/health", headers=_headers(),
                                timeout=(connect_timeout, read_timeout))
        elapsed = round((time.monotonic() - started) * 1000, 1)
        if response.status_code in TRANSIENT_HTTP_STATUSES:
            return {"online": False, "error": f"Image worker temporarily unavailable ({response.status_code}).", "latency_ms": elapsed}
        if response.status_code in (401, 403):
            return {"online": False, "error": "Image worker authentication failed.", "latency_ms": elapsed}
        if response.status_code != 200:
            return {"online": False, "error": f"Image worker health check failed ({response.status_code}).", "latency_ms": elapsed}
        data = response.json()
        if not isinstance(data, dict) or str(data.get("status", "")).lower() not in {"ok", "online", "healthy"}:
            return {"online": False, "error": "Image worker returned an invalid health response.", "latency_ms": elapsed}
        return {"online": True, "status": "online", "vram": str(data.get("vram", "Unknown"))[:80], "latency_ms": elapsed}
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return {"online": False, "error": "Image worker did not respond.", "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except (requests.exceptions.RequestException, ValueError):
        return {"online": False, "error": "Image worker health check failed."}


def fetch_scene_image(
    worker_url: str | None,
    prompt: str,
    width: int,
    height: int,
    save_path: str | Path,
    seed: int = 42,
    strength: float = 1.0,
    timeout: int | None = None,
    max_retries: int | None = None,
    parent_task_id: str | None = None,
    user_id: int | None = None,
) -> str:
    """Use Flask's durable GPU queue; optional legacy HTTP mode is debug-only."""
    target = (worker_url if worker_url is not None else KAGGLE_WORKER_URL).strip().rstrip("/")
    out_file = Path(save_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if parent_task_id and user_id is not None:
        if _batch_request_manager is None:
            raise WorkerUnavailableError("The local GPU request manager is not configured.")
        return _batch_request_manager.fetch_scene_image(parent_task_id, user_id, prompt,
            width, height, out_file, seed, strength)
    if not target:
        raise WorkerUnavailableError("Image worker URL is not configured.")
    if not KAGGLE_WORKER_TOKEN:
        raise WorkerRequestError("Image worker token is not configured.")

    payload = {"prompt": prompt, "width": int(width), "height": int(height),
               "seed": int(seed), "strength": float(strength), "save_name": out_file.stem}
    retries = max(1, min(5, int(max_retries or IMAGE_WORKER_MAX_RETRIES)))
    timeout_pair = (IMAGE_WORKER_CONNECT_TIMEOUT, float(timeout or IMAGE_WORKER_READ_TIMEOUT))
    endpoint = f"{target}/generate"
    last_reason = "Image worker did not respond."

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(endpoint, headers=_headers(), json=payload, timeout=timeout_pair)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            last_reason = "Image worker did not respond."
            if attempt < retries:
                time.sleep(IMAGE_WORKER_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
                continue
            raise WorkerUnavailableError(last_reason)
        except requests.exceptions.RequestException:
            raise WorkerUnavailableError("Image worker request failed.")

        if response.status_code in TRANSIENT_HTTP_STATUSES:
            last_reason = f"Image worker temporarily unavailable ({response.status_code})."
            if attempt < retries:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(30.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = IMAGE_WORKER_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                time.sleep(delay)
                continue
            raise WorkerUnavailableError(last_reason)
        if response.status_code in (401, 403):
            raise WorkerRequestError("Image worker authentication failed.")
        if response.status_code in (400, 422):
            raise WorkerRequestError("Image worker rejected the generation request.")
        if response.status_code != 200:
            raise WorkerRequestError(f"Image worker returned HTTP {response.status_code}.")

        try:
            data = response.json()
            if not isinstance(data, dict) or str(data.get("status", "")).lower() != "completed":
                raise ValueError("invalid status")
            encoded = data.get("image_b64")
            if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
                raise ValueError("invalid image data")
            image_bytes = base64.b64decode(encoded, validate=True)
            if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
                raise ValueError("invalid image size")
            with Image.open(io.BytesIO(image_bytes)) as image:
                if image.format != "PNG":
                    raise ValueError("image is not PNG")
                image.verify()
            with Image.open(io.BytesIO(image_bytes)) as image:
                actual_width, actual_height = image.size
                if actual_width != width or actual_height != height:
                    raise ValueError("image dimensions do not match request")
                if int(data.get("width", -1)) != actual_width or int(data.get("height", -1)) != actual_height:
                    raise ValueError("response dimensions do not match PNG")
            temp_file = out_file.with_name(out_file.name + ".partial")
            try:
                temp_file.write_bytes(image_bytes)
                temp_file.replace(out_file)
            finally:
                # Remove only this request's exact temporary file; never walk
                # directories or unlink paths supplied by the remote worker.
                if temp_file.exists():
                    temp_file.unlink()
            return str(out_file)
        except (ValueError, TypeError, KeyError, binascii.Error, OSError) as exc:
            raise WorkerRequestError("Image worker returned an invalid PNG response.") from exc

    raise WorkerUnavailableError(last_reason)
