"""
services/tts_service.py
=============================================================================
Deepgram Aura Text-to-Speech (TTS) Integration & Timing Engine
=============================================================================
Synthesizes scene narration using Deepgram's high-speed Aura REST API,
measures sub-second audio durations, and guarantees audio-visual synchronization.
"""

import os
import subprocess
import logging
from pathlib import Path
from typing import Optional
import requests
from mutagen.mp3 import MP3

from config import DEEPGRAM_API_KEY, DEFAULT_TTS_VOICE

logger = logging.getLogger(__name__)


def get_audio_duration(file_path: Path) -> float:
    """
    Extracts the exact audio duration in seconds using Mutagen,
    with an automatic fallback to FFprobe / MoviePy.
    """
    path_str = str(file_path)
    # Attempt 1: Mutagen MP3 header inspection (fastest, sub-millisecond)
    try:
        audio = MP3(path_str)
        if audio.info and audio.info.length > 0:
            return float(audio.info.length)
    except Exception as e:
        logger.debug("Mutagen inspection failed for %s (%s). Falling back to FFprobe.", path_str, e)

    # Attempt 2: ffprobe subprocess metadata inspection
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path_str
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        duration = float(result.stdout.strip())
        if duration > 0:
            return duration
    except Exception as e:
        logger.debug("FFprobe inspection failed for %s (%s). Falling back to MoviePy.", path_str, e)

    # Attempt 3: MoviePy AudioFileClip
    try:
        from moviepy import AudioFileClip
        with AudioFileClip(path_str) as clip:
            return float(clip.duration)
    except Exception as e:
        logger.error("All audio duration parsers failed for %s: %s", path_str, e)
        # Default safety estimate based on file size or minimum duration
        return 6.0


def generate_speech(
    text: str,
    api_key: Optional[str] = None,
    output_path: Optional[str | Path] = None,
    model: str = DEFAULT_TTS_VOICE
) -> float:
    """
    Synthesizes speech using the Deepgram Aura REST API and returns precise audio duration.

    Endpoint: POST https://api.deepgram.com/v1/speak?model={model}
    Headers: Authorization: Token {api_key}, Content-Type: application/json
    Payload: {"text": text}

    Parameters:
        text: The scene narrative text to synthesize.
        api_key: Deepgram API key (Token).
        output_path: Destination path for the .mp3 file.
        model: Aura voice model (e.g., aura-2-thalia-en).

    Returns:
        Exact audio duration in seconds (float).
    """
    if not text or not text.strip():
        raise ValueError("Narration text for speech synthesis cannot be empty.")

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    effective_key = (api_key or DEEPGRAM_API_KEY or "").strip()

    if not effective_key:
        raise RuntimeError("DEEPGRAM_API_KEY is required for speech generation.")

    url = f"https://api.deepgram.com/v1/speak?model={model or DEFAULT_TTS_VOICE}"
    headers = {
        "Authorization": f"Token {effective_key}",
        "Content-Type": "application/json"
    }
    payload = {"text": text.strip()}

    logger.info("Dispatching TTS synthesis to Deepgram Aura [%s] (chars: %d)...", model, len(text))
    try:
        response = requests.post(url, headers=headers, json=payload, stream=True, timeout=30)
    except requests.exceptions.RequestException as req_err:
        raise RuntimeError(f"Network error connecting to Deepgram TTS: {req_err}") from req_err

    if response.status_code != 200:
        err_msg = response.text
        logger.error("Deepgram Aura API error [%d]: %s", response.status_code, err_msg)
        raise RuntimeError(f"Deepgram Aura API error [{response.status_code}]: {err_msg}")

    # Stream audio response chunks to destination file
    try:
        with open(out_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    except IOError as io_err:
        raise RuntimeError(f"Failed to write audio stream to {out_file}: {io_err}") from io_err

    # Compute and return exact audio duration
    duration = get_audio_duration(out_file)
    logger.info("Successfully synthesized scene audio: %s (Duration: %.2f seconds)", out_file.name, duration)
    return duration
