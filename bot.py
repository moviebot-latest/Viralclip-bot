"""
bot.py
Main Telegram bot entrypoint. Orchestrates the full pipeline:
link -> validate -> download -> transcribe -> analyze -> cut/caption -> send

Phase 1 scope (working end-to-end):
  - /start, /history, /myappeals, /status
  - Link handling with pre-download validation + safety check
  - Daily free limit (5/day) + admin bonus credits
  - File size tiers (2GB default / 4GB admin-approved)
  - 1 concurrent job per user
  - Live status updates
  - Word-boundary-safe clipping + smart crop + captions
  - Virality score + reasoning + hook + platform suggestion in output
  - Appeal system (user disputes a rejection -> admin approves/rejects)
  - Temp file cleanup
  - Download caching (same URL reused)

Admin commands: /addcredit, /allow4gb, /revoke4gb, review approve/reject buttons
"""

import os
import uuid
import logging
import asyncio
import threading
from dotenv import load_dotenv
from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

import database as db
import downloader
import safety
import ai_analysis
import clipper

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x}
FREE_DAILY_LIMIT = 5
MAX_CLIPS = 10
MAX_VIDEO_SECONDS = 2 * 3600  # 2 hour cap, Phase 1

# Processing safety cutoff (separate from the 2GB/4GB Bot API *upload* limit).
# Normal users: up to 1GB processed. Admin-approved 4GB users: up to 4GB.
# Free Render tier hardware (512MB RAM, 0.1 CPU) may still be slow/unstable
# near these ceilings — raise if you upgrade Render's plan.
NORMAL_SAFE_PROCESSING_BYTES = 1 * 1024 ** 3       # 1GB
ADMIN_SAFE_PROCESSING_BYTES = 4 * 1024 ** 3        # 4GB

# Local Bot API server (raises Telegram's 20MB download / 50MB upload caps to 2GB).
# Set LOCAL_BOT_API_URL to your deployed telegram-bot-api service, e.g.
# "https://your-bot-api-server.onrender.com". Leave unset to use api.telegram.org.
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# tracks users with an active job right now (in-memory, per-process)
active_jobs = set()

# tracks a job waiting on the user's platform choice: user_id -> job spec dict
# spec = {"source_url": ..., "local_video_path": ..., "status_message": Message}
pending_platform_choice = {}


# ---------------- helper ----------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def cleanup_files(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            logger.warning(f"Could not remove {p}")


# ---------------- basic commands ----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bhej de koi bhi YouTube/Instagram/TikTok/Facebook link ya video file — "
        "main usme se best 10 viral clips nikaal ke dunga, hooks aur captions ke saath.\n\n"
        "Free: 5 videos/day. Zyada chahiye toh admin se bonus credit maango.\n\n"
        "⚠️ Disclaimer: content ki copyright responsibility tumhari hai, bot sirf ek tool hai."
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db.get_user_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Abhi tak koi video process nahi hua.")
        return
    lines = [f"• {r[1][:40]}... — {r[3]} ({r[2][:16]})" for r in rows]
    await update.message.reply_text("📜 Tera history:\n" + "\n".join(lines))


async def myappeals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = await db.get_appeal_count_today(user_id)
    await update.message.reply_text(f"Aaj ke appeals: {count}/3")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"✅ Bot online.\nActive jobs abhi: {len(active_jobs)}"
    )


# ---------------- admin commands ----------------

async def addcredit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        target_id, count = int(context.args[0]), int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addcredit <user_id> <count>")
        return
    await db.add_bonus_credits(target_id, count)
    await update.message.reply_text(f"✅ {count} bonus credits diye user {target_id} ko.")
    try:
        await context.bot.send_message(target_id, f"🎁 Admin ne tumhe {count} bonus videos diye hain aaj ke liye!")
    except Exception:
        pass


async def allow4gb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        target_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /allow4gb <user_id>")
        return
    await db.set_max_file_size(target_id, 4 * 1024 ** 3)
    await update.message.reply_text(f"✅ User {target_id} ab 4GB tak upload/download kar sakta hai.")


async def revoke4gb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        target_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /revoke4gb <user_id>")
        return
    await db.set_max_file_size(target_id, 2 * 1024 ** 3)
    await update.message.reply_text(f"✅ User {target_id} wapas 2GB limit pe.")


# ---------------- appeal system ----------------

async def appeal_button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, source_url = query.data.split("|", 1)
    user_id = query.from_user.id

    appeal_count = await db.get_appeal_count_today(user_id)
    if appeal_count >= 3:
        await query.edit_message_text("❌ Aaj ke appeal limit (3) khatam ho gaye. Kal try karo.")
        return

    await db.increment_appeal_count(user_id)
    review_id = await db.create_review_request(user_id, source_url, "User disputed automatic rejection")

    await query.edit_message_text("📨 Tera appeal admin ko bhej diya gaya hai. Review hone tak wait karo.")

    for admin_id in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"radm|{review_id}|approve"),
                InlineKeyboardButton("❌ Reject", callback_data=f"radm|{review_id}|reject"),
            ]])
            await context.bot.send_message(
                admin_id,
                f"🔍 Appeal Review #{review_id}\nUser: {user_id}\nSource: {source_url}\n"
                f"Bot's original reason: content flagged automatically",
                reply_markup=kb,
            )
        except Exception:
            logger.warning(f"Could not notify admin {admin_id}")


async def admin_review_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Sirf admin ke liye.", show_alert=True)
        return
    await query.answer()

    _, review_id, decision = query.data.split("|")
    review_id = int(review_id)
    review = await db.get_review(review_id)
    if not review:
        await query.edit_message_text("Review not found (already handled?).")
        return

    _, user_id, source_url, reason, status = review
    if status != "pending":
        await query.edit_message_text(f"Already handled: {status}")
        return

    if decision == "approve":
        await db.set_review_status(review_id, "approved")
        await query.edit_message_text(f"✅ Approved review #{review_id}.")
        await context.bot.send_message(
            user_id, "✅ Admin ne approve kar diya! Video ab process ho raha hai..."
        )
        await process_video_job(context, user_id, source_url)
    else:
        await db.set_review_status(review_id, "rejected")
        await query.edit_message_text(f"❌ Rejected review #{review_id}.")
        await context.bot.send_message(
            user_id, "❌ Admin ne bhi confirm kiya — ye content process nahi ho sakta."
        )


# ---------------- feedback buttons ----------------

async def feedback_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Dhanyawaad!")
    _, clip_id, value = query.data.split("|")
    await db.set_clip_feedback(clip_id, int(value))


async def platform_choice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, platform = query.data.split("|")

    spec = pending_platform_choice.pop(user_id, None)
    if spec is None:
        await query.edit_message_text("Ye request expire ho gayi. Video/link dobara bhejo.")
        return

    label = {"instagram": "Instagram", "youtube": "YouTube", "both": "Instagram + YouTube"}[platform]
    await query.edit_message_text(f"✅ {label} ke liye clips banayenge. Processing shuru...")

    await process_video_job(
        context, user_id, source_url=spec["source_url"],
        status_message=query.message, local_video_path=spec["local_video_path"],
        platform=platform,
    )


# ---------------- core pipeline ----------------

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not text.startswith("http"):
        await update.message.reply_text("Ye valid link nahi lag raha. YouTube/Instagram/TikTok/Facebook link bhejo.")
        return

    if user_id in active_jobs:
        await update.message.reply_text("⏳ Tera pehle se ek video process ho raha hai. Uske complete hone ka wait karo.")
        return

    if not await db.can_process(user_id, FREE_DAILY_LIMIT):
        await update.message.reply_text(
            "🚫 Aaj ka free limit (5 videos) khatam ho gaya. Kal try karo ya admin se bonus credit maango."
        )
        return

    pending_platform_choice[user_id] = {"source_url": text, "local_video_path": None}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 Instagram", callback_data="platform|instagram"),
        InlineKeyboardButton("▶️ YouTube", callback_data="platform|youtube"),
        InlineKeyboardButton("🔀 Dono", callback_data="platform|both"),
    ]])
    await update.message.reply_text(
        "Kis platform ke liye clips chahiye?", reply_markup=kb
    )


async def handle_video_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message

    if user_id in active_jobs:
        await msg.reply_text("⏳ Tera pehle se ek video process ho raha hai. Uske complete hone ka wait karo.")
        return

    if not await db.can_process(user_id, FREE_DAILY_LIMIT):
        await msg.reply_text(
            "🚫 Aaj ka free limit (5 videos) khatam ho gaya. Kal try karo ya admin se bonus credit maango."
        )
        return

    tg_file = msg.video or msg.document
    if tg_file is None:
        return

    max_size = await db.get_max_file_size(user_id)
    if tg_file.file_size and tg_file.file_size > max_size:
        limit_gb = max_size / (1024 ** 3)
        await msg.reply_text(f"❌ File bahut badi hai. Tera limit {limit_gb:.0f}GB hai.")
        return

    # Processing safety cutoff: normal users up to 1GB, admin-approved 4GB
    # users up to 4GB. (Separate from the accept/upload limit above.)
    is_4gb_user = max_size > 2 * 1024 ** 3
    safe_limit = ADMIN_SAFE_PROCESSING_BYTES if is_4gb_user else NORMAL_SAFE_PROCESSING_BYTES

    if tg_file.file_size and tg_file.file_size > safe_limit:
        limit_mb = safe_limit / (1024 ** 2)
        await msg.reply_text(
            f"⚠️ Ye file abhi ke server resources ke liye bahut badi hai "
            f"(current safe limit ~{limit_mb:.0f}MB). Chhoti video try karo, "
            f"ya link bhejo (link se processing zyada stable hai)."
        )
        return

    status_msg = await msg.reply_text("📥 File download ho raha hai...")

    job_id = uuid.uuid4().hex[:12]
    local_path = os.path.join("downloads", f"{job_id}_upload.mp4")

    try:
        file_obj = await context.bot.get_file(tg_file.file_id)
        await file_obj.download_to_drive(local_path)
    except Exception as e:
        await status_msg.edit_text(f"❌ File download nahi ho payi: {str(e)[:150]}")
        return

    await status_msg.edit_text("✅ File mil gayi.")
    pending_platform_choice[user_id] = {"source_url": f"direct_upload:{tg_file.file_id}", "local_video_path": local_path}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 Instagram", callback_data="platform|instagram"),
        InlineKeyboardButton("▶️ YouTube", callback_data="platform|youtube"),
        InlineKeyboardButton("🔀 Dono", callback_data="platform|both"),
    ]])
    await status_msg.reply_text("Kis platform ke liye clips chahiye?", reply_markup=kb)


async def process_video_job(context: ContextTypes.DEFAULT_TYPE, user_id: int, source_url: str,
                             status_message=None, local_video_path: str = None, platform: str = "both"):
    active_jobs.add(user_id)
    job_id = uuid.uuid4().hex[:12]
    video_path = local_video_path
    audio_path = None

    async def send_status(text):
        if status_message:
            return await status_message.reply_text(text)
        return await context.bot.send_message(user_id, text)

    try:
        await db.create_job(job_id, user_id, source_url)

        status_msg = await send_status("🔎 Video check kar raha hoon...")
        cached_transcript = None

        if local_video_path is None:
            # --- pre-download validation (link flow only) ---
            meta = await downloader.probe_metadata(source_url)
            dur_check = safety.check_duration(meta["duration"], MAX_VIDEO_SECONDS)
            if not dur_check["ok"]:
                await status_msg.edit_text(f"❌ {dur_check['reason']}")
                await db.update_job_status(job_id, "rejected")
                return

            approx_size = meta.get("filesize_approx", 0)
            user_max_size = await db.get_max_file_size(user_id)
            is_4gb_user = user_max_size > 2 * 1024 ** 3
            safe_limit = ADMIN_SAFE_PROCESSING_BYTES if is_4gb_user else NORMAL_SAFE_PROCESSING_BYTES

            if approx_size and approx_size > safe_limit:
                limit_mb = safe_limit / (1024 ** 2)
                await status_msg.edit_text(
                    f"⚠️ Ye video abhi ke server resources ke liye bahut badi hai "
                    f"(~{approx_size/(1024**2):.0f}MB, safe limit ~{limit_mb:.0f}MB). "
                    "Chhoti video try karo."
                )
                await db.update_job_status(job_id, "rejected")
                return

            safety_check = safety.check_metadata_safety(meta["title"], meta["description"])
            if not safety_check["safe"]:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚠️ Galat laga? Admin ko batao", callback_data=f"appeal|{source_url}")
                ]])
                await status_msg.edit_text(
                    f"❌ Ye content policy ke against lag raha hai: {safety_check['reason']}\n"
                    "Agar galat laga hai toh niche button se admin ko bata sakte ho.",
                    reply_markup=kb,
                )
                await db.update_job_status(job_id, "rejected")
                return

            # --- download (with caching) ---
            url_hash = downloader.url_hash(source_url)
            cached = await db.get_cached_download(url_hash)

            await status_msg.edit_text("📥 Downloading video...")
            if cached and os.path.exists(cached[0]):
                video_path = cached[0]
            else:
                video_path = await downloader.download_best_quality(source_url, job_id)
            if cached:
                cached_transcript = cached[1]
        else:
            # --- direct upload flow: skip link validation/download ---
            await status_msg.edit_text("🔎 Uploaded video check kar raha hoon...")
            duration = await clipper.get_video_duration(video_path)
            dur_check = safety.check_duration(duration, MAX_VIDEO_SECONDS)
            if not dur_check["ok"]:
                await status_msg.edit_text(f"❌ {dur_check['reason']}")
                await db.update_job_status(job_id, "rejected")
                return
            url_hash = downloader.url_hash(f"upload:{job_id}")

        await db.update_job_status(job_id, "processing")

        # --- transcription ---
        await status_msg.edit_text("🎙️ Transcribing audio...")
        audio_path = os.path.join("downloads", f"{job_id}_audio.wav")
        await clipper.extract_audio(video_path, audio_path)

        if cached_transcript:
            import json as _json
            transcript = _json.loads(cached_transcript)
        else:
            transcript = await ai_analysis.transcribe(audio_path)
            import json as _json
            await db.save_cached_download(url_hash, video_path, _json.dumps(transcript))

        # --- AI analysis ---
        await status_msg.edit_text("🧠 Analyzing for viral moments...")
        video_duration = await clipper.get_video_duration(video_path)
        candidate_clips = await ai_analysis.analyze_for_clips(
            transcript, MAX_CLIPS, video_duration=video_duration, platform=platform
        )

        if not candidate_clips:
            await status_msg.edit_text("😕 Koi high-value moment nahi mila is video mein.")
            await db.update_job_status(job_id, "failed")
            return

        all_words = transcript.get("words", [])

        # --- cut + render each clip ---
        sent_count = 0
        for i, c in enumerate(candidate_clips, 1):
            await status_msg.edit_text(f"✂️ Cutting clip {i}/{len(candidate_clips)}...")

            clip_words = [w for w in all_words if c["start_time"] - 1 <= w["start"] <= c["end_time"] + 1]

            start = clipper.snap_to_word_boundary(c["start_time"], clip_words, is_start=True)
            end = clipper.snap_to_word_boundary(c["end_time"], clip_words, is_start=False)

            clip_id = f"{job_id}_{i}"
            try:
                out_path = await clipper.cut_and_render_clip(
                    video_path, clip_id, start, end, clip_words, style="style_2"
                )
            except Exception as e:
                logger.exception(f"Clip {i} render failed")
                continue

            await db.save_clip(
                clip_id, job_id, out_path, c["virality_score"], c["reasoning"],
                c["hook_text"], c["suggested_platform"], start, end,
            )

            caption = (
                f"🔥 Virality Score: {c['virality_score']}/100\n"
                f"💡 {c['reasoning']}\n"
                f"📱 Best for: {c['suggested_platform']}\n"
                f"⏱️ Original timestamp: {int(start//60)}:{int(start%60):02d}"
            )
            fb_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("👍", callback_data=f"fb|{clip_id}|1"),
                InlineKeyboardButton("👎", callback_data=f"fb|{clip_id}|-1"),
            ]])

            with open(out_path, "rb") as vf:
                await context.bot.send_video(user_id, vf, caption=caption, reply_markup=fb_kb)
            sent_count += 1

        await status_msg.edit_text(f"✅ Done! {sent_count} clips bhej diye.")
        await db.update_job_status(job_id, "done")
        await db.increment_usage(user_id)

    except Exception as e:
        logger.exception("Job failed")
        await send_status(f"❌ Kuch galat ho gaya: {str(e)[:200]}\nDobara try karo.")
        await db.update_job_status(job_id, "failed")

    finally:
        active_jobs.discard(user_id)
        # cleanup temp files (keep original video cached, remove audio + subtitle temp files)
        await cleanup_files(audio_path)
        for f in os.listdir("clips"):
            if f.endswith(".ass"):
                await cleanup_files(os.path.join("clips", f))


# ---------------- main ----------------

async def post_init(application: Application):
    await db.init_db()
    logger.info("Database initialized.")


# ---------------- health check server (for Render free Web Service) ----------------
# Render free tier only supports Web Services, which need to respond to HTTP
# requests to stay "alive". The actual bot runs on Telegram polling in the
# main thread; this tiny Flask server just answers pings on the port Render
# expects, on a separate thread, so Render doesn't consider the service dead.

health_app = Flask(__name__)


@health_app.route("/")
def health():
    return "Bot is running.", 200


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    health_app.run(host="0.0.0.0", port=port)


def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    # Python 3.14 no longer auto-creates an event loop for the main thread.
    # python-telegram-bot's run_polling() relies on asyncio.get_event_loop()
    # internally, so we create and set one explicitly before building the app.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)
    if LOCAL_BOT_API_URL:
        builder = builder.base_url(f"{LOCAL_BOT_API_URL}/bot").base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
        logger.info(f"Using local Bot API server: {LOCAL_BOT_API_URL}")
    app = builder.build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("myappeals", myappeals_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    app.add_handler(CommandHandler("addcredit", addcredit_cmd))
    app.add_handler(CommandHandler("allow4gb", allow4gb_cmd))
    app.add_handler(CommandHandler("revoke4gb", revoke4gb_cmd))

    app.add_handler(CallbackQueryHandler(appeal_button_cb, pattern=r"^appeal\|"))
    app.add_handler(CallbackQueryHandler(admin_review_cb, pattern=r"^radm\|"))
    app.add_handler(CallbackQueryHandler(feedback_cb, pattern=r"^fb\|"))
    app.add_handler(CallbackQueryHandler(platform_choice_cb, pattern=r"^platform\|"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video_upload))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
