"""Central configuration for the Shadow Slave monitor."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import os
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPOSITORY_ROOT / "state" / "state.json"
WATCHDOG_STATE_PATH = REPOSITORY_ROOT / "state" / "watchdog_state.json"

def _temporary_result_path(env_name: str, filename: str) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured)
    temp_root = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    return temp_root / "shadow-slave-monitor" / filename

MONITOR_RESULT_PATH = _temporary_result_path("SHADOW_SLAVE_MONITOR_RESULT_PATH", "run_result.json")
WATCHDOG_RESULT_PATH = _temporary_result_path("SHADOW_SLAVE_WATCHDOG_RESULT_PATH", "watchdog_result.json")
PYTHON_VERSION = "3.12"

WEBNOVEL_CATALOG_URL = "https://www.webnovel.com/book/22196546206090805/catalog"
WEBNOVEL_CHECK_INTERVAL = timedelta(minutes=20)
WEBNOVEL_CHECK_WINDOW = timedelta(minutes=8)
SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT = 25
PUBLIC_SITE_WORKERS = 6
MIN_CHAPTER = 1
MAX_CHAPTER = 10000
TITLE_MAX_LENGTH = 180
NTFY_BODY_MAX_BYTES = 3900
MAX_HTML_BYTES = 5 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 20
HTTP_RETRIES = 3
HTTP_BACKOFF_SECONDS = 2

NTFY_BASE_URL = "https://ntfy.sh"
NTFY_CONNECT_TIMEOUT_SECONDS = 5
NTFY_READ_TIMEOUT_SECONDS = 20
PENDING_RETRY_DELAYS_SECONDS = (5 * 60, 10 * 60, 20 * 60, 40 * 60)
PENDING_RETRY_MAX_DELAY_SECONDS = 60 * 60

WATCHDOG_STALE_HOURS = 5
WATCHDOG_REPEAT_ALERT_HOURS = 12
WATCHDOG_IN_PROGRESS_GRACE_MINUTES = 30
MONITOR_WORKFLOW_FILE = "monitor.yml"
MONITOR_WORKFLOW_PATH = ".github/workflows/monitor.yml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    url: str
    enabled: bool
    allowed_hosts: tuple[str, ...]

WEBNOVEL_SOURCE = SourceConfig(
    "WebNovel",
    WEBNOVEL_CATALOG_URL,
    True,
    ("www.webnovel.com", "webnovel.com"),
)

PUBLIC_SITES: tuple[SourceConfig, ...] = (
    SourceConfig("Light Novel World", "https://lightnovelworld.org/novel/shadow-slave", True, ("lightnovelworld.org", "www.lightnovelworld.org")),
    SourceConfig("Telegram", "https://t.me/s/shadow_slave_fastes", True, ("t.me", "telegram.me", "www.t.me")),
    SourceConfig("Novel Buddy", "https://novelbuddy.me/shadow-slave", True, ("novelbuddy.me", "www.novelbuddy.me")),
    SourceConfig("NovelArrow", "https://novelarrow.com/novel/shadow-slave", False, ("novelarrow.com", "www.novelarrow.com")),
    SourceConfig("NovelFire", "https://novelfire.net/book/shadow-slave", False, ("novelfire.net", "www.novelfire.net")),
    SourceConfig("NovelBin", "https://novelbin.com/b/shadow-slave", False, ("novelbin.com", "www.novelbin.com")),
    SourceConfig("SSNovel", "https://ssnovel.app", True, ("ssnovel.app", "www.ssnovel.app")),
    SourceConfig("NovelFull", "https://novelfull.com/shadow-slave.html", True, ("novelfull.com", "www.novelfull.com")),
)

PUBLIC_SITE_ORDER = {site.name: index for index, site in enumerate(PUBLIC_SITES)}
