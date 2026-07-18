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


async def transcribe(audio_path: str) -> dict:
    """
    Returns Groq verbose_json transcript with word-level timestamps.
    Groq auto-detects language; Hindi/English/mixed all supported.
    """
    loop = asyncio.get_event_loop()

    def _transcribe():
        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(
                file=(os.path.basename(audio_path), f.read()),
                model=WHISPER_MODEL,
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )
        return result.model_dump() if hasattr(result, "model_dump") else dict(result)

    return await loop.run_in_executor(None, _transcribe)


def _build_analysis_prompt(transcript_text: str, segments: list, max_clips: int) -> str:
    segments_json = json.dumps(segments, ensure_ascii=False)
    return f"""You are a viral short-form video editor analyzing a transcript to find
the best moments for standalone vertical clips (Reels/Shorts/TikTok).

Full transcript segments with timestamps (start, end, text):
{segments_json}

Task: Select up to {max_clips} NON-OVERLAPPING segments that would work best as
standalone 15-60 second viral clips. Each clip must:
- Start and end on natural sentence boundaries (use the given timestamps)
- Contain a strong hook, emotional peak, controversial statement, punchline, or high-value insight
- Make sense without the rest of the video for context

For each selected clip return JSON with:
- start_time (float, seconds)
- end_time (float, seconds)
- virality_score (0-100 integer)
- reasoning (short, why this clip is high value)
- hook_text (a punchy 3-6 word on-screen hook caption for the start of the clip)
- suggested_platform (one of: "Instagram Reels", "YouTube Shorts", "TikTok")

Respond ONLY with a JSON array of objects, no other text, no markdown fences."""


async def analyze_for_clips(transcript: dict, max_clips: int = 10) -> list:
    """
    Sends transcript segments to the LLM, gets back ranked, non-overlapping
    high-value clip candidates with reasoning.
    """
    segments = transcript.get("segments", [])
    simplified = [
        {"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
        for s in segments
    ]
    full_text = transcript.get("text", "")

    prompt = _build_analysis_prompt(full_text, simplified, max_clips)

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

    return filtered[:max_clips]
