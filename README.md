# ViralClip Bot — Phase 1

Telegram bot: link/video bhejo → AI se best 10 viral clips (hooks + captions + smart crop) nikaal ke bhejta hai.

## Setup

1. `.env.example` ko `.env` bana ke apni values daalo:
   - `BOT_TOKEN` — @BotFather se lo
   - `GROQ_API_KEY` — console.groq.com se lo
   - `ADMIN_IDS` — apna Telegram numeric user_id (comma-separated agar multiple)

2. GitHub par ye pura repo push karo.

3. Render par:
   - New + → Background Worker
   - Repo connect karo (render.yaml auto-detect hoga)
   - Environment tab mein BOT_TOKEN, GROQ_API_KEY, ADMIN_IDS add karo
   - Deploy

4. Local test (optional):
   ```
   pip install -r requirements.txt
   python bot.py
   ```
   (ffmpeg system mein installed hona chahiye: `apt install ffmpeg`)

## Files
- `bot.py` — main entrypoint, handlers, pipeline orchestration
- `database.py` — SQLite schema + helpers
- `downloader.py` — yt-dlp wrapper
- `ai_analysis.py` — Groq Whisper transcription + LLM clip analysis
- `clipper.py` — ffmpeg cutting, smart crop, caption burn-in
- `safety.py` — basic content safety checks
- `render.yaml` — Render deployment config
