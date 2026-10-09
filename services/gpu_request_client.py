"""Local wait/notification client for the durable Flask-to-Kaggle GPU queue."""
import time
import uuid
import hashlib
from pathlib import Path

from config import GPU_REQUEST_MAX_PENDING
from services.image_client import WorkerUnavailableError, WorkerRequestError
from services.object_storage import download_file


class GpuQueueFull(RuntimeError):
    pass


class KaggleBatchGpuClient:
    def __init__(self, store, dispatcher=None, poll_seconds=1.0):
        self.store, self.dispatcher, self.poll_seconds = store, dispatcher, poll_seconds

    def enqueue_batch(self, parent_task_id, user_id, requests):
        rows = []
        for item in requests:
            request_id = item.get("request_id") or str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"fluxstory:{parent_task_id}:{Path(item['save_path']).name}"))
            path = Path(item["save_path"]).resolve()
            if path.parent.name != "images" or path.parent.parent.name != str(parent_task_id) or path.suffix.lower() != ".png":
                raise ValueError("GPU image output must be inside the parent task images directory")
            reference_path = Path(item["reference_image_path"]).resolve() if item.get("reference_image_path") else None
            if reference_path and (reference_path.parent != path.parent or not reference_path.is_file()):
                raise ValueError("Reference image must be an existing file in the parent task images directory")
            rows.append({"request_id": request_id, "parent_task_id": parent_task_id,
                         "user_id": int(user_id), "request_type": item.get("request_type", "image"),
                         "prompt": item["prompt"], "width": int(item["width"]),
                         "height": int(item["height"]), "seed": item.get("seed"),
                         "strength": float(item.get("strength", 1.0)),
                         "reference_image_path": str(reference_path) if reference_path else None,
                         "output_key": str(path)})
        try:
            self.store.enqueue_gpu_requests(rows, GPU_REQUEST_MAX_PENDING)
        except OverflowError as exc:
            raise WorkerUnavailableError("The GPU request queue is full; retry shortly.") from exc
        if self.dispatcher:
            self.dispatcher.notify()
        return rows

    def wait_for(self, request_id, timeout=None):
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            row = self.store.get_gpu_request(request_id)
            if not row:
                raise RuntimeError("GPU request is unavailable.")
            if row["status"] == "completed":
                path = Path(row["image_path"] or row["output_key"])
                if str(row.get("image_path") or "").startswith("supabase://"):
                    key = str(row["image_path"])[len("supabase://"):]
                    download_file(key, row["output_key"])
                    path = Path(row["output_key"])
                if not path.is_file():
                    raise RuntimeError("GPU image output is missing.")
                return str(path)
            if row["status"] == "failed":
                raise WorkerRequestError("GPU image generation failed after retry attempts.")
            if deadline and time.monotonic() >= deadline:
                raise TimeoutError("GPU image request is still queued; retrying the user job is safe.")
            time.sleep(self.poll_seconds)

    def fetch_scene_image(self, parent_task_id, user_id, prompt, width, height, save_path, seed=42, strength=1.0):
        row = self.enqueue_batch(parent_task_id, user_id, [{"prompt": prompt, "width": width,
            "height": height, "save_path": save_path, "seed": seed, "strength": strength}])[0]
        return self.wait_for(row["request_id"])
