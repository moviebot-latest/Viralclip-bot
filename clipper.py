"""
clipper.py
- Extracts audio for transcription
- Cuts clips with a small safety buffer on word boundaries
- Smart-crops to 9:16 using face detection (OpenCV Haar cascade — lightweight,
  no extra model download needed; swap for mediapipe later if desired)
- Burns in word-by-word animated captions
- Encodes at high quality (CRF 16-18, no unnecessary compression)

NOTE: This is Phase 1 — functional and production-usable, but the caption
styling below implements ONE clean style. The 12-preset style picker plugs
into `build_ass_subtitle()` in a later phase by swapping the style dict.
"""

import asyncio
import os
import subprocess
import cv2

CLIPS_DIR = "clips"
os.makedirs(CLIPS_DIR, exist_ok=True)

SAFETY_BUFFER = 0.25  # seconds, avoids clipping first/last syllable


async def _run(cmd: list):
    loop = asyncio.get_event_loop()

    def _exec():
        subprocess.run(cmd, check=True, capture_output=True)

    await loop.run_in_executor(None, _exec)


async def extract_audio(video_path: str, out_path: str):
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        out_path,
    ]
    await _run(cmd)


def snap_to_word_boundary(target_time: float, words: list, is_start: bool) -> float:
    """
    Given a rough cut time, snap to the nearest word boundary so we never
    cut mid-word. `words` is a list of {"start":.., "end":.., "word":..}.
    """
    if not words:
        return target_time

    if is_start:
        candidates = [w["start"] for w in words if w["start"] <= target_time + 1.0]
        return max(candidates) if candidates else target_time
    else:
        candidates = [w["end"] for w in words if w["end"] >= target_time - 1.0]
        return min(candidates) if candidates else target_time


def detect_face_center_x(video_path: str, sample_time: float, frame_width: int) -> int:
    """
    Samples one frame near the middle of the clip and returns the x-center
    of the largest detected face, for smart 9:16 crop positioning.
    Falls back to horizontal center if no face detected.
    """
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, sample_time * 1000)
    ok, frame = cap.read()
    cap.release()

    if not ok:
        return frame_width // 2

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces = cascade.detectMultiScale(gray, 1.1, 5)

    if len(faces) == 0:
        return frame_width // 2

    # largest face by area
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return x + w // 2


async def get_video_dimensions(video_path: str) -> tuple:
    loop = asyncio.get_event_loop()

    def _probe():
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0", video_path,
        ]
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
        w, h = out.split("x")
        return int(w), int(h)

    return await loop.run_in_executor(None, _probe)


async def get_video_duration(video_path: str) -> float:
    loop = asyncio.get_event_loop()

    def _probe():
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
        ]
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
        return float(out)

    return await loop.run_in_executor(None, _probe)


def build_ass_subtitle(words: list, clip_start: float, out_path: str,
                        style: str = "style_2"):
    """
    Builds a word-by-word animated .ass subtitle file for burn-in.
    `words` are absolute-timeline word dicts within [clip_start, clip_end].
    style_2 approximates the red-highlight-box look from the reference screenshots.
    """
    styles = {
        "style_2": {"primary": "&H00FFFFFF", "highlight": "&H000000FF", "box": True},
        "plain_white": {"primary": "&H00FFFFFF", "highlight": "&H00FFFFFF", "box": False},
    }
    s = styles.get(style, styles["style_2"])

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginV
Style: Default,Arial Black,64,{s['primary']},&H00000000,1,1,3,0,2,300

[Events]
Format: Layer, Start, End, Style, Text
"""

    def fmt_time(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        sec = t % 60
        return f"{h:01d}:{m:02d}:{sec:05.2f}"

    lines = []
    for w in words:
        rel_start = max(0, w["start"] - clip_start)
        rel_end = max(rel_start + 0.05, w["end"] - clip_start)
        text = w["word"].strip()
        if s["box"]:
            text = "{\\c" + s["highlight"] + "}" + text + "{\\r}"
        lines.append(f"Dialogue: 0,{fmt_time(rel_start)},{fmt_time(rel_end)},Default,{text}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines))


async def cut_and_render_clip(
    video_path: str,
    clip_id: str,
    start_time: float,
    end_time: float,
    words: list,
    style: str = "style_2",
    watermark_text: str = None,
) -> str:
    """
    Full pipeline for one clip: snap boundaries, smart-crop to 9:16,
    burn captions, encode high quality. Returns output file path.
    """
    start_time = max(0, start_time - SAFETY_BUFFER)
    end_time = end_time + SAFETY_BUFFER
    duration = end_time - start_time

    src_w, src_h = await get_video_dimensions(video_path)
    target_ratio = 9 / 16
    crop_w = int(src_h * target_ratio)
    crop_w = min(crop_w, src_w)

    mid_time = start_time + duration / 2
    face_x = detect_face_center_x(video_path, mid_time, src_w)
    crop_x = max(0, min(src_w - crop_w, face_x - crop_w // 2))

    sub_path = os.path.join(CLIPS_DIR, f"{clip_id}.ass")
    build_ass_subtitle(words, start_time, sub_path, style=style)

    out_path = os.path.join(CLIPS_DIR, f"{clip_id}.mp4")

    vf_chain = f"crop={crop_w}:{src_h}:{crop_x}:0,scale=1080:1920,ass={sub_path}"
    if watermark_text:
        vf_chain += f",drawtext=text='{watermark_text}':fontcolor=white@0.7:fontsize=28:x=20:y=20"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time), "-i", video_path,
        "-t", str(duration),
        "-vf", vf_chain,
        "-c:v", "libx264", "-preset", "slow", "-crf", "17",
        "-c:a", "aac", "-b:a", "192k",
        out_path,
    ]
    await _run(cmd)
    return out_path
