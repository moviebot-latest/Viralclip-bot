"""
bot.py — Pyrogram edition
Main Telegram bot entrypoint. Orchestrates the full pipeline:
link -> validate -> download -> transcribe -> analyze -> cut/caption -> send

Switched from python-telegram-bot (Bot API) to Pyrogram (MTProto client) for
reliable large-file download/upload with real byte-level progress, without
needing a separately-hosted Local Bot API Server.

File size tiers:
  - Normal user: 2GB
  - Admin-approved (/allow4gb): 2GB extra = 4GB total

Admin commands: /addcredit, /allow4gb, /revoke4gb, review approve/reject buttons
"""

import os
import uuid
import time
import json
import logging
import asyncio
from threading import Thread
from dotenv import load_dotenv
from flask import Flask

# Python 3.14 no longer auto-creates an event loop for the main thread.
# Pyrogram's sync.py module calls asyncio.get_event_loop() at IMPORT time
# (not just at runtime), so the loop must exist BEFORE `from pyrogram import
# ...` runs — setting it inside main() is too late and crashes on import.
asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
import downloader
import safety
import ai_analysis
import clipper

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x}
FREE_DAILY_LIMIT = 5
MAX_CLIPS = 10
MAX_VIDEO_SECONDS = 2 * 3600  # 2 hour cap, Phase 1

NORMAL_MAX_BYTES = 2 * 1024 ** 3  # 2GB
ADMIN_MAX_BYTES = 4 * 1024 ** 3   # 2GB extra = 4GB total

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Client(
    "viralclip-bot",
    api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
    in_memory=True,
    sleep_threshold=300,
)

active_jobs = set()
pending_platform_choice = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def cleanup_files(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            logger.warning(f"Could not remove {p}")


def platform_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 Instagram", callback_data="platform|instagram"),
        InlineKeyboardButton("▶️ YouTube", callback_data="platform|youtube"),
        InlineKeyboardButton("🔀 Dono", callback_data="platform|both"),
    ]])


_last_edit_time: dict[int, float] = {}
EDIT_THROTTLE_SECONDS = 2.0


def _fmt_size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


async def _progress_cb(current: int, total: int, status_msg, label: str, msg_key: int):
    if not total:
        return
    now = time.time()
    last = _last_edit_time.get(msg_key, 0)
    pct = current / total * 100
    if pct < 100 and (now - last) < EDIT_THROTTLE_SECONDS:
        return
    _last_edit_time[msg_key] = now
    bar_len = 20
    filled = int(pct / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    try:
        await status_msg.edit_text(
            f"{label}\n[{bar}] {pct:.0f}%\n{_fmt_size(current)} / {_fmt_size(total)}"
        )
    except Exception:
        pass


@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "👋 Bhej de koi bhi YouTube/Instagram/TikTok/Facebook link ya video file — "
        "main usme se best 10 viral clips nikaal ke dunga, hooks aur captions ke saath.\n\n"
        "Free: 5 videos/day, 2GB file size. Zyada chahiye toh admin se bonus credit maango.\n\n"
        "⚠️ Disclaimer: content ki copyright responsibility tumhari hai, bot sirf ek tool hai."
    )


@app.on_message(filters.command("history"))
async def history_cmd(client, message):
    rows = await db.get_user_history(message.from_user.id)
    if not rows:
        await message.reply_text("Abhi tak koi video process nahi hua.")
        return
    lines = [f"• {r[1][:40]}... — {r[3]} ({r[2][:16]})" for r in rows]
    await message.reply_text("📜 Tera history:\n" + "\n".join(lines))


@app.on_message(filters.command("myappeals"))
async def myappeals_cmd(client, message):
    count = await db.get_appeal_count_today(message.from_user.id)
    await message.reply_text(f"Aaj ke appeals: {count}/3")


@app.on_message(filters.command("status"))
async def status_cmd(client, message):
    await message.reply_text(f"✅ Bot online.\nActive jobs abhi: {len(active_jobs)}")


@app.on_message(filters.command("addcredit"))
async def addcredit_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split()
        target_id, count = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        await message.reply_text("Usage: /addcredit <user_id> <count>")
        return
    await db.add_bonus_credits(target_id, count)
    await message.reply_text(f"✅ {count} bonus credits diye user {target_id} ko.")
    try:
        await client.send_message(target_id, f"🎁 Admin ne tumhe {count} bonus videos diye hain aaj ke liye!")
    except Exception:
        pass


@app.on_message(filters.command("allow4gb"))
async def allow4gb_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    try:
        target_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.reply_text("Usage: /allow4gb <user_id>")
        return
    await db.set_max_file_size(target_id, ADMIN_MAX_BYTES)
    await message.reply_text(f"✅ User {target_id} ab 4GB (2GB extra) tak upload/download kar sakta hai.")


@app.on_message(filters.command("revoke4gb"))
async def revoke4gb_cmd(client, message):
    if not is_admin(message.from_user.id):
        return
    try:
        target_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.reply_text("Usage: /revoke4gb <user_id>")
        return
    await db.set_max_file_size(target_id, NORMAL_MAX_BYTES)
    await message.reply_text(f"✅ User {target_id} wapas 2GB limit pe.")


@app.on_callback_query(filters.regex(r"^appeal\|"))
async def appeal_button_cb(client, callback_query):
    _, source_url = callback_query.data.split("|", 1)
    user_id = callback_query.from_user.id

    appeal_count = await db.get_appeal_count_today(user_id)
    if appeal_count >= 3:
        await callback_query.answer()
        await callback_query.edit_message_text("❌ Aaj ke appeal limit (3) khatam ho gaye. Kal try karo.")
        return

    await db.increment_appeal_count(user_id)
    review_id = await db.create_review_request(user_id, source_url, "User disputed automatic rejection")

    await callback_query.answer()
    await callback_query.edit_message_text("📨 Tera appeal admin ko bhej diya gaya hai. Review hone tak wait karo.")

    for admin_id in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"radm|{review_id}|approve"),
                InlineKeyboardButton("❌ Reject", callback_data=f"radm|{review_id}|reject"),
            ]])
            await client.send_message(
                admin_id,
                f"🔍 Appeal Review #{review_id}\nUser: {user_id}\nSource: {source_url}\n"
                f"Bot's original reason: content flagged automatically",
                reply_markup=kb,
            )
        except Exception:
            logger.warning(f"Could not notify admin {admin_id}")


@app.on_callback_query(filters.regex(r"^radm\|"))
async def admin_review_cb(client, callback_query):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("Sirf admin ke liye.", show_alert=True)
        return
    await callback_query.answer()

    _, review_id, decision = callback_query.data.split("|")
    review_id = int(review_id)
    review = await db.get_review(review_id)
    if not review:
        await callback_query.edit_message_text("Review not found (already handled?).")
        return

    _, user_id, source_url, reason, status = review
    if status != "pending":
        await callback_query.edit_message_text(f"Already handled: {status}")
        return

    if decision == "approve":
        await db.set_review_status(review_id, "approved")
        await callback_query.edit_message_text(f"✅ Approved review #{review_id}.")
        await client.send_message(user_id, "✅ Admin ne approve kar diya! Video ab process ho raha hai...")
        await process_video_job(client, user_id, source_url)
    else:
        await db.set_review_status(review_id, "rejected")
        await callback_query.edit_message_text(f"❌ Rejected review #{review_id}.")
        await client.send_message(user_id, "❌ Admin ne bhi confirm kiya — ye content process nahi ho sakta.")


@app.on_callback_query(filters.regex(r"^fb\|"))
async def feedback_cb(client, callback_query):
    await callback_query.answer("Dhanyawaad!")
    _, clip_id, value = callback_query.data.split("|")
    await db.set_clip_feedback(clip_id, int(value))


@app.on_callback_query(filters.regex(r"^platform\|"))
async def platform_choice_cb(client, callback_query):
    await callback_query.answer()
    user_id = callback_query.from_user.id
    _, platform = callback_query.data.split("|")

    spec = pending_platform_choice.pop(user_id, None)
    if spec is None:
        await callback_query.edit_message_text("Ye request expire ho gayi. Video/link dobara bhejo.")
        return

    label = {"instagram": "Instagram", "youtube": "YouTube", "both": "Instagram + YouTube"}[platform]
    await callback_query.edit_message_text(f"✅ {label} ke liye clips banayenge. Processing shuru...")

    await process_video_job(
        client, user_id, source_url=spec["source_url"],
        status_message=callback_query.message, local_video_path=spec["local_video_path"],
        platform=platform,
    )


@app.on_message(filters.text & filters.incoming & ~filters.command([
    "start", "history", "myappeals", "status", "addcredit", "allow4gb", "revoke4gb"
]))
async def handle_link(client, message):
    user_id = message.from_user.id
    text = message.text.strip()

    if not text.startswith("http"):
        await message.reply_text("Ye valid link nahi lag raha. YouTube/Instagram/TikTok/Facebook link bhejo.")
        return

    if user_id in active_jobs:
        await message.reply_text("⏳ Tera pehle se ek video process ho raha hai. Uske complete hone ka wait karo.")
        return

    if not await db.can_process(user_id, FREE_DAILY_LIMIT):
        await message.reply_text(
            "🚫 Aaj ka free limit (5 videos) khatam ho gaya. Kal try karo ya admin se bonus credit maango."
        )
        return

    pending_platform_choice[user_id] = {"source_url": text, "local_video_path": None}
    await message.reply_text("Kis platform ke liye clips chahiye?", reply_markup=platform_kb())


@app.on_message(filters.incoming & (filters.video | filters.document))
async def handle_video_upload(client, message):
    user_id = message.from_user.id

    if user_id in active_jobs:
        await message.reply_text("⏳ Tera pehle se ek video process ho raha hai. Uske complete hone ka wait karo.")
        return

    if not await db.can_process(user_id, FREE_DAILY_LIMIT):
        await message.reply_text(
            "🚫 Aaj ka free limit (5 videos) khatam ho gaya. Kal try karo ya admin se bonus credit maango."
        )
        return

    media = message.video or message.document
    if media is None:
        return

    mime = getattr(media, "mime_type", "") or ""
    if mime and not (mime.startswith("video/") or mime == "application/octet-stream"):
        return

    max_size = await db.get_max_file_size(user_id)
    file_size = getattr(media, "file_size", 0) or 0
    if file_size and file_size > max_size:
        limit_gb = max_size / (1024 ** 3)
        await message.reply_text(f"❌ File bahut badi hai. Tera limit {limit_gb:.0f}GB hai.")
        return

    job_id = uuid.uuid4().hex[:12]
    local_path = os.path.join("downloads", f"{job_id}_upload.mp4")

    status_msg = await message.reply_text(f"📥 Downloading...\n[{'░'*20}] 0%\n0.0MB / {_fmt_size(file_size)}")

    try:
        await message.download(
            file_name=local_path,
            progress=_progress_cb,
            progress_args=(status_msg, "📥 Downloading...", message.id),
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ File download nahi ho payi: {str(e)[:150]}")
        return

    if not os.path.exists(local_path):
        await status_msg.edit_text("❌ File download nahi ho payi (file not saved).")
        return

    await status_msg.edit_text("✅ File mil gayi.")
    pending_platform_choice[user_id] = {
        "source_url": f"direct_upload:{job_id}",
        "local_video_path": local_path,
    }
    await status_msg.reply_text("Kis platform ke liye clips chahiye?", reply_markup=platform_kb())


async def process_video_job(client, user_id: int, source_url: str,
                             status_message=None, local_video_path: str = None, platform: str = "both"):
    active_jobs.add(user_id)
    job_id = uuid.uuid4().hex[:12]
    video_path = local_video_path
    audio_path = None

    async def send_status(text):
        if status_message:
            return await status_message.reply_text(text)
        return await client.send_message(user_id, text)

    try:
        await db.create_job(job_id, user_id, source_url)

        status_msg = await send_status("🔎 Video check kar raha hoon...")
        cached_transcript = None

        if local_video_path is None:
            meta = await downloader.probe_metadata(source_url)
            dur_check = safety.check_duration(meta["duration"], MAX_VIDEO_SECONDS)
            if not dur_check["ok"]:
                await status_msg.edit_text(f"❌ {dur_check['reason']}")
                await db.update_job_status(job_id, "rejected")
                return

            approx_size = meta.get("filesize_approx", 0)
            user_max_size = await db.get_max_file_size(user_id)
            if approx_size and approx_size > user_max_size:
                limit_gb = user_max_size / (1024 ** 3)
                await status_msg.edit_text(
                    f"⚠️ Ye video bahut badi hai (~{approx_size/(1024**2):.0f}MB). "
                    f"Tera limit {limit_gb:.0f}GB hai."
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
            await status_msg.edit_text("🔎 Uploaded video check kar raha hoon...")
            duration = await clipper.get_video_duration(video_path)
            dur_check = safety.check_duration(duration, MAX_VIDEO_SECONDS)
            if not dur_check["ok"]:
                await status_msg.edit_text(f"❌ {dur_check['reason']}")
                await db.update_job_status(job_id, "rejected")
                return
            url_hash = downloader.url_hash(f"upload:{job_id}")

        await db.update_job_status(job_id, "processing")

        await status_msg.edit_text("🎙️ Transcribing audio...")
        audio_path = os.path.join("downloads", f"{job_id}_audio.wav")
        await clipper.extract_audio(video_path, audio_path)

        if cached_transcript:
            transcript = json.loads(cached_transcript)
        else:
            transcript = await ai_analysis.transcribe(audio_path)
            await db.save_cached_download(url_hash, video_path, json.dumps(transcript))

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
            except Exception:
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

            upload_status = await status_msg.reply_text(f"⬆️ Uploading clip {i}/{len(candidate_clips)}...")
            try:
                await client.send_video(
                    user_id, out_path, caption=caption, reply_markup=fb_kb,
                    progress=_progress_cb,
                    progress_args=(upload_status, f"⬆️ Uploading clip {i}...", hash(clip_id) & 0x7FFFFFFF),
                )
                sent_count += 1
                try:
                    await upload_status.delete()
                except Exception:
                    pass
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                try:
                    await client.send_video(user_id, out_path, caption=caption, reply_markup=fb_kb)
                    sent_count += 1
                except Exception:
                    logger.exception(f"Clip {i} upload failed after FloodWait retry")
            except Exception:
                logger.exception(f"Clip {i} upload failed")

        await status_msg.edit_text(f"✅ Done! {sent_count} clips bhej diye.")
        await db.update_job_status(job_id, "done")
        await db.increment_usage(user_id)

    except Exception as e:
        logger.exception("Job failed")
        await send_status(f"❌ Kuch galat ho gaya: {str(e)[:200]}\nDobara try karo.")
        await db.update_job_status(job_id, "failed")

    finally:
        active_jobs.discard(user_id)
        await cleanup_files(audio_path)
        for f in os.listdir("clips"):
            if f.endswith(".ass"):
                await cleanup_files(os.path.join("clips", f))


health_app = Flask(__name__)


@health_app.route("/")
def health():
    return "Bot is running.", 200


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    health_app.run(host="0.0.0.0", port=port)


async def _startup():
    await db.init_db()
    logger.info("Database initialized.")


def main():
    Thread(target=run_health_server, daemon=True).start()

    os.makedirs("downloads", exist_ok=True)
    os.makedirs("clips", exist_ok=True)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_startup())

    logger.info("Bot starting (Pyrogram)...")
    app.run()


if __name__ == "__main__":
    main()
