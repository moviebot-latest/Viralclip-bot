# Ultra Bot v3.0 — FIX #24: RPCError as ServerError (latest pyrogram)
import os
import sys
import glob
import time
import math
import asyncio
import logging
import traceback
from collections import deque
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.errors import RPCError as ServerError  # FIX #24 — ServerError removed in latest pyrogram

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("UltraBot")


# ══════════════════════════════════════════════════════════════
#  FIX #12 + #19 — ENV VALIDATION
#  - Clear error if missing (not cryptic TypeError)
#  - ValueError if API_ID is not a number
# ══════════════════════════════════════════════════════════════
def _require_env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        log.critical(f"❌ Environment variable '{key}' is not set. Bot cannot start.")
        sys.exit(1)
    return val

def _require_int_env(key: str) -> int:
    raw = _require_env(key)
    try:
        return int(raw)
    except ValueError:
        log.critical(f"❌ '{key}' must be an integer, got: '{raw}'")
        sys.exit(1)

API_ID    = _require_int_env("API_ID")
API_HASH  = _require_env("API_HASH")
BOT_TOKEN = _require_env("BOT_TOKEN")


# ══════════════════════════════════════════════════════════════
#  CONSTANTS & DIRS
# ══════════════════════════════════════════════════════════════
DOWNLOAD_DIR        = "downloads"
THUMB_DIR           = "thumbs"
MAX_FILE_WARN       = 1.8 * 1024 ** 3  # 1.8 GB — warn near TG limit
FFMPEG_CUT_TIMEOUT  = 600              # 10 min per segment
FFPROBE_TIMEOUT     = 30              # 30 s for duration probe
THUMB_TIMEOUT       = 15              # 15 s for thumbnail
MIN_PART_BYTES      = 1024            # FIX #21 — reject parts smaller than 1 KB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR,    exist_ok=True)


# ══════════════════════════════════════════════════════════════
#  FIX #13 + #22 — Stale file cleanup on startup
#  Added .wmv and .3gp patterns that were missing in v2
# ══════════════════════════════════════════════════════════════
def _cleanup_stale_files() -> None:
    patterns = [
        f"{DOWNLOAD_DIR}/video_*.mp4",  f"{DOWNLOAD_DIR}/video_*.mkv",
        f"{DOWNLOAD_DIR}/video_*.avi",  f"{DOWNLOAD_DIR}/video_*.mov",
        f"{DOWNLOAD_DIR}/video_*.webm", f"{DOWNLOAD_DIR}/video_*.wmv",
        f"{DOWNLOAD_DIR}/video_*.3gp",  f"{DOWNLOAD_DIR}/part_*.mp4",
        f"{THUMB_DIR}/thumb_*.jpg",
    ]
    removed = 0
    for pat in patterns:
        for f in glob.glob(pat):
            try:
                os.remove(f)
                removed += 1
            except Exception:
                pass
    if removed:
        log.info(f"🧹 Cleaned {removed} stale file(s) from previous session.")

_cleanup_stale_files()


# ══════════════════════════════════════════════════════════════
#  CLIENT
# ══════════════════════════════════════════════════════════════
app = Client(
    "ultra-bot",
    api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
    in_memory=True,
    sleep_threshold=300,
    ipv6=False,
)


# ══════════════════════════════════════════════════════════════
#  PER-USER STATE
# ══════════════════════════════════════════════════════════════
user_files:  dict[int, str]           = {}
user_locks:  dict[int, asyncio.Lock]  = {}
user_cancel: dict[int, asyncio.Event] = {}
user_status: dict[int, dict]          = {}

# FIX #23 — Prune throttled: track last prune time, don't prune on every call
_MAX_USERS        = 2000
_last_prune_time  = 0.0
_PRUNE_INTERVAL   = 300  # prune at most once every 5 minutes

def _prune_state() -> None:
    """
    Drop oldest inactive users when state grows too large.
    FIX #17: Never prune a user whose lock is currently held (task running).
    FIX #23: Throttled — runs at most once per 5 minutes.
    """
    global _last_prune_time
    now = time.time()
    if len(user_locks) < _MAX_USERS:
        return
    if now - _last_prune_time < _PRUNE_INTERVAL:
        return
    _last_prune_time = now

    # FIX #17 — exclude users with active locks from pruning
    active = {uid for uid, lk in user_locks.items() if lk.locked()}
    inactive = [uid for uid in list(user_locks.keys()) if uid not in active]
    # Drop oldest half of inactive users
    to_remove = set(inactive[:len(inactive) // 2])

    for d in (user_locks, user_cancel, user_status):
        for k in list(to_remove):
            d.pop(k, None)
    for k in list(to_remove):
        path = user_files.pop(k, None)
        if path:
            try: os.remove(path)
            except: pass

    log.info(f"🧹 Pruned {len(to_remove)} inactive user states.")


def _get_lock(uid: int) -> asyncio.Lock:
    _prune_state()
    if uid not in user_locks:
        user_locks[uid] = asyncio.Lock()
    return user_locks[uid]

def _get_cancel(uid: int) -> asyncio.Event:
    if uid not in user_cancel:
        user_cancel[uid] = asyncio.Event()
    return user_cancel[uid]

def _set_status(uid: int, task: str, detail: str = "") -> None:
    user_status[uid] = {"task": task, "detail": detail, "since": time.time()}

def _clear_status(uid: int) -> None:
    user_status.pop(uid, None)

def _uid(message) -> int | None:
    """Return user id, or None for channel posts / anonymous senders."""
    return message.from_user.id if message.from_user else None


# ══════════════════════════════════════════════════════════════
#  DEDUP  (FIX #16 — correct eviction order)
#
#  v2 BUG:  eviction checked _seen_deque[0] AFTER append.
#           When deque (maxlen=500) is full, append auto-pops
#           the OLDEST item from the deque. So _seen_deque[0]
#           after append is the 2nd-oldest — wrong key discarded
#           from _seen_set → set could grow unbounded.
#
#  v3 FIX:  check len BEFORE append. If deque is full, grab
#           the item that WILL be evicted (index 0), remove it
#           from the set, THEN append the new key.
# ══════════════════════════════════════════════════════════════
_seen_set:       set               = set()
_seen_deque:     deque             = deque(maxlen=500)
_seen_lock_obj:  asyncio.Lock | None = None

def _get_seen_lock() -> asyncio.Lock:
    global _seen_lock_obj
    if _seen_lock_obj is None:
        _seen_lock_obj = asyncio.Lock()
    return _seen_lock_obj

async def _dedup(message) -> bool:
    """Returns True if this exact message was already processed."""
    key = (message.chat.id, message.id)
    async with _get_seen_lock():
        if key in _seen_set:
            return True
        # FIX #16 — evict the item that WILL be auto-popped BEFORE appending
        if len(_seen_deque) == _seen_deque.maxlen:
            _seen_set.discard(_seen_deque[0])  # this is the one about to leave
        _seen_set.add(key)
        _seen_deque.append(key)
        return False


# ══════════════════════════════════════════════════════════════
#  PROGRESS ENGINE
# ══════════════════════════════════════════════════════════════
THROTTLE  = 0.5
EMA_ALPHA = 0.35
SPINNER   = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

_last_edit: dict[int, float] = {}
_ema_speed: dict[int, float] = {}
_spin_idx:  dict[int, int]   = {}
_shown_pct: dict[int, float] = {}

def _reset(uid: int) -> None:
    for d in (_last_edit, _ema_speed, _spin_idx, _shown_pct):
        d.pop(uid, None)

def _sz(b: float) -> str:
    b = max(0.0, b)  # guard negative
    for u in ("B","KB","MB","GB"):
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def _eta(s: float) -> str:
    s = max(0.0, s)
    if s >= 3600: return f"{int(s//3600)}h {int(s%3600//60)}m"
    if s >= 60:   return f"{int(s//60)}m {int(s%60)}s"
    return f"{int(s)}s"

def _bar(pct: float, w: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    n = int(pct / 100 * w)
    return "[" + "█" * n + "░" * (w - n) + "]"

def _badge(pct: float) -> str:
    return ("🏁" if pct>=100 else "🔥" if pct>=80 else
            "⚡" if pct>=60 else "🚀" if pct>=40 else
            "💫" if pct>=20 else "🌀")

def _count_up(uid: int, real: float, step: float = 1.8) -> float:
    prev  = _shown_pct.get(uid, 0.0)
    shown = min(real, prev + step) if real > prev else real
    _shown_pct[uid] = shown
    return shown

async def _safe_edit(msg, text: str) -> None:
    try:
        await msg.edit(text)
    except FloodWait as e:
        await asyncio.sleep(e.value + 0.5)
        try: await msg.edit(text)
        except: pass
    except MessageNotModified:
        pass
    except Exception:
        pass

async def progress(current, total, msg, start, uid: int = 0,
                   mode: str = "📥 Download") -> None:
    if not isinstance(total, (int, float)) or total <= 0: return
    if _get_cancel(uid).is_set(): return
    now     = time.time()
    elapsed = max(now - start, 0.001)
    if now - _last_edit.get(uid, 0.0) < THROTTLE and _last_edit.get(uid, 0.0) != 0.0:
        return
    _last_edit[uid] = now
    raw   = current / elapsed
    ema   = EMA_ALPHA * raw + (1 - EMA_ALPHA) * _ema_speed.get(uid, raw)
    _ema_speed[uid] = ema
    eta_s = (total - current) / ema if ema > 0 else 0
    real  = current * 100 / total
    shown = _count_up(uid, real)
    spin  = SPINNER[_spin_idx.get(uid, 0) % len(SPINNER)]
    _spin_idx[uid] = _spin_idx.get(uid, 0) + 1
    kb    = ema / 1024
    tier  = "🟢 Fast" if kb >= 1024 else ("🟡 Good" if kb >= 256 else "🔴 Slow")
    await _safe_edit(msg,
        f"{spin} **{mode}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_bar(shown)}\n"
        f"  {_badge(shown)} **{shown:.1f}%** ·  {_sz(current)} / {_sz(total)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  🚄 **Speed** : `{_sz(ema)}/s`  {tier}\n"
        f"  ⏱ **ETA** : `{_eta(eta_s)}`\n"
        f"  ⏳ **Elapsed** : `{_eta(elapsed)}`\n"
        f"  ❌ /cancel to stop"
    )

async def upload_progress(current, total, msg, start, uid: int = 0) -> None:
    await progress(current, total, msg, start, uid=uid, mode="⬆️ Upload")


# ══════════════════════════════════════════════════════════════
#  FFMPEG HELPERS
#  FIX #18 — proc.wait() after every proc.kill() to reap zombies
# ══════════════════════════════════════════════════════════════
async def _kill_proc(proc) -> None:
    """Kill a subprocess and wait for it to avoid zombie processes."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # already dead
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass  # best effort

async def ffmpeg_cut(inp: str, out: str, ss: float, t: float) -> bool:
    """Cut a video segment. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(ss), "-i", inp,
            "-t", str(t), "-c", "copy", "-avoid_negative_ts", "make_zero", out,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=FFMPEG_CUT_TIMEOUT
            )
        except asyncio.TimeoutError:
            await _kill_proc(proc)  # FIX #18
            log.error(f"ffmpeg_cut timed out ({FFMPEG_CUT_TIMEOUT}s): {inp}")
            return False
        if proc.returncode != 0:
            log.error(f"ffmpeg_cut rc={proc.returncode}: {stderr_data.decode()[-400:]}")
        return proc.returncode == 0
    except Exception as e:
        log.error(f"ffmpeg_cut exception: {e}")
        return False

async def make_thumb(video: str, ss: float, out: str) -> str | None:
    """Generate thumbnail image. Returns path or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(ss), "-i", video,
            "-vframes", "1", "-q:v", "2", "-vf", "scale=320:-1", out,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=THUMB_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_proc(proc)  # FIX #18
            return None
        return out if os.path.exists(out) else None
    except Exception:
        return None

async def get_duration(file: str) -> float | None:
    """Get video duration in seconds. Returns None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=FFPROBE_TIMEOUT
            )
        except asyncio.TimeoutError:
            await _kill_proc(proc)  # FIX #18
            log.error(f"ffprobe timed out: {file}")
            return None
        raw = out.decode().strip()
        return float(raw) if raw and raw != "N/A" else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  SPLIT UI
# ══════════════════════════════════════════════════════════════
async def _split_update(msg, done: int, total: int, note: str = "") -> None:
    pct = (done * 100 // total) if total > 0 else 0
    note_line = f"\n  ┗ _{note}_" if note else ""
    await _safe_edit(msg,
        f"✂️ **Splitting…**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_bar(pct, 16)} **{pct}%**\n"
        f"  Part **{done}** / **{total}** done{note_line}\n"
        f"  ❌ /cancel to stop"
    )


# ══════════════════════════════════════════════════════════════
#  UPLOAD PART
# ══════════════════════════════════════════════════════════════
async def _upload_part(message, path: str, num: int, total: int,
                       uid: int, thumb_time: float) -> bool:
    if _get_cancel(uid).is_set(): return False

    # FIX #21 — reject 0-byte or tiny part files before even trying to upload
    part_size = os.path.getsize(path) if os.path.exists(path) else 0
    if part_size < MIN_PART_BYTES:
        log.error(f"Part {num} is too small ({part_size} bytes) — skipping upload.")
        return False

    try:
        status = await message.reply(
            f"⬆️ **Upload** — part {num}/{total}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"[░░░░░░░░░░░░░░░░░░░░]\n"
            f"  🌀 **0%** ·  starting…"
        )
    except Exception as e:
        log.error(f"Could not send upload status for part {num}: {e}")
        return False

    _reset(uid)
    t0 = time.time()
    thumb_path = f"{THUMB_DIR}/thumb_{uid}_{num}.jpg"
    thumb = await make_thumb(path, thumb_time, thumb_path)
    uploaded = False

    for attempt in range(2):  # max 1 FloodWait retry, never retry on other errors
        if _get_cancel(uid).is_set():
            await _safe_edit(status, "🚫 Upload cancelled.")
            break
        try:
            await message.reply_video(
                path,
                caption=f"🎬 **Part {num} / {total}**",
                thumb=thumb,
                progress=upload_progress,
                progress_args=(status, t0, uid),
            )
            uploaded = True
            break  # ✅ NEVER retry after success — prevents double upload

        except FloodWait as e:
            # Safe to retry: FloodWait means Telegram rejected before storing
            wait = e.value + 2
            await _safe_edit(status,
                f"⏳ **Flood wait** — part {num}/{total}\n"
                f"  Resuming in `{wait}s`…"
            )
            await asyncio.sleep(wait)

        except Exception as e:
            # ⚠️ NEVER RETRY — file may already have been sent to Telegram
            # ServerError, RPCError, network errors etc — all treated as no-retry
            log.error(f"Upload part {num} (no retry): {e}")
            await _safe_edit(status, f"❌ Upload failed part {num}: `{e}`")
            break

    _reset(uid)
    if thumb and os.path.exists(thumb_path):
        try: os.remove(thumb_path)
        except: pass
    try: await status.delete()
    except: pass
    return uploaded


# ══════════════════════════════════════════════════════════════
#  CORE SPLIT ENGINE
#  FIX #20 — duration re-fetched inside lock to avoid mismatch
#            when user sends a new video during get_duration call
# ══════════════════════════════════════════════════════════════
async def _do_split(message, uid: int, parts: int,
                    seg_override: float | None = None,
                    label: str = "") -> None:
    """
    seg_override: if given, use as segment length (splitmin/splitsize).
                  If None, compute as dur/parts (split N).
    """
    # Re-validate + re-fetch duration INSIDE the lock
    file = user_files.get(uid)
    if not file or not os.path.exists(file):
        await message.reply("❌ File mil nahi rahi. Dobara video bhejo!")
        return

    dur = await get_duration(file)
    if not dur:
        await message.reply("❌ Video duration nahi mila (inside lock).")
        return

    seg = seg_override if seg_override is not None else dur / parts

    cancel = _get_cancel(uid)
    cancel.clear()
    msg = await message.reply(
        f"✂️ **{label}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_bar(0, 16)} **0%**\n"
        f"  ❌ /cancel to stop"
    )
    try:
        for i in range(parts):
            if cancel.is_set():
                await _safe_edit(msg,
                    f"🚫 **Cancelled!**\n  Stopped after **{i}** / **{parts}** parts.")
                _clear_status(uid)
                return
            ss = i * seg
            _set_status(uid, "Splitting", f"part {i+1}/{parts}")
            await _split_update(msg, i, parts, f"cutting {i+1}/{parts}…")
            out = f"{DOWNLOAD_DIR}/part_{uid}_{i+1}.mp4"
            ok  = await ffmpeg_cut(file, out, ss, seg)
            if not ok or not os.path.exists(out):
                await _safe_edit(msg, f"❌ ffmpeg failed on part {i+1}. Check logs.")
                _clear_status(uid)
                return
            _set_status(uid, "Uploading", f"part {i+1}/{parts}")
            uploaded = await _upload_part(message, out, i+1, parts, uid, ss + seg/2)
            try:
                if os.path.exists(out): os.remove(out)
            except: pass
            if not uploaded:
                await _safe_edit(msg,
                    f"🚫 **Cancelled!**\n  Stopped after **{i+1}** / **{parts}** parts.")
                _clear_status(uid)
                return

        # All parts done
        try:
            if os.path.exists(file): os.remove(file)
        except: pass
        user_files.pop(uid, None)
        _clear_status(uid)
        await _safe_edit(msg,
            f"🏁 **All {parts} parts done!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{_bar(100, 16)} **100%**\n"
            f"  ✅ Complete — send next video!"
        )
        log.info(f"Split done uid={uid} parts={parts}")

    except Exception as e:
        log.error(f"_do_split error uid={uid}: {traceback.format_exc()}")
        _clear_status(uid)
        await _safe_edit(msg, f"❌ Error: `{e}`")


# ══════════════════════════════════════════════════════════════
#  COMMAND LIST — defined before receive handler (FIX #3)
# ══════════════════════════════════════════════════════════════
COMMAND_LIST = [
    "start", "help", "split", "splitmin", "splitsize",
    "info",  "status", "cancel", "clear",
]


# ══════════════════════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════════════════════
@app.on_message(filters.command("start"), group=1)
async def cmd_start(client, message):
    if await _dedup(message): return
    name = getattr(message.from_user, "first_name", "User") or "User"
    await message.reply(
        f"⚡ **ULTRA BOT v3** — ready!\n\n"
        f"👋 Hey **{name}**!\n\n"
        f"📤 Send any **video**, then:\n"
        f"  • `/split 3`       — N equal parts\n"
        f"  • `/splitmin 2`    — chunk every N minutes\n"
        f"  • `/splitsize 500` — chunk every N MB\n\n"
        f"🛠 Other commands:\n"
        f"  • `/info`    — video details\n"
        f"  • `/status`  — current task\n"
        f"  • `/cancel`  — stop task\n"
        f"  • `/clear`   — reset stuck state\n"
        f"  • `/help`    — full help\n\n"
        f"✨ Multi-user · Async ffmpeg · Auto thumbnails\n"
        f"🔄 FloodWait safe · No crash · No double upload"
    )

@app.on_message(filters.command("help"), group=1)
async def cmd_help(client, message):
    if await _dedup(message): return
    await message.reply(
        f"📖 **ULTRA BOT v3 — Help**\n\n"
        f"**Step 1:** Koi bhi video send karo\n"
        f"  _(MP4, MKV, AVI, MOV, WEBM, WMV, 3GP)_\n\n"
        f"**Step 2:** Split command do:\n\n"
        f"  `/split N`       → N equal parts  |  `/split 3`\n"
        f"  `/splitmin N`    → N min chunks   |  `/splitmin 5`\n"
        f"  `/splitsize N`   → N MB chunks    |  `/splitsize 500`\n\n"
        f"**Utils:**\n"
        f"  `/info`   → loaded video ki details\n"
        f"  `/status` → kya chal raha hai\n"
        f"  `/cancel` → rok do\n"
        f"  `/clear`  → stuck state reset karo\n"
    )

@app.on_message(filters.command("info"), group=1)
async def cmd_info(client, message):
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return
    path = user_files.get(uid)
    if not path or not os.path.exists(path):
        return await message.reply("❌ Koi video load nahi hai.\nPehle video bhejo!")
    d  = await get_duration(path)
    sz = os.path.getsize(path)
    opts = ""
    if d:
        for n in [2, 3, 4, 5]:
            opts += f"  `/split {n}` → {n}×{_eta(d/n)}\n"
    await message.reply(
        f"📋 **File Info**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  📁 `{os.path.basename(path)}`\n"
        f"  📦 `{_sz(sz)}`\n"
        f"  🎬 `{_eta(d) if d else 'unknown'}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  💡 Split options:\n{opts}"
        f"  👉 `/split N` · `/splitmin N` · `/splitsize N`"
    )

@app.on_message(filters.command("status"), group=1)
async def cmd_status(client, message):
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return
    info = user_status.get(uid)
    if not _get_lock(uid).locked() or not info:
        path = user_files.get(uid)
        if path and os.path.exists(path):
            return await message.reply(
                f"💤 **Idle — Ready**\n"
                f"  📁 `{os.path.basename(path)}`\n"
                f"  📦 `{_sz(os.path.getsize(path))}`\n"
                f"  👉 `/split N` · `/splitmin N`"
            )
        return await message.reply("💤 **Idle** — send a video!")
    elapsed = _eta(time.time() - info["since"])
    await message.reply(
        f"⚙️ **Running…**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  📌 **Task** : `{info['task']}`\n"
        f"  📝 **Detail** : `{info['detail']}`\n"
        f"  ⏳ **Time** : `{elapsed}`\n\n"
        f"  ❌ /cancel to stop"
    )

@app.on_message(filters.command("cancel"), group=1)
async def cmd_cancel(client, message):
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return
    if not _get_lock(uid).locked():
        return await message.reply("💤 Koi task nahi chal raha.")
    _get_cancel(uid).set()
    await message.reply("🚫 **Cancel requested!**\nStopping at next checkpoint…")

@app.on_message(filters.command("clear"), group=1)
async def cmd_clear(message):
    """Reset stuck user state without bot restart."""
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return
    if _get_lock(uid).locked():
        return await message.reply(
            "⚠️ Task abhi chal raha hai.\n"
            "Pehle `/cancel` karo, phir `/clear`."
        )
    path = user_files.pop(uid, None)
    if path:
        try: os.remove(path)
        except: pass
    _get_cancel(uid).clear()
    _clear_status(uid)
    _reset(uid)
    await message.reply(
        "🗑️ **State cleared!**\n"
        "Sab kuch reset ho gaya.\n"
        "Naya video bhejo 👇"
    )


# ══════════════════════════════════════════════════════════════
#  RECEIVE VIDEO
#  group=-1 ensures this runs BEFORE command handlers (group=1)
#  ~filters.command(COMMAND_LIST) prevents overlap with commands
# ══════════════════════════════════════════════════════════════
@app.on_message(
    filters.incoming & (filters.video | filters.document)
    & ~filters.command(COMMAND_LIST),
    group=-1,
)
async def receive(client, message):
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return

    lock = _get_lock(uid)
    if lock.locked():
        return await message.reply("⏳ Task running.\n👉 /status · /cancel")

    media = message.document or message.video
    if not media: return

    mime      = getattr(media, "mime_type", "") or ""
    file_size = getattr(media, "file_size", 0) or 0

    # Filter: only accept video-like MIME types
    if mime and not (mime.startswith("video/") or mime == "application/octet-stream"):
        return

    ext_map = {
        "video/x-matroska": "mkv", "video/mkv": "mkv",
        "video/avi":         "avi", "video/x-msvideo": "avi",
        "video/webm":       "webm", "video/quicktime": "mov",
        "video/x-ms-wmv":   "wmv", "video/3gpp": "3gp",
    }
    ext    = ext_map.get(mime, "mp4")
    fname  = f"{DOWNLOAD_DIR}/video_{uid}_{message.id}.{ext}"
    sz_str = _sz(file_size) if file_size else "?"

    size_warn = ""
    if file_size and file_size > MAX_FILE_WARN:
        size_warn = f"\n  ⚠️ File `{sz_str}` — near Telegram 2 GB limit!"

    # FIX double message: acquire lock BEFORE sending status reply
    # so only one task ever starts the download for this user
    if lock.locked():
        return  # silently drop — first task already handling it
    async with lock:
        status = await message.reply(
            f"📥 **Downloading…** `{ext.upper()}` · {sz_str}{size_warn}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Starting…"
        )

        old_path = user_files.pop(uid, None)
        if old_path and os.path.exists(old_path):
            try: os.remove(old_path)
            except: pass

        _get_cancel(uid).clear()
        _reset(uid)
        _set_status(uid, "Downloading", sz_str)
        t0 = time.time()

        try:
            path = await message.download(
                file_name=fname,
                progress=progress,
                progress_args=(status, t0, uid, "📥 Download"),
            )
        except Exception as e:
            _clear_status(uid)
            await _safe_edit(status, f"❌ Download failed: `{e}`")
            return

        if not path or not os.path.exists(path):
            _clear_status(uid)
            await _safe_edit(status, "❌ File not saved — try again.")
            return

        _reset(uid)
        _clear_status(uid)
        user_files[uid] = path
        await _safe_edit(status,
            f"✅ **Download complete!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  📁 `{os.path.basename(path)}`\n"
            f"  📦 {_sz(os.path.getsize(path))}  ·  ⏱ {_eta(time.time()-t0)}\n\n"
            f"👉 `/split N`  ·  `/splitmin N`  ·  `/splitsize N`"
        )


# ══════════════════════════════════════════════════════════════
#  SPLIT COMMANDS
#  FIX #20 — duration re-fetched inside _do_split (inside lock)
#            so the seg value is always consistent with the file
# ══════════════════════════════════════════════════════════════
def _is_ready(uid: int) -> bool:
    path = user_files.get(uid)
    return bool(path and os.path.exists(path))

@app.on_message(filters.command("split"), group=1)
async def cmd_split(client, message):
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return
    lock = _get_lock(uid)
    if lock.locked():
        return await message.reply("⏳ Already processing.\n👉 /status · /cancel")
    if not _is_ready(uid):
        return await message.reply("❌ Pehle video bhejo!")
    try:
        parts = int(message.command[1])
        assert 2 <= parts <= 100
    except:
        return await message.reply("❌ Usage: `/split 3`\n  Min 2, max 100.")
    async with lock:
        if not _is_ready(uid):
            return await message.reply("❌ File mil nahi rahi. Dobara bhejo!")
        await _do_split(message, uid, parts,
                        seg_override=None,
                        label=f"Splitting into {parts} equal parts…")

@app.on_message(filters.command("splitmin"), group=1)
async def cmd_splitmin(client, message):
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return
    lock = _get_lock(uid)
    if lock.locked():
        return await message.reply("⏳ Already processing.\n👉 /status · /cancel")
    if not _is_ready(uid):
        return await message.reply("❌ Pehle video bhejo!")
    try:
        mins = int(message.command[1])
        assert 1 <= mins <= 120
    except:
        return await message.reply("❌ Usage: `/splitmin 2`\n  Minutes 1-120.")

    # Quick pre-check (outside lock) — full check happens inside _do_split
    dur_pre = await get_duration(user_files[uid])
    if not dur_pre:
        return await message.reply("❌ Video duration nahi mila.")
    seg   = mins * 60
    parts = math.ceil(dur_pre / seg)
    if parts > 100:
        return await message.reply(f"❌ Too many parts ({parts}). Bada chunk lo.")
    if parts < 2:
        return await message.reply(f"❌ Video {mins} min se chhota hai!")

    async with lock:
        if not _is_ready(uid):
            return await message.reply("❌ File mil nahi rahi. Dobara bhejo!")
        await _do_split(message, uid, parts,
                        seg_override=seg,
                        label=f"{mins} min chunks → {parts} parts…")

@app.on_message(filters.command("splitsize"), group=1)
async def cmd_splitsize(client, message):
    if await _dedup(message): return
    uid = _uid(message)
    if uid is None: return
    lock = _get_lock(uid)
    if lock.locked():
        return await message.reply("⏳ Already processing.\n👉 /status · /cancel")
    if not _is_ready(uid):
        return await message.reply("❌ Pehle video bhejo!")
    try:
        mb = int(message.command[1])
        assert 10 <= mb <= 2000
    except:
        return await message.reply("❌ Usage: `/splitsize 500`\n  MB 10-2000.")

    file     = user_files[uid]
    total_mb = os.path.getsize(file) / 1048576
    dur_pre  = await get_duration(file)
    if not dur_pre:
        return await message.reply("❌ Video duration nahi mila.")
    parts = math.ceil(total_mb / mb)
    if parts > 100:
        return await message.reply(f"❌ Too many parts ({parts}).")
    if parts < 2:
        return await message.reply(f"❌ File already ≤{mb}MB. Split ki zaroorat nahi.")
    seg = dur_pre / parts

    async with lock:
        if not _is_ready(uid):
            return await message.reply("❌ File mil nahi rahi. Dobara bhejo!")
        await _do_split(message, uid, parts,
                        seg_override=seg,
                        label=f"{mb}MB chunks → {parts} parts…")


# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info("🚀 Ultra Bot v3 starting…")
    app.run()
