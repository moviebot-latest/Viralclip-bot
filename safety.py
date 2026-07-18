"""
safety.py
Basic automated content safety check.
This is a first line of defense, NOT a legal guarantee.
Bot decides itself; only on low confidence / flagged case does
it get routed to admin (via the appeal system in bot.py after
a user disputes an automatic rejection).
"""

import re

# Very small illustrative keyword sets. Extend over time based on logs.
COPYRIGHT_FLAG_KEYWORDS = [
    "full movie", "full episode", "pirated", "leaked movie",
    "official soundtrack full album",
]

NSFW_FLAG_KEYWORDS = [
    "nsfw", "18+", "xxx", "onlyfans",
]


def check_metadata_safety(title: str, description: str = "") -> dict:
    """
    Returns {"safe": bool, "reason": str or None}
    Cheap heuristic check on title/description text before any download.
    """
    text = f"{title} {description}".lower()

    for kw in NSFW_FLAG_KEYWORDS:
        if kw in text:
            return {"safe": False, "reason": f"Possible NSFW content (matched: '{kw}')"}

    for kw in COPYRIGHT_FLAG_KEYWORDS:
        if kw in text:
            return {"safe": False, "reason": f"Possible copyrighted/pirated content (matched: '{kw}')"}

    return {"safe": True, "reason": None}


def check_duration(duration_seconds: float, max_seconds: int = 2 * 3600) -> dict:
    if duration_seconds > max_seconds:
        return {
            "ok": False,
            "reason": f"Video is {duration_seconds/3600:.1f}h, max supported is {max_seconds/3600:.0f}h",
        }
    return {"ok": True, "reason": None}
