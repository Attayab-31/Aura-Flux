"""
services/llm_director.py
=============================================================================
LLM Director Service: Narrative Decomposition, Timing & Visual Consistency
=============================================================================
Deconstructs high-level story scripts into structured scene manifests with
timed voiceover transcripts, cinematic visual prompts, motion behaviors,
and fluid video transitions.
"""

import json
import logging
import re
from typing import List, Dict, Any, Optional

from config import LLM_PROVIDER_SETTINGS, LLM_PROVIDER, DEFAULT_LLM_MODEL, LLM_FALLBACKS, STYLE_PRESETS, TARGET_SCENE_DURATION_SEC

logger = logging.getLogger(__name__)

# Valid choices for transitions and camera motion
VALID_TRANSITIONS = {"crossfade", "fade_black", "none"}
VALID_MOTIONS = {"zoom_in", "zoom_out", "pan_left", "pan_right"}


def _build_system_prompt(target_scene_count: int, style_anchor: str, target_scene_duration: float) -> str:
    """Builds a strict JSON-enforcing director prompt with visual consistency anchoring."""
    min_words = max(4, round(target_scene_duration * 1.8))
    max_words = max(min_words + 2, round(target_scene_duration * 2.6))
    return f"""You are an elite Hollywood Film Director and AI Video Producer.
Your mission is to decompose a narrative story into a structured cinematic scene manifest for automated video synthesis.

Target Scene Count: Exactly {target_scene_count} scenes.
Target Scene Duration: ~{target_scene_duration:.1f} seconds of natural voiceover per scene.
Visual Consistency Anchor: "{style_anchor}"

INSTRUCTIONS:
1. Divide the narrative chronologically into {target_scene_count} coherent narrative beats.
2. For each scene, create:
   - "scene_id": Integer sequence (1, 2, 3, ...)
   - "narration_text": The exact script spoken by the narrator for this scene. Pace it for about {target_scene_duration:.1f} seconds (roughly {min_words}-{max_words} words per scene), while preserving the complete story and natural delivery.
   - "visual_prompt": An exceptionally vivid, photorealistic visual prompt for FLUX diffusion. Include the visual consistency anchor, explicit camera framing (e.g. wide shot, medium close-up, dramatic low-angle), specific lighting (e.g. volumetric god-rays, rim lighting, twilight golden hour), specific setting elements, character appearance consistency, and mood. Avoid generic text.
   - "transition_type": Transition to the NEXT scene. Choose strictly from: "crossfade", "fade_black", or "none". (Default to "crossfade" for fluid storytelling; use "fade_black" for dramatic breaks; "none" for the final scene).
   - "motion_type": Ken Burns camera movement. Choose strictly from: "zoom_in", "zoom_out", "pan_left", "pan_right". Alternate dynamically across scenes.

RETURN ONLY VALID JSON WITH THE EXACT STRUCTURE:
{{
  "visual_style_summary": "Brief summary of consistent style",
  "scenes": [
    {{
      "scene_id": 1,
      "narration_text": "...",
      "visual_prompt": "...",
      "transition_type": "crossfade",
      "motion_type": "zoom_in"
    }}
  ]
}}
Do NOT include Markdown wrappers like ```json or additional chatter outside the JSON object.
"""


def _sanitize_manifest(raw_scenes: List[Dict[str, Any]], target_count: int, style_anchor: str) -> List[Dict[str, Any]]:
    """Validates and sanitizes scene dictionaries to ensure complete pipeline compliance."""
    sanitized: List[Dict[str, Any]] = []
    motion_cycle = ["zoom_in", "pan_right", "zoom_out", "pan_left"]

    for idx, sc in enumerate(raw_scenes, start=1):
        # Validate or fallback narration
        narr = str(sc.get("narration_text", "")).strip()
        if not narr:
            narr = f"Scene {idx} narrative."

        # Validate or enrich visual prompt
        v_prompt = str(sc.get("visual_prompt", "")).strip()
        if not v_prompt:
            v_prompt = f"{narr}. {style_anchor}"
        elif style_anchor and style_anchor.lower() not in v_prompt.lower():
            v_prompt = f"{v_prompt}, {style_anchor}"

        # Transition validation
        trans = str(sc.get("transition_type", "crossfade")).lower()
        if trans not in VALID_TRANSITIONS:
            trans = "crossfade"
        if idx == len(raw_scenes):
            trans = "none"  # Final scene has no outgoing transition

        # Motion validation
        motion = str(sc.get("motion_type", "")).lower()
        if motion not in VALID_MOTIONS:
            motion = motion_cycle[(idx - 1) % len(motion_cycle)]

        sanitized.append({
            "scene_id": idx,
            "narration_text": narr,
            "visual_prompt": v_prompt,
            "transition_type": trans,
            "motion_type": motion
        })

    return sanitized


def _rule_based_fallback_director(
    story_text: str,
    target_scene_count: int,
    style_anchor: str
) -> List[Dict[str, Any]]:
    """
    Intelligent heuristic fallback when LLM API keys are unavailable or rate-limited.
    Splits text by sentence boundaries into the required scene count.
    """
    logger.info("Executing rule-based fallback narrative decomposition...")
    cleaned = re.sub(r'\s+', ' ', story_text).strip()
    # Split by sentence end punctuation
    sentences = [s.strip() for s in re.split(r'(?<=[.!?]) +', cleaned) if s.strip()]

    if not sentences:
        sentences = [story_text or "In a cinematic world of wonder and discovery."]

    # Group sentences into target_scene_count buckets
    total_s = len(sentences)
    chunk_size = max(1, total_s // target_scene_count)
    scene_texts = []

    for i in range(target_scene_count):
        start_idx = i * chunk_size
        if i == target_scene_count - 1:
            chunk = sentences[start_idx:]
        else:
            chunk = sentences[start_idx:start_idx + chunk_size]
        if chunk:
            scene_texts.append(" ".join(chunk))

    if not scene_texts:
        scene_texts = sentences[:target_scene_count]

    # Ensure we match target_scene_count
    while len(scene_texts) < target_scene_count:
        scene_texts.append(f"The story continues with profound depth and momentum (part {len(scene_texts) + 1}).")

    motion_cycle = ["zoom_in", "pan_right", "zoom_out", "pan_left"]
    manifest = []

    for idx, text in enumerate(scene_texts, start=1):
        motion = motion_cycle[(idx - 1) % len(motion_cycle)]
        trans = "crossfade" if idx < len(scene_texts) else "none"
        prompt = (
            f"Cinematic masterpiece depicting: {text[:140]}. "
            f"{style_anchor}, atmospheric lighting, 8k resolution, highly detailed, masterwork composition."
        )
        manifest.append({
            "scene_id": idx,
            "narration_text": text,
            "visual_prompt": prompt,
            "transition_type": trans,
            "motion_type": motion
        })

    return manifest


def plan_narrative(
    story_text: str,
    duration_minutes: float,
    api_key: Optional[str] = None,
    model: str = DEFAULT_LLM_MODEL,
    style_preset: Optional[str] = "cinematic",
    provider: Optional[str] = None,
    target_scene_count: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Decomposes the input story into a list of timed, styled scene objects.

    Parameters:
        story_text: The user's input narrative or script.
        duration_minutes: Desired total video run time in minutes.
        api_key: OpenAI API key (optional; falls back to config or heuristic).
        model: LLM model name (default: gpt-4o-mini).
        style_preset: Visual style preset key from STYLE_PRESETS.

    Returns:
        List of scene dictionaries containing scene_id, narration_text,
        visual_prompt, transition_type, and motion_type.
    """
    if not story_text or not story_text.strip():
        raise ValueError("Story text cannot be empty.")

    total_seconds = max(15.0, float(duration_minutes) * 60.0)
    target_scene_count = target_scene_count or min(60, max(2, int(round(total_seconds / TARGET_SCENE_DURATION_SEC))))
    if target_scene_count < 2 or target_scene_count > 60:
        raise ValueError("Image count must be between 2 and 60.")
    target_scene_duration = total_seconds / target_scene_count

    # Resolve visual style anchor
    preset_data = STYLE_PRESETS.get(style_preset or "cinematic", STYLE_PRESETS["cinematic"])
    style_anchor = preset_data["prompt_anchor"]

    selected_provider = (provider or LLM_PROVIDER).lower()
    attempts = [(selected_provider, model or DEFAULT_LLM_MODEL)]
    for entry in LLM_FALLBACKS.split(","):
        provider_name, separator, fallback_model = entry.strip().partition(":")
        if separator and provider_name.strip() and fallback_model.strip():
            attempts.append((provider_name.strip().lower(), fallback_model.strip()))

    # Preserve order while avoiding accidental repeated calls to the same model.
    attempts = list(dict.fromkeys(attempts))
    system_prompt = _build_system_prompt(target_scene_count, style_anchor, target_scene_duration)
    user_prompt = (
        f"STORY SCRIPT:\n\"\"\"{story_text}\"\"\"\n\n"
        f"Target Video Length: {duration_minutes:.2f} minutes ({int(total_seconds)} seconds).\n"
        f"Generate exactly {target_scene_count} scenes following the system instructions."
    )

    for index, (attempt_provider, attempt_model) in enumerate(attempts):
        provider_config = LLM_PROVIDER_SETTINGS.get(attempt_provider)
        if provider_config is None:
            logger.warning("Skipping unknown LLM provider '%s'.", attempt_provider)
            continue
        if attempt_provider == "custom" and not provider_config["base_url"]:
            logger.info("Skipping custom LLM %s: CUSTOM_LLM_BASE_URL is not configured.", attempt_model)
            continue
        effective_api_key = api_key if index == 0 and api_key else provider_config["api_key"]
        if not effective_api_key:
            logger.info("Skipping LLM %s (%s): no API key configured.", attempt_provider, attempt_model)
            continue

        try:
            from openai import OpenAI
            client_options = {"api_key": effective_api_key}
            if provider_config["base_url"]:
                client_options["base_url"] = provider_config["base_url"]
            client = OpenAI(**client_options)
            response = client.chat.completions.create(
                model=attempt_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=4000
            )
            raw_content = response.choices[0].message.content
            data = json.loads(raw_content or "")
            if isinstance(data, list):
                raw_scenes = data
            elif isinstance(data, dict):
                raw_scenes = data.get("scenes", [])
            else:
                raise ValueError("LLM response must be a JSON object or array.")
            if not raw_scenes:
                raise ValueError("LLM returned empty scene list.")
            if not isinstance(raw_scenes, list) or len(raw_scenes) != target_scene_count:
                actual_count = len(raw_scenes) if isinstance(raw_scenes, list) else "invalid"
                raise ValueError(f"LLM returned {actual_count} scenes; expected exactly {target_scene_count}.")

            sanitized = _sanitize_manifest(raw_scenes, target_scene_count, style_anchor)
            logger.info("Successfully planned %d scenes using %s (%s)", len(sanitized), attempt_provider, attempt_model)
            return sanitized
        except Exception as exc:
            logger.warning(
                "LLM attempt %s (%s) failed: %s%s",
                attempt_provider,
                attempt_model,
                exc,
                "; trying next configured model" if index < len(attempts) - 1 else "; no further configured model"
            )

    logger.warning("All configured LLM attempts failed or were skipped; using rule-based director.")
    return _rule_based_fallback_director(story_text, target_scene_count, style_anchor)
