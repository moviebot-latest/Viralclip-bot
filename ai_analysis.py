"""
ai_analysis.py
- Transcribes audio with Groq's Whisper endpoint (word-level timestamps)
- Uses Groq's llama-3.3-70b-versatile to find high-value/viral segments
  with a virality score + reasoning + hook text
"""

import os
import json
import asyncio
from groq import Groq

client = Groq(api_key=os.environ["GROQ_API_KEY"])

WHISPER_MODEL = "whisper-large-v3"
LLM_MODEL = "llama-3.3-70b-versatile"


GROQ_FILE_SIZE_LIMIT = 24 * 1024 * 1024  # ~24MB (safety margin under Groq's 25MB ceiling)
CHUNK_SECONDS = 8 * 60  # 8-minute chunks — comfortably under the size limit at any reasonable bitrate


async def _get_duration(audio_path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def _call_groq_whisper(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read()
    result = client.audio.transcriptions.create(
        file=(os.path.basename(path), data),
        model=WHISPER_MODEL,
        response_format="verbose_json",
        timestamp_granularities=["word", "segment"],
    )
    return result.model_dump() if hasattr(result, "model_dump") else dict(result)


async def transcribe(audio_path: str) -> dict:
    """
    Returns a Groq verbose_json-style transcript with word-level timestamps.
    Groq auto-detects language; Hindi/English/mixed all supported.

    Guaranteed to work regardless of source video length:
    - If the audio file is under Groq's ~25MB request limit, transcribe in
      one shot (fast path, most videos).
    - If it's larger (long videos, e.g. 1-2hr), split into ~8-minute chunks,
      transcribe each chunk separately, then merge the results with
      corrected absolute timestamps — so downstream code (word-boundary
      snapping, clip analysis) sees one continuous transcript exactly as
      before, regardless of which path was taken.
    """
    loop = asyncio.get_event_loop()
    file_size = os.path.getsize(audio_path)

    if file_size <= GROQ_FILE_SIZE_LIMIT:
        return await loop.run_in_executor(None, _call_groq_whisper, audio_path)

    # --- chunked path ---
    duration = await _get_duration(audio_path)
    if duration <= 0:
        # Duration probe failed; fall back to a single lower-bitrate attempt
        # rather than chunking blind.
        compressed_path = audio_path.rsplit(".", 1)[0] + "_compressed.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", audio_path,
            "-ar", "16000", "-ac", "1", "-b:a", "32k", compressed_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        try:
            return await loop.run_in_executor(None, _call_groq_whisper, compressed_path)
        finally:
            if os.path.exists(compressed_path):
                os.remove(compressed_path)

    num_chunks = max(1, int(duration // CHUNK_SECONDS) + (1 if duration % CHUNK_SECONDS else 0))
    base_path = audio_path.rsplit(".", 1)[0]

    merged_text_parts = []
    merged_segments = []
    merged_words = []

    for i in range(num_chunks):
        chunk_start = i * CHUNK_SECONDS
        chunk_path = f"{base_path}_chunk{i}.mp3"

        # Compressed mono MP3 chunk — small and fast even for an 8-min segment.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(chunk_start), "-t", str(CHUNK_SECONDS),
            "-i", audio_path, "-ar", "16000", "-ac", "1", "-b:a", "64k", chunk_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        if not os.path.exists(chunk_path):
            continue

        try:
            chunk_result = await loop.run_in_executor(None, _call_groq_whisper, chunk_path)
        finally:
            if os.path.exists(chunk_path):
                os.remove(chunk_path)

        merged_text_parts.append(chunk_result.get("text", ""))
        for seg in chunk_result.get("segments", []):
            seg = dict(seg)
            seg["start"] = seg.get("start", 0) + chunk_start
            seg["end"] = seg.get("end", 0) + chunk_start
            merged_segments.append(seg)
        for w in chunk_result.get("words", []):
            w = dict(w)
            w["start"] = w.get("start", 0) + chunk_start
            w["end"] = w.get("end", 0) + chunk_start
            merged_words.append(w)

    return {
        "text": " ".join(merged_text_parts),
        "segments": merged_segments,
        "words": merged_words,
    }


def _build_analysis_prompt(transcript_text: str, segments: list, max_clips: int,
                            video_duration: float, platform: str = "both") -> str:
    segments_json = json.dumps(segments, ensure_ascii=False)

    platform_line = {
        "instagram": "Target platform: Instagram Reels. Prefer 15-60 second clips.",
        "youtube": "Target platform: YouTube Shorts. Prefer 15-60 second clips (up to 90s is fine).",
        "both": "Target platforms: Instagram Reels AND YouTube Shorts. Prefer 15-60 second clips.",
    }.get(platform, "Target platforms: Instagram Reels and YouTube Shorts. Prefer 15-60 second clips.")

    # If the source video itself is short (typical of reels/shorts already),
    # a strict 15-60s-per-clip rule can leave nothing selectable. Relax the
    # rule and allow the whole video (or most of it) as a single clip if it's
    # high value, instead of forcing artificial sub-cuts.
    if video_duration <= 75:
        length_rule = (
            f"This source video is only {video_duration:.0f} seconds long — shorter than "
            "a typical clip target. In this case, do NOT force artificial 15-60s cuts. "
            "Instead, evaluate the ENTIRE video (or the strongest continuous portion of it, "
            "trimming only dead air/intro filler at the very start or end) as ONE clip if it "
            "has genuine viral value. Return just 1 clip in that case. If the video has clearly "
            "distinct segments each with independent value, you may return more than 1, but do "
            "not invent boundaries that cut mid-thought just to hit a target length."
        )
    else:
        length_rule = (
            f"Select up to {max_clips} NON-OVERLAPPING segments, each a natural standalone "
            "15-60 second viral moment."
        )

    return f"""You are a viral short-form video editor analyzing a transcript to find
the best moments for standalone vertical clips (Reels/Shorts/TikTok).

Video duration: {video_duration:.0f} seconds.
{platform_line}

Full transcript segments with timestamps (start, end, text):
{segments_json}

Task: {length_rule}

Each clip must:
- Start and end on natural sentence boundaries (use the given timestamps)
- Contain a strong hook, emotional peak, controversial statement, punchline, or high-value insight
- Make sense without the rest of the video for context

Only return clips that are GENUINELY high-value. If truly nothing in this video has
viral potential, it is fine to return an empty array — do not force weak selections.

For each selected clip return JSON with:
- start_time (float, seconds)
- end_time (float, seconds)
- virality_score (0-100 integer)
- reasoning (short, why this clip is high value)
- hook_text (a punchy 3-6 word on-screen hook caption for the start of the clip)
- suggested_platform (one of: "Instagram Reels", "YouTube Shorts", "TikTok")

Respond ONLY with a JSON array of objects, no other text, no markdown fences."""


async def analyze_for_clips(transcript: dict, max_clips: int = 10,
                             video_duration: float = None, platform: str = "both") -> list:
    """
    Sends transcript segments to the LLM, gets back ranked, non-overlapping
    high-value clip candidates with reasoning.
    """
    segments = transcript.get("segments", [])
    simplified = [
        {"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
        for s in segments
    ]

    if video_duration is None:
        video_duration = segments[-1]["end"] if segments else 0

    prompt = _build_analysis_prompt(transcript.get("text", ""), simplified, max_clips,
                                     video_duration, platform)

    loop = asyncio.get_event_loop()

    def _call_llm():
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise JSON-only API. Never include prose outside the JSON array."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
        )
        return resp.choices[0].message.content

    raw = await loop.run_in_executor(None, _call_llm)

    # Defensive parsing: strip markdown fences if the model adds them anyway
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1)

    try:
        clips = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: try to locate the JSON array within the text
        start = cleaned.find("[")
        end = cleaned.rfind("]") + 1
        clips = json.loads(cleaned[start:end])

    # Remove overlaps defensively (in case LLM ignored the instruction)
    clips.sort(key=lambda c: c["start_time"])
    filtered = []
    last_end = -1
    for c in clips:
        if c["start_time"] >= last_end:
            filtered.append(c)
            last_end = c["end_time"]

    # Fallback 1: for a short source video (already reel-length), if the LLM
    # returned nothing at all, don't leave the user empty-handed — treat the
    # whole clip as one candidate.
    if not filtered and video_duration and video_duration <= 75 and segments:
        full_text_stripped = transcript.get("text", "").strip()
        if full_text_stripped:
            filtered = [{
                "start_time": segments[0]["start"],
                "end_time": segments[-1]["end"],
                "virality_score": 55,
                "reasoning": "Short source clip used as-is (below minimum segment length for sub-cutting).",
                "hook_text": "Watch this",
                "suggested_platform": "Instagram Reels" if platform == "instagram" else "YouTube Shorts",
            }]

    # Fallback 2: for ANY length video, if the LLM still returned nothing
    # (e.g. it was overly conservative about what counts as "viral"), retry
    # once with a more permissive prompt before giving up. This prevents
    # "0 clips" on perfectly normal videos where the model was just being
    # too strict on its first pass.
    if not filtered and segments:
        relaxed_prompt = _build_analysis_prompt(
            transcript.get("text", ""), simplified, max_clips, video_duration, platform
        ) + (
            "\n\nIMPORTANT: Your previous attempt returned zero clips. Be less strict this "
            "time — pick the most interesting or informative moments available, even if "
            "they're not spectacular. Every video has SOMETHING worth clipping. Return at "
            "least 1 clip unless the transcript is completely empty or silent."
        )

        def _call_llm_relaxed():
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "You are a precise JSON-only API. Never include prose outside the JSON array."},
                    {"role": "user", "content": relaxed_prompt},
                ],
                temperature=0.6,
            )
            return resp.choices[0].message.content

        raw2 = await loop.run_in_executor(None, _call_llm_relaxed)
        cleaned2 = raw2.strip()
        if cleaned2.startswith("```"):
            cleaned2 = cleaned2.strip("`").replace("json\n", "", 1)
        try:
            clips2 = json.loads(cleaned2)
        except json.JSONDecodeError:
            s, e = cleaned2.find("["), cleaned2.rfind("]") + 1
            try:
                clips2 = json.loads(cleaned2[s:e])
            except (json.JSONDecodeError, ValueError):
                clips2 = []

        clips2.sort(key=lambda c: c["start_time"])
        last_end2 = -1
        for c in clips2:
            if c["start_time"] >= last_end2:
                filtered.append(c)
                last_end2 = c["end_time"]

    return filtered[:max_clips]
