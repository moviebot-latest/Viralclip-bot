"""
downloader.py
Wraps yt-dlp for:
  1. Cheap metadata probe (no download) -> used for pre-validation
  2. Full best-quality download
Runs yt-dlp in a thread executor so it never blocks the asyncio loop.
"""

import asyncio
import os
import hashlib
import yt_dlp

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


async def probe_metadata(url: str) -> dict:
    """
    Fast metadata-only fetch (no video download).
    Used for pre-download validation: duration, title, description.
    """
    loop = asyncio.get_event_loop()

    def _probe():
        ydl_opts = {"quiet": True, "skip_download": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", ""),
                "description": info.get("description", "") or "",
                "duration": info.get("duration", 0) or 0,
                "id": info.get("id", ""),
            }

    return await loop.run_in_executor(None, _probe)


async def download_best_quality(url: str, job_id: str) -> str:
    """
    Downloads best available video+audio quality.
    Returns local file path.
    """
    loop = asyncio.get_event_loop()
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    def _download():
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # merge_output_format forces mp4 extension
            base, _ = os.path.splitext(filename)
            mp4_path = base + ".mp4"
            return mp4_path if os.path.exists(mp4_path) else filename

    return await loop.run_in_executor(None, _download)
