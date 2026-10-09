"""
config.py
=============================================================================
Central Configuration and Path Management for AI Video Generation Control Plane
=============================================================================
"""

import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Base project paths
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
TEMP_DIR = BASE_DIR / "temp"
_persistent_data_env = os.getenv("PERSISTENT_DATA_DIR", "").strip()
INSTANCE_DIR = Path(_persistent_data_env or (BASE_DIR / "instance")).resolve()
TEMP_DIR = Path(os.getenv("PERSISTENT_TEMP_DIR", str(INSTANCE_DIR / "temp" if _persistent_data_env else BASE_DIR / "temp"))).resolve()
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

# Private, durable project storage (served only through authenticated routes).
EXPORTS_DIR = INSTANCE_DIR / "exports"
LEGACY_EXPORTS_DIR = STATIC_DIR / "exports"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
if LEGACY_EXPORTS_DIR.is_dir():
    for legacy_export in LEGACY_EXPORTS_DIR.glob("video_*.mp4"):
        private_export = EXPORTS_DIR / legacy_export.name
        if not private_export.exists():
            shutil.move(str(legacy_export), str(private_export))
TEMP_AUDIO_DIR = TEMP_DIR / "audio"
TEMP_IMAGES_DIR = TEMP_DIR / "images"
TEMP_CLIPS_DIR = TEMP_DIR / "clips"

# Ensure all critical workspace directories exist
for directory in (EXPORTS_DIR, TEMP_AUDIO_DIR, TEMP_IMAGES_DIR, TEMP_CLIPS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# Flask application settings
SECRET_KEY = os.getenv("SECRET_KEY", "")
if len(SECRET_KEY) < 32 or SECRET_KEY.startswith("change-this"):
    raise RuntimeError("Set SECRET_KEY to a random value of at least 32 characters in the process environment or .env.")
MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MB

# API Defaults and Environment Keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CUSTOM_LLM_API_KEY = os.getenv("CUSTOM_LLM_API_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
CUSTOM_LLM_BASE_URL = os.getenv("CUSTOM_LLM_BASE_URL", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
KAGGLE_WORKER_URL = os.getenv("KAGGLE_WORKER_URL", "").rstrip("/")
KAGGLE_WORKER_TOKEN = os.getenv("KAGGLE_WORKER_TOKEN", "").strip()
IMAGE_WORKER_CONNECT_TIMEOUT = float(os.getenv("IMAGE_WORKER_CONNECT_TIMEOUT", "5"))
IMAGE_WORKER_READ_TIMEOUT = float(os.getenv("IMAGE_WORKER_READ_TIMEOUT", "180"))
IMAGE_WORKER_MAX_RETRIES = max(1, min(5, int(os.getenv("IMAGE_WORKER_MAX_RETRIES", "3"))))
IMAGE_WORKER_RETRY_BASE_SECONDS = max(0.1, float(os.getenv("IMAGE_WORKER_RETRY_BASE_SECONDS", "2")))

# Finite Kaggle batch runner. Autostart is deliberately opt-in.
KAGGLE_BATCH_AUTOSTART = os.getenv("KAGGLE_BATCH_AUTOSTART", "false").lower() == "true"
KAGGLE_API_TOKEN = os.getenv("KAGGLE_API_TOKEN", "").strip()
KAGGLE_USERNAME = os.getenv("KAGGLE_USERNAME", "").strip()
KAGGLE_KEY = os.getenv("KAGGLE_KEY", "").strip()
KAGGLE_KERNEL_ID = os.getenv("KAGGLE_KERNEL_ID", "owner/fluxstory-on-demand-worker").strip()
KAGGLE_KERNEL_PATH = Path(os.getenv("KAGGLE_KERNEL_PATH", str(BASE_DIR / "workers" / "kaggle_fluxstory"))).resolve()
KAGGLE_BATCH_ACCELERATOR = os.getenv("KAGGLE_BATCH_ACCELERATOR", "NvidiaTeslaT4").strip()
KAGGLE_BATCH_TIMEOUT_SECONDS = max(300, min(14400, int(os.getenv("KAGGLE_BATCH_TIMEOUT_SECONDS", "14400"))))
KAGGLE_BATCH_DEBOUNCE_SECONDS = max(0, min(120, float(os.getenv("KAGGLE_BATCH_DEBOUNCE_SECONDS", "10"))))
KAGGLE_BATCH_IDLE_EXIT_SECONDS = max(10, min(600, int(os.getenv("KAGGLE_BATCH_IDLE_EXIT_SECONDS", "45"))))
KAGGLE_BATCH_MAX_RUNTIME_SECONDS = max(300, min(14400, int(os.getenv("KAGGLE_BATCH_MAX_RUNTIME_SECONDS", "14400"))))
KAGGLE_BATCH_MAX_STARTS_PER_DAY = max(0, int(os.getenv("KAGGLE_BATCH_MAX_STARTS_PER_DAY", "6")))
KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS = max(0, int(os.getenv("KAGGLE_BATCH_MAX_RUNTIME_PER_WEEK_SECONDS", "0")))
KAGGLE_BATCH_POLL_SECONDS = max(15, min(300, int(os.getenv("KAGGLE_BATCH_POLL_SECONDS", "60"))))
WORKER_CALLBACK_TOKEN = os.getenv("WORKER_CALLBACK_TOKEN", "").strip()
GPU_REQUEST_MAX_PENDING = max(1, min(1000, int(os.getenv("GPU_REQUEST_MAX_PENDING", "500"))))
GPU_REQUEST_LEASE_SECONDS = max(60, int(os.getenv("GPU_REQUEST_LEASE_SECONDS", "900")))
GPU_REQUEST_MAX_ATTEMPTS = max(1, min(5, int(os.getenv("GPU_REQUEST_MAX_ATTEMPTS", "3"))))

# AI & Generation Configuration
DEFAULT_LLM_MODEL = os.getenv("DEFAULT_LLM_MODEL", "gpt-4o-mini")
LLM_FALLBACKS = os.getenv("LLM_FALLBACKS", "")
LLM_PROVIDER_SETTINGS = {
    "openai": {"api_key": OPENAI_API_KEY, "base_url": ""},
    "gemini": {"api_key": GEMINI_API_KEY, "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
    "groq": {"api_key": GROQ_API_KEY, "base_url": "https://api.groq.com/openai/v1"},
    "custom": {"api_key": CUSTOM_LLM_API_KEY, "base_url": CUSTOM_LLM_BASE_URL},
}
DEFAULT_TTS_VOICE = os.getenv("DEFAULT_TTS_VOICE", "aura-2-thalia-en")
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "30"))
CROSSFADE_BUFFER = float(os.getenv("CROSSFADE_BUFFER", "1.0"))  # Seconds overlap for smooth transitions
TARGET_SCENE_DURATION_SEC = 7.0  # Ideal target length per scene (6-8s)

# H.264 / FLUX Resolution Profiles (Always snapped to multiples of 16)
RESOLUTION_PROFILES = {
    "16:9": {
        "name": "Landscape (16:9)",
        "width": 1024,
        "height": 576,
        "description": "Standard widescreen for YouTube, Vimeo, and Desktop playback"
    },
    "9:16": {
        "name": "Portrait (9:16)",
        "width": 576,
        "height": 1024,
        "description": "Vertical format optimized for TikTok, Reels, and YouTube Shorts"
    },
    "1:1": {
        "name": "Square (1:1)",
        "width": 768,
        "height": 768,
        "description": "Square format optimized for Instagram feeds and social carousels"
    },
    "16:9_HD": {
        "name": "HD Landscape (16:9)",
        "width": 1280,
        "height": 720,
        "description": "High definition 720p widescreen"
    }
}

# Available Deepgram Aura Voices
AURA_VOICES = [
    {"id": "aura-2-thalia-en", "name": "Thalia (Aura 2 - Clear, Expressive, Narrative)", "gender": "Female"},
    {"id": "aura-2-orion-en", "name": "Orion (Aura 2 - Deep, Authoritative, Cinematic)", "gender": "Male"},
    {"id": "aura-asteria-en", "name": "Asteria (Aura 1 - Warm, Engaging)", "gender": "Female"},
    {"id": "aura-luna-en", "name": "Luna (Aura 1 - Soft, Pleasant)", "gender": "Female"},
    {"id": "aura-stella-en", "name": "Stella (Aura 1 - Professional, Balanced)", "gender": "Female"},
    {"id": "aura-athena-en", "name": "Athena (Aura 1 - Confident, Formal)", "gender": "Female"},
    {"id": "aura-hera-en", "name": "Hera (Aura 1 - Mature, Resonant)", "gender": "Female"},
    {"id": "aura-orion-en", "name": "Orion (Aura 1 - Calm, Resonant)", "gender": "Male"},
    {"id": "aura-arcas-en", "name": "Arcas (Aura 1 - Crisp, Energetic)", "gender": "Male"},
    {"id": "aura-perseus-en", "name": "Perseus (Aura 1 - Grounded, Conversational)", "gender": "Male"},
    {"id": "aura-angus-en", "name": "Angus (Aura 1 - Deep, Classic Irish cadence)", "gender": "Male"},
]

VOICE_PREVIEW_TEXT = (
    "Beneath the quiet canopy, a small light appeared between the trees. "
    "By morning, everyone in the village had heard the story."
)

# Visual Style Consistency Anchors
STYLE_PRESETS = {
    "cinematic": {
        "label": "Cinematic Photorealism (Default)",
        "prompt_anchor": "cinematic film still, 35mm photograph, masterfully lit, photorealistic, 8k resolution, highly detailed texture, dramatic lighting, anamorphic lens blur, depth of field"
    },
    "cyberpunk": {
        "label": "Cyberpunk Neon Noir",
        "prompt_anchor": "cyberpunk noir aesthetic, volumetric neon lighting, reflective rain puddles, high tech dystopian atmosphere, sharp focus, octane render style"
    },
    "dark_fantasy": {
        "label": "Dark Fantasy & Mythic",
        "prompt_anchor": "dark fantasy oil painting, dramatic chiaroscuro lighting, moody atmospheric haze, intricate baroque details, epic fantasy concept art"
    },
    "anime_ghibli": {
        "label": "Studio Anime & Ghibli",
        "prompt_anchor": "beautiful anime scenic art, vibrant hand-painted aesthetic, studio ghibli inspired, lush colors, soft sunlight, nostalgic atmosphere"
    },
    "documentary": {
        "label": "National Geographic Documentary",
        "prompt_anchor": "award-winning national geographic documentary photograph, ultra-realistic natural lighting, hyper-detailed, telephoto lens, candid clarity"
    },
    "vintage_film": {
        "label": "Vintage 1970s Kodachrome",
        "prompt_anchor": "authentic 1970s kodachrome film photograph, warm color palette, subtle analog film grain, vintage camera aesthetic, nostalgic natural lighting"
    }
}


def snap16(val: int) -> int:
    """Snaps any integer dimension to the nearest multiple of 16 (required for H.264/FLUX)."""
    return max(16, (int(val) // 16) * 16)


def get_resolution(aspect_ratio: str) -> tuple[int, int]:
    """Resolves aspect ratio key to validated (width, height) tuple."""
    profile = RESOLUTION_PROFILES.get(aspect_ratio, RESOLUTION_PROFILES["16:9"])
    w = snap16(profile["width"])
    h = snap16(profile["height"])
    return (w, h)
