"""Database-backed runtime settings with encrypted storage for provider credentials."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from config import (
    SETTINGS_ENCRYPTION_KEY,
    DEEPGRAM_API_KEY, LLM_PROVIDER, DEFAULT_LLM_MODEL, LLM_FALLBACKS,
    OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, CUSTOM_LLM_API_KEY,
    CUSTOM_LLM_BASE_URL, DEFAULT_TTS_VOICE, DEFAULT_FPS, CROSSFADE_BUFFER,
    TARGET_SCENE_DURATION_SEC, KAGGLE_BATCH_AUTOSTART, KAGGLE_API_TOKEN,
    KAGGLE_KERNEL_ID, KAGGLE_BATCH_ACCELERATOR, KAGGLE_BATCH_TIMEOUT_SECONDS,
    KAGGLE_BATCH_DEBOUNCE_SECONDS, KAGGLE_BATCH_IDLE_EXIT_SECONDS,
    KAGGLE_BATCH_MAX_RUNTIME_SECONDS, KAGGLE_BATCH_MAX_STARTS_PER_DAY,
    KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS, KAGGLE_BATCH_POLL_SECONDS,
    GPU_REQUEST_MAX_PENDING, GPU_REQUEST_LEASE_SECONDS, GPU_REQUEST_MAX_ATTEMPTS,
    WORKER_IDLE_EXIT_SECONDS, WORKER_MAX_RUNTIME_SECONDS,
    WORKER_POLL_INTERVAL_SECONDS, WORKER_HTTP_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, Any] = {
    "LLM_PROVIDER": LLM_PROVIDER,
    "DEFAULT_LLM_MODEL": DEFAULT_LLM_MODEL,
    "LLM_FALLBACKS": LLM_FALLBACKS,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "GROQ_API_KEY": GROQ_API_KEY,
    "CUSTOM_LLM_API_KEY": CUSTOM_LLM_API_KEY,
    "CUSTOM_LLM_BASE_URL": CUSTOM_LLM_BASE_URL,
    "DEEPGRAM_API_KEY": DEEPGRAM_API_KEY,
    "DEFAULT_TTS_VOICE": DEFAULT_TTS_VOICE,
    "DEFAULT_FPS": DEFAULT_FPS,
    "CROSSFADE_BUFFER": CROSSFADE_BUFFER,
    "TARGET_SCENE_DURATION_SEC": TARGET_SCENE_DURATION_SEC,
    "KAGGLE_BATCH_AUTOSTART": KAGGLE_BATCH_AUTOSTART,
    "KAGGLE_API_TOKEN": KAGGLE_API_TOKEN,
    "KAGGLE_KERNEL_ID": KAGGLE_KERNEL_ID,
    "KAGGLE_BATCH_ACCELERATOR": KAGGLE_BATCH_ACCELERATOR,
    "KAGGLE_BATCH_TIMEOUT_SECONDS": KAGGLE_BATCH_TIMEOUT_SECONDS,
    "KAGGLE_BATCH_DEBOUNCE_SECONDS": KAGGLE_BATCH_DEBOUNCE_SECONDS,
    "KAGGLE_BATCH_IDLE_EXIT_SECONDS": KAGGLE_BATCH_IDLE_EXIT_SECONDS,
    "KAGGLE_BATCH_MAX_RUNTIME_SECONDS": KAGGLE_BATCH_MAX_RUNTIME_SECONDS,
    "KAGGLE_BATCH_MAX_STARTS_PER_DAY": KAGGLE_BATCH_MAX_STARTS_PER_DAY,
    "KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS": KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS,
    "KAGGLE_BATCH_POLL_SECONDS": KAGGLE_BATCH_POLL_SECONDS,
    "WORKER_IDLE_EXIT_SECONDS": WORKER_IDLE_EXIT_SECONDS,
    "WORKER_MAX_RUNTIME_SECONDS": WORKER_MAX_RUNTIME_SECONDS,
    "WORKER_POLL_INTERVAL_SECONDS": WORKER_POLL_INTERVAL_SECONDS,
    "WORKER_HTTP_TIMEOUT_SECONDS": WORKER_HTTP_TIMEOUT_SECONDS,
    "GPU_REQUEST_MAX_PENDING": GPU_REQUEST_MAX_PENDING,
    "GPU_REQUEST_LEASE_SECONDS": GPU_REQUEST_LEASE_SECONDS,
    "GPU_REQUEST_MAX_ATTEMPTS": GPU_REQUEST_MAX_ATTEMPTS,
}
SECRET_SETTINGS = {
    "OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "CUSTOM_LLM_API_KEY",
    "DEEPGRAM_API_KEY", "KAGGLE_API_TOKEN",
}

_store = None
_lock = threading.RLock()
_cache_values: dict[str, Any] = dict(DEFAULT_SETTINGS)
_cache_rows: dict[str, dict[str, Any]] = {}
_cache_until = 0.0
_cache_seconds = 2.0
_cipher = Fernet(SETTINGS_ENCRYPTION_KEY.encode("ascii"))


def configure(store) -> None:
    global _store, _cache_until
    with _lock:
        _store = store
        _cache_until = 0.0


def _snapshot(force: bool = False) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    global _cache_values, _cache_rows, _cache_until
    with _lock:
        now = time.monotonic()
        if not force and now < _cache_until:
            return dict(_cache_values), dict(_cache_rows)
        values = dict(DEFAULT_SETTINGS)
        rows = _store.get_app_settings() if _store is not None else {}
        for key, row in rows.items():
            try:
                if row.get("is_secret"):
                    raw = _cipher.decrypt(row["value"].encode("ascii")).decode("utf-8")
                    values[key] = raw
                else:
                    values[key] = json.loads(row["value"])
            except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                logger.error("Could not read encrypted runtime setting %s; check SETTINGS_ENCRYPTION_KEY.", key)
                values[key] = "" if row.get("is_secret") else DEFAULT_SETTINGS.get(key)
        _cache_values, _cache_rows = values, rows
        _cache_until = now + _cache_seconds
        return dict(values), dict(rows)


def get(name: str, default: Any = None) -> Any:
    values, _ = _snapshot()
    return values.get(name, default)


def all_values() -> dict[str, Any]:
    return _snapshot()[0]


def source(name: str) -> str:
    _, rows = _snapshot()
    return "Admin panel" if name in rows else "Render environment"


def secret_configured(name: str) -> bool:
    return bool(str(get(name, "") or "").strip())


def save(values: dict[str, Any], updated_by: int) -> None:
    if _store is None:
        raise RuntimeError("Runtime settings storage is unavailable.")
    prepared: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        if key in SECRET_SETTINGS:
            prepared[key] = {
                "value": _cipher.encrypt(str(value).encode("utf-8")).decode("ascii"),
                "is_secret": True,
            }
        else:
            prepared[key] = {"value": json.dumps(value, separators=(",", ":")), "is_secret": False}
    _store.save_app_settings(prepared, updated_by)
    with _lock:
        global _cache_until
        _cache_until = 0.0


def reset(names: list[str]) -> None:
    if _store is None:
        raise RuntimeError("Runtime settings storage is unavailable.")
    _store.delete_app_settings(names)
    with _lock:
        global _cache_until
        _cache_until = 0.0
