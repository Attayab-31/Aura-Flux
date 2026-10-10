"""Client for Flask's durable Kaggle GPU request queue."""
from pathlib import Path

_batch_request_manager = None


def configure_batch_request_manager(manager) -> None:
    """Bind the app's durable GPU request manager; no worker web server is needed."""
    global _batch_request_manager
    _batch_request_manager = manager


class WorkerUnavailableError(RuntimeError):
    """A transient worker connectivity or availability failure."""


class WorkerRequestError(RuntimeError):
    """A permanent worker protocol, authorization, or validation failure."""


def fetch_scene_image(
    prompt: str,
    width: int,
    height: int,
    save_path: str | Path,
    seed: int = 42,
    strength: float = 1.0,
    parent_task_id: str | None = None,
    user_id: int | None = None,
) -> str:
    """Submit an image request to Flask's durable Kaggle GPU queue."""
    out_file = Path(save_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if not parent_task_id or user_id is None:
        raise ValueError("A parent task and user are required for a queued image request.")
    if _batch_request_manager is None:
        raise WorkerUnavailableError("The GPU request manager is not configured.")
    return _batch_request_manager.fetch_scene_image(parent_task_id, user_id, prompt,
        width, height, out_file, seed, strength)
