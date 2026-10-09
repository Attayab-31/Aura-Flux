"""Create fixed voice previews for local static serving.

Run once after configuring DEEPGRAM_API_KEY in .env. Style preview artwork is
bundled in static/previews/styles and does not need a worker or API request.
Existing assets are left untouched; deleting a file before rerunning rebuilds it.
"""

from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from config import (
    AURA_VOICES,
    DEEPGRAM_API_KEY,
    VOICE_PREVIEW_TEXT,
)
from services.tts_service import generate_speech


PREVIEW_DIR = PROJECT_DIR / "static" / "previews"
VOICE_DIR = PREVIEW_DIR / "voices"


def main() -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    missing_voices = [
        voice for voice in AURA_VOICES
        if not (VOICE_DIR / f"{voice['id']}.mp3").exists()
        or (VOICE_DIR / f"{voice['id']}.mp3").stat().st_size == 0
    ]
    if not missing_voices:
        print(f"All {len(AURA_VOICES)} fixed voice samples are already available in {VOICE_DIR}")
        return
    if not DEEPGRAM_API_KEY:
        raise SystemExit("Set a working DEEPGRAM_API_KEY in .env to create missing voice previews.")

    for voice in missing_voices:
        target = VOICE_DIR / f"{voice['id']}.mp3"
        print(f"Creating fixed voice sample: {target.name}")
        generate_speech(VOICE_PREVIEW_TEXT, api_key=DEEPGRAM_API_KEY, output_path=target, model=voice["id"])

    print(f"Preview assets are ready in {PREVIEW_DIR}")


if __name__ == "__main__":
    main()
