"""
database.py
All SQLite schema + async helper functions for the bot.
Uses aiosqlite so nothing blocks the event loop.
"""

import aiosqlite
from datetime import datetime, date

DB_PATH = "bot_data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER,
    date TEXT,
    videos_used INTEGER DEFAULT 0,
    bonus_credits INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS user_limits (
    user_id INTEGER PRIMARY KEY,
    max_file_size INTEGER DEFAULT 2147483648  -- 2GB default
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    user_id INTEGER,
    source_url TEXT,
    status TEXT DEFAULT 'pending',   -- pending/processing/done/failed/rejected
    style TEXT DEFAULT 'style_2',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS clips (
    clip_id TEXT PRIMARY KEY,
    job_id TEXT,
    file_path TEXT,
    virality_score INTEGER,
    reasoning TEXT,
    hook_text TEXT,
    suggested_platform TEXT,
    start_time REAL,
    end_time REAL,
    feedback INTEGER DEFAULT 0  -- -1 down, 0 none, 1 up
);

CREATE TABLE IF NOT EXISTS pending_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    video_source TEXT,
    bot_reason TEXT,
    status TEXT DEFAULT 'pending',  -- pending/approved/rejected
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS appeal_count (
    user_id INTEGER,
    date TEXT,
    count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS download_cache (
    url_hash TEXT PRIMARY KEY,
    video_path TEXT,
    transcript TEXT,
    created_at TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _today():
    return date.today().isoformat()


# ---------- usage / limits ----------

async def get_usage(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT videos_used, bonus_credits FROM usage WHERE user_id=? AND date=?",
            (user_id, _today()),
        )
        row = await cur.fetchone()
        if row:
            return {"videos_used": row[0], "bonus_credits": row[1]}
        return {"videos_used": 0, "bonus_credits": 0}


async def can_process(user_id: int, free_limit: int = 5) -> bool:
    usage = await get_usage(user_id)
    limit = free_limit + usage["bonus_credits"]
    return usage["videos_used"] < limit


async def increment_usage(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO usage (user_id, date, videos_used, bonus_credits)
               VALUES (?, ?, 1, 0)
               ON CONFLICT(user_id, date)
               DO UPDATE SET videos_used = videos_used + 1""",
            (user_id, _today()),
        )
        await db.commit()


async def add_bonus_credits(user_id: int, count: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO usage (user_id, date, videos_used, bonus_credits)
               VALUES (?, ?, 0, ?)
               ON CONFLICT(user_id, date)
               DO UPDATE SET bonus_credits = bonus_credits + ?""",
            (user_id, _today(), count, count),
        )
        await db.commit()


async def set_max_file_size(user_id: int, size_bytes: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO user_limits (user_id, max_file_size) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET max_file_size = ?""",
            (user_id, size_bytes, size_bytes),
        )
        await db.commit()


async def get_max_file_size(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT max_file_size FROM user_limits WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 2 * 1024 ** 3  # 2GB default


# ---------- jobs / clips ----------

async def create_job(job_id: str, user_id: int, source_url: str, style: str = "style_2"):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO jobs (job_id, user_id, source_url, status, style, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
            (job_id, user_id, source_url, style, now, now),
        )
        await db.commit()


async def update_job_status(job_id: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE job_id=?",
            (status, datetime.utcnow().isoformat(), job_id),
        )
        await db.commit()


async def save_clip(clip_id, job_id, file_path, score, reasoning, hook_text, platform, start, end):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO clips (clip_id, job_id, file_path, virality_score, reasoning,
               hook_text, suggested_platform, start_time, end_time)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (clip_id, job_id, file_path, score, reasoning, hook_text, platform, start, end),
        )
        await db.commit()


async def get_user_history(user_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT j.job_id, j.source_url, j.created_at, j.status
               FROM jobs j WHERE j.user_id=? ORDER BY j.created_at DESC LIMIT ?""",
            (user_id, limit),
        )
        return await cur.fetchall()


async def set_clip_feedback(clip_id: str, value: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clips SET feedback=? WHERE clip_id=?", (value, clip_id))
        await db.commit()


# ---------- appeal / review system ----------

async def get_appeal_count_today(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT count FROM appeal_count WHERE user_id=? AND date=?",
            (user_id, _today()),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def increment_appeal_count(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO appeal_count (user_id, date, count) VALUES (?, ?, 1)
               ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1""",
            (user_id, _today()),
        )
        await db.commit()


async def create_review_request(user_id: int, video_source: str, reason: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO pending_review (user_id, video_source, bot_reason, status, timestamp)
               VALUES (?, ?, ?, 'pending', ?)""",
            (user_id, video_source, reason, datetime.utcnow().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def get_review(review_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, video_source, bot_reason, status FROM pending_review WHERE id=?",
            (review_id,),
        )
        return await cur.fetchone()


async def set_review_status(review_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_review SET status=? WHERE id=?", (status, review_id)
        )
        await db.commit()


# ---------- download cache ----------

async def get_cached_download(url_hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT video_path, transcript FROM download_cache WHERE url_hash=?",
            (url_hash,),
        )
        return await cur.fetchone()


async def save_cached_download(url_hash: str, video_path: str, transcript: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO download_cache (url_hash, video_path, transcript, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(url_hash) DO UPDATE SET video_path=?, transcript=?""",
            (url_hash, video_path, transcript, datetime.utcnow().isoformat(),
             video_path, transcript),
        )
        await db.commit()
