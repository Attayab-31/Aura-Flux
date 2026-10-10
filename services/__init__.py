"""
services package initialization.
Exposes core pipeline services for narrative decomposition, speech synthesis,
generative visual acquisition, and video composition.
"""

from .llm_director import plan_narrative
from .tts_service import generate_speech
from .image_client import fetch_scene_image
from .video_composer import assemble_video, create_ken_burns_clip

__all__ = [
    "plan_narrative",
    "generate_speech",
    "fetch_scene_image",
    "assemble_video",
    "create_ken_burns_clip"
]
