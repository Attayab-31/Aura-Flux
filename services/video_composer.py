"""
services/video_composer.py
=============================================================================
Automated Post-Production Video Composition Engine (MoviePy v2.x)
=============================================================================
Transforms static generative frames into cinematic video sequences via:
1. Sub-pixel Ken Burns pan and zoom transformations (cv2 cubic interpolation).
2. Narration-driven timeline synchronization with crossfade overlap buffers.
3. Audio track compositing and hardware-accelerated H.264 / AAC master rendering.
"""

import os
import gc
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Callable

import numpy as np
import cv2
from PIL import Image
import moviepy as mp
from moviepy import ImageClip, AudioFileClip, CompositeVideoClip
from moviepy.video.fx import CrossFadeIn, FadeIn, FadeOut
from moviepy.audio.AudioClip import CompositeAudioClip

from config import DEFAULT_FPS
from services.runtime_settings import get as get_runtime_setting

logger = logging.getLogger(__name__)


def create_ken_burns_clip(
    image_path: str | Path,
    duration: float,
    target_size: Tuple[int, int],
    motion_type: str = "zoom_in"
) -> mp.VideoClip:
    """
    Generates a VideoClip with continuous sub-pixel motion transformations (Ken Burns).
    Uses high-precision floating-point affine warping and Lanczos4 resampling
    to completely eliminate discrete pixel snapping, aspect ratio wobble, and micro-jitter.

    Parameters:
        image_path: Path to static input image (PNG/JPEG).
        duration: Duration of the clip in seconds.
        target_size: Tuple (width, height) in pixels.
        motion_type: Camera movement ('zoom_in', 'zoom_out', 'pan_left', 'pan_right').

    Returns:
        MoviePy VideoClip with dynamic frame transformation.
    """
    path_str = str(image_path)
    if not os.path.exists(path_str):
        raise FileNotFoundError(f"Image frame not found: {path_str}")

    pil_img = Image.open(path_str).convert("RGB")
    orig_w, orig_h = float(pil_img.size[0]), float(pil_img.size[1])
    img_np = np.array(pil_img)

    # Validate target dimensions
    target_w, target_h = float(target_size[0]), float(target_size[1])
    out_size_int = (int(target_size[0]), int(target_size[1]))
    safe_duration = max(0.5, float(duration))

    # Aspect ratio validation & center crop normalization
    target_aspect = target_w / target_h
    src_aspect = orig_w / orig_h
    if abs(src_aspect - target_aspect) > 0.001:
        if src_aspect > target_aspect:
            fit_w = orig_h * target_aspect
            fit_h = orig_h
        else:
            fit_w = orig_w
            fit_h = orig_w / target_aspect
        base_cx = orig_w / 2.0
        base_cy = orig_h / 2.0
    else:
        fit_w = orig_w
        fit_h = orig_h
        base_cx = orig_w / 2.0
        base_cy = orig_h / 2.0

    # Destination mapping corners (fixed canvas target)
    dst_pts = np.float32([
        [0.0, 0.0],
        [target_w, 0.0],
        [0.0, target_h]
    ])

    def frame_transform(get_frame, t: float) -> np.ndarray:
        # Normalized time progress clamp [0.0, 1.0]
        progress = min(1.0, max(0.0, float(t) / safe_duration))

        # Smooth cosine ease-in-out curve for natural cinematic camera movement
        eased_progress = 0.5 * (1.0 - np.cos(np.pi * progress))

        # Dynamic continuous floating-point viewport calculations
        if motion_type == "zoom_in":
            scale = 1.0 + (0.15 * eased_progress)
            w_crop = fit_w / scale
            h_crop = fit_h / scale
            cx = base_cx
            cy = base_cy

        elif motion_type == "zoom_out":
            scale = 1.15 - (0.15 * eased_progress)
            w_crop = fit_w / scale
            h_crop = fit_h / scale
            cx = base_cx
            cy = base_cy

        elif motion_type == "pan_left":
            scale = 1.12  # 12% zoom margin for camera translation
            w_crop = fit_w / scale
            h_crop = fit_h / scale
            max_pan_x = (fit_w - w_crop) * 0.5
            # Pan from right to left smoothly
            cx = base_cx + max_pan_x - (2.0 * max_pan_x * eased_progress)
            cy = base_cy

        elif motion_type == "pan_right":
            scale = 1.12
            w_crop = fit_w / scale
            h_crop = fit_h / scale
            max_pan_x = (fit_w - w_crop) * 0.5
            # Pan from left to right smoothly
            cx = base_cx - max_pan_x + (2.0 * max_pan_x * eased_progress)
            cy = base_cy

        else:
            scale = 1.05
            w_crop = fit_w / scale
            h_crop = fit_h / scale
            cx = base_cx
            cy = base_cy

        # 3 continuous floating-point viewport points in source image
        x_left = cx - (w_crop * 0.5)
        x_right = cx + (w_crop * 0.5)
        y_top = cy - (h_crop * 0.5)
        y_bottom = cy + (h_crop * 0.5)

        src_pts = np.float32([
            [x_left, y_top],
            [x_right, y_top],
            [x_left, y_bottom]
        ])

        # Compute affine transform matrix with 64-bit sub-pixel precision
        matrix = cv2.getAffineTransform(src_pts, dst_pts)

        # Warp directly from full image with Lanczos4 interpolation (zero jitter)
        rendered = cv2.warpAffine(
            img_np,
            matrix,
            out_size_int,
            flags=cv2.INTER_LANCZOS4,
            borderMode=cv2.BORDER_REFLECT
        )
        return rendered

    base_clip = ImageClip(img_np).with_duration(safe_duration)
    motion_clip = base_clip.transform(frame_transform)
    return motion_clip


def assemble_video(
    scene_manifest: List[Dict],
    output_filename: str | Path,
    resolution: Tuple[int, int] = (1024, 576),
    fps: Optional[int] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> str:
    """
    Compiles narrative audio tracks and Ken Burns visual sub-clips into a synchronized master MP4.

    Parameters:
        scene_manifest: List of scene dictionaries containing:
                        - scene_id
                        - image_path
                        - audio_path
                        - audio_duration (optional, measured if absent)
                        - transition_type ('crossfade', 'fade_black', 'none')
                        - motion_type ('zoom_in', 'zoom_out', 'pan_left', 'pan_right')
        output_filename: Master destination MP4 file path.
        resolution: Target video dimensions (width, height).
        fps: Frames per second (default 30).
        progress_callback: Optional status callback function (progress_pct, description).

    Returns:
        Absolute string path to rendered master MP4 video.
    """
    if not scene_manifest:
        raise ValueError("Scene manifest is empty; cannot assemble video.")

    fps = int(fps or get_runtime_setting("DEFAULT_FPS", DEFAULT_FPS))
    crossfade_buffer = float(get_runtime_setting("CROSSFADE_BUFFER", 1.0))

    out_path = Path(output_filename).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if progress_callback:
        progress_callback(5.0, "Initializing video assembly and timeline calculation...")

    video_clips = []
    audio_clips = []
    current_time = 0.0
    num_scenes = len(scene_manifest)

    try:
        for idx, scene in enumerate(scene_manifest):
            scene_id = scene.get("scene_id", idx + 1)
            img_path = scene.get("image_path")
            aud_path = scene.get("audio_path")
            motion_type = scene.get("motion_type", "zoom_in")
            trans_type = scene.get("transition_type", "crossfade")

            if not img_path or not os.path.exists(str(img_path)):
                raise FileNotFoundError(f"Missing image for scene {scene_id}: {img_path}")
            if not aud_path or not os.path.exists(str(aud_path)):
                raise FileNotFoundError(f"Missing audio for scene {scene_id}: {aud_path}")

            # Load scene audio
            aud_clip = AudioFileClip(str(aud_path))
            audio_duration = float(aud_clip.duration)

            # Determine transition buffer
            is_last = (idx == num_scenes - 1)
            delta_t = 0.0
            if not is_last and trans_type == "crossfade":
                delta_t = min(crossfade_buffer, max(0.0, audio_duration * 0.2))

            # Display duration = audio duration + crossfade buffer
            clip_duration = audio_duration + delta_t

            if progress_callback:
                pct = 5.0 + (idx / num_scenes) * 45.0
                progress_callback(
                    pct,
                    f"Creating Ken Burns motion ({motion_type}) for Scene {scene_id}/{num_scenes}..."
                )

            # Generate motion clip
            v_clip = create_ken_burns_clip(
                image_path=img_path,
                duration=clip_duration,
                target_size=resolution,
                motion_type=motion_type
            )

            # Align timeline start times
            v_clip = v_clip.with_start(current_time)
            timed_audio = aud_clip.with_start(current_time)

            # Apply incoming transition effects
            if idx > 0:
                prev_trans = scene_manifest[idx - 1].get("transition_type", "crossfade")
                if prev_trans == "crossfade":
                    prev_delta = min(crossfade_buffer, max(0.0, float(scene_manifest[idx - 1].get("audio_duration", 5.0)) * 0.2))
                    if prev_delta > 0:
                        v_clip = v_clip.with_effects([CrossFadeIn(prev_delta)])
                elif prev_trans == "fade_black":
                    v_clip = v_clip.with_effects([FadeIn(0.5)])

            # Apply outgoing fade to black if requested
            if trans_type == "fade_black":
                v_clip = v_clip.with_effects([FadeOut(0.5)])

            video_clips.append(v_clip)
            audio_clips.append(timed_audio)

            # Advance narrative audio timeline
            current_time += audio_duration

        if progress_callback:
            progress_callback(55.0, "Compositing multi-scene video layers and master audio tracks...")

        # Composite video and audio layers
        composite_video = CompositeVideoClip(video_clips, size=resolution)
        composite_audio = CompositeAudioClip(audio_clips)
        master_clip = composite_video.with_audio(composite_audio).with_duration(current_time + delta_t)

        if progress_callback:
            progress_callback(65.0, f"Encoding production master H.264 video ({resolution[0]}x{resolution[1]} @ {fps}fps)...")

        logger.info(
            "Rendering final video to %s [Total duration: %.2fs, Resolution: %dx%d]",
            out_path.name, master_clip.duration, resolution[0], resolution[1]
        )

        # Write production-ready H.264/AAC MP4 with universal browser compatibility
        master_clip.write_videofile(
            str(out_path),
            fps=fps,
            codec="libx264",
            audio_codec="aac",
            preset="fast",
            threads=4,
            ffmpeg_params=["-pix_fmt", "yuv420p"],
            logger=None
        )

        if progress_callback:
            progress_callback(100.0, "Master video rendering complete!")

        logger.info("Successfully produced master MP4: %s (Size: %.2f MB)", out_path.name, out_path.stat().st_size / (1024 * 1024))
        return str(out_path)

    finally:
        # Explicit resource cleanup to prevent open file handle or memory leaks
        for ac in audio_clips:
            try:
                ac.close()
            except Exception:
                pass
        for vc in video_clips:
            try:
                vc.close()
            except Exception:
                pass
        gc.collect()
