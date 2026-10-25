"""Dependency-free validation shared by emergency discovery and the state writer."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from shadow_slave_monitor.config import MIN_CHAPTER, MAX_CHAPTER


def telegram_emergency_evidence_url(url: str, chapter: int, base_url: str) -> bool:
    """Accept canonical numbered chapter links or configured-channel posts only."""
    if (type(chapter) is not int or not MIN_CHAPTER <= chapter <= MAX_CHAPTER
            or not isinstance(url, str) or len(url) > 2048):
        return False
    linked = re.fullmatch(r"https://(?:www\.)?telegra\.ph/([1-9][0-9]{2,4})-([a-zA-Z0-9_-]+)/?", url)
    if linked:
        return int(linked[1]) == chapter and bool(re.search(r"[a-zA-Z]", linked[2]))
    channel = urlparse(base_url).path.removeprefix("/s/").strip("/")
    return bool(re.fullmatch(rf"https://t\.me/(?:s/)?{re.escape(channel)}/[1-9][0-9]*", url))
