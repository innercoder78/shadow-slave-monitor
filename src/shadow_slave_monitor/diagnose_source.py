"""Read-only manual check of one exact configured public source name."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from shadow_slave_monitor.config import PUBLIC_SITES, STATE_PATH
from shadow_slave_monitor.diagnostics import diagnostic_summary
from shadow_slave_monitor.http_client import HttpFetchError
from shadow_slave_monitor.parsers import ParseError, check_public_site
from shadow_slave_monitor.state_manager import StateError, load_state, parse_int


def diagnose_source(name: str, *, state_path: Path = STATE_PATH) -> int:
    site = next((source for source in PUBLIC_SITES if source.name == name), None)
    if site is None or not site.enabled:
        # Do not echo the untrusted workflow input, even on invalid requests.
        logging.error("Invalid source name; enter an exact enabled public source name from config.py.")
        return 2
    try:
        state, _ = load_state(state_path)
    except (StateError, OSError):
        logging.error("Diagnostic setup failed: code=DIAGNOSTIC_STATE_ERROR stage=setup")
        return 2
    expected = parse_int(state.get("target_chapter")) if state.get("mode") == "watch_free_sites" else None
    try:
        report = check_public_site(
            site, expected, state.get("target_title") if expected is not None else None,
            parse_int(state.get("latest_seen")) if expected is not None else None,
            state.get("latest_title") if expected is not None else None,
        )
    except (ParseError, HttpFetchError, requests.RequestException) as exc:
        logging.warning("Diagnostic outcome: %s", diagnostic_summary(site, exc))
        # Expected source failures are results, not workflow infrastructure failures.
        return 0
    except Exception:
        logging.error("Diagnostic infrastructure failure: code=DIAGNOSTIC_INTERNAL_ERROR stage=internal")
        return 2
    if report is None and site.name == "Rentry":
        code = "TARGET_NOT_AVAILABLE" if expected is not None else "TARGET_CONTEXT_MISSING"
        logging.info("Diagnostic outcome: source=%r code=%s stage=complete", site.name, code)
        return 0
    logging.info("Diagnostic outcome: source=%r code=SOURCE_OK stage=complete chapter=%s", site.name, report.chapter)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return diagnose_source(os.environ.get("SOURCE_NAME", ""))


if __name__ == "__main__":
    raise SystemExit(main())
