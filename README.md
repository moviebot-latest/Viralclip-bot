# Telegram Local Bot API Server

Raises Telegram Bot API's file limits from 50MB to 2GB. Deploy this as its
own Render service, separate from the main bot.

## Setup

1. Push this folder (`Dockerfile` + `render.yaml`) as its **own GitHub repo**
   (or a subfolder with Root Directory set in Render — see below).

2. Render → **New + → Web Service**
   - Environment: **Docker** (not Python)
   - Connect this repo/folder
   - Plan: Free (note: free plan has limited RAM — 512MB — which may be tight
     for large video files; Starter plan is more reliable for real use)

3. Environment Variables:
   - `TELEGRAM_API_ID` — from my.telegram.org
   - `TELEGRAM_API_HASH` — from my.telegram.org

4. Deploy. Once live, copy the service URL, e.g.
   `https://telegram-bot-api-server.onrender.com`

5. Go to your **main bot** service on Render → Environment tab → add:
   - `LOCAL_BOT_API_URL` = `https://telegram-bot-api-server.onrender.com`

6. Redeploy the main bot. It will now route all Telegram API calls through
   your local server, unlocking 2GB file support.

## Important notes

- Free Render plan has **no persistent disk** guarantee and **512MB RAM** —
  large video uploads/downloads may be slow or fail under memory pressure.
  If this becomes unreliable, upgrading this specific service to Starter
  ($7/mo) fixes it while the main bot can stay free.
- Both services must stay awake for uploads to work — free tier sleep
  after 15 min inactivity applies to this service too. Use UptimeRobot on
  BOTH service URLs to keep them alive.
- Build takes longer than usual (~5-10 min) since it compiles
  telegram-bot-api from source.
