# ViralClip Bot — Phase 1 (Pyrogram edition)

Telegram bot: link/video bhejo → AI se best 10 viral clips (hooks + captions + smart
crop) nikaal ke bhejta hai.

Runs on **Pyrogram** (MTProto client) instead of the Bot API, so it can
reliably handle large file downloads/uploads (2GB normal, 4GB admin-approved)
without needing a separately-hosted Local Bot API Server.

## Setup

1. `.env.example` ko `.env` bana ke apni values daalo:
   - `BOT_TOKEN` — @BotFather se lo
   - `GROQ_API_KEY` — console.groq.com se lo
   - `ADMIN_IDS` — apna Telegram numeric user_id (comma-separated agar multiple)
   - `TELEGRAM_API_ID` — my.telegram.org se lo
   - `TELEGRAM_API_HASH` — my.telegram.org se lo

2. GitHub par ye pura repo push karo.

3. Render par:
   - New + -> Web Service (Free plan)
   - Repo connect karo (render.yaml auto-detect hoga)
   - Environment tab mein BOT_TOKEN, GROQ_API_KEY, ADMIN_IDS,
     TELEGRAM_API_ID, TELEGRAM_API_HASH add karo
   - Deploy

4. Local test (optional):
   pip install -r requirements.txt
   python bot.py
   (ffmpeg system mein installed hona chahiye: apt install ffmpeg)

## File size tiers
- Normal user: 2GB
- Admin-approved (/allow4gb user_id): 4GB (2GB extra)
- /revoke4gb user_id - wapas 2GB pe le aata hai

## Files
- bot.py - main entrypoint (Pyrogram), handlers, pipeline orchestration
- database.py - SQLite schema + helpers
- downloader.py - yt-dlp wrapper
- ai_analysis.py - Groq Whisper transcription + LLM clip analysis
- clipper.py - ffmpeg cutting, smart crop, caption burn-in
- safety.py - basic content safety checks
- render.yaml - Render deployment config

## Notes on the Pyrogram switch
Pyrogram connects directly via MTProto instead of going through the Bot API:
- Native 2GB/4GB file support (no separate Local Bot API Server needed)
- Real byte-level download/upload progress bars
- More reliable large-file transfers on free-tier hosting

Render free tier still has only 512MB RAM / 0.1 CPU, so very large files
(1GB+) may process slowly (transcription + ffmpeg encoding are CPU/RAM
heavy) even though the download/upload itself is reliable. Upgrading
Render's plan improves processing speed, not just file size support.
