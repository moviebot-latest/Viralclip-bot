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


GROQ_FILE_SIZE_LIMIT = 25 * 1024 * 1024  # ~25MB, Groq's request size ceiling


async def transcribe(audio_path: str) -> dict:
    """
    Returns Groq verbose_json transcript with word-level timestamps.
    Groq auto-detects language; Hindi/English/mixed all supported.

    If the audio file exceeds Groq's ~25MB request limit (can happen on
    long videos even with FLAC), we re-compress to a lower-bitrate mono MP3
    before retrying, rather than failing with a 413 error.
    """
    loop = asyncio.get_event_loop()

    def _read_and_call(path):
        with open(path, "rb") as f:
            data = f.read()
        result = client.audio.transcriptions.create(
            file=(os.path.basename(path), data),
            model=WHISPER_MODEL,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    file_size = os.path.getsize(audio_path)
    if file_size <= GROQ_FILE_SIZE_LIMIT:
        return await loop.run_in_executor(None, _read_and_call, audio_path)

    # Too large even as FLAC — re-compress to low-bitrate mono MP3 (32kbps is
    # plenty for speech-to-text) and retry once.
    compressed_path = audio_path.rsplit(".", 1)[0] + "_compressed.mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", audio_path,
        "-ar", "16000", "-ac", "1", "-b:a", "32k", compressed_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    try:
        return await loop.run_in_executor(None, _read_and_call, compressed_path)
    finally:
        if os.path.exists(compressed_path):
            os.remove(compressed_path)


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

    # Fallback: for a short source video (already reel-length), if the LLM
    # returned nothing at all, don't leave the user empty-handed — treat the
    # whole clip as one candidate. This only fires for short sources, so it
    # can't accidentally dump a full 1-hour video as "one clip".
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

    return filtered[:max_clips]
