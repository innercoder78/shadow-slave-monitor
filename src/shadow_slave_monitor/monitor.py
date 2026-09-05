#!/usr/bin/env python3
"""Entry point for Shadow Slave chapter monitoring."""
from __future__ import annotations

import json
import logging
import os
from enum import StrEnum
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from shadow_slave_monitor.config import MONITOR_RESULT_PATH, PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT, PUBLIC_SITE_ORDER, PUBLIC_SITE_WORKERS, PUBLIC_SITES, STATE_PATH, SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT, WEBNOVEL_CHECK_INTERVAL, WEBNOVEL_CHECK_WINDOW, WEBNOVEL_SOURCE
from shadow_slave_monitor.http_client import HttpFetchError, safe_exception_category, safe_exception_details
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import NotificationConfigError, NotificationDeliveryError, merge_pending, pending_due, report_from_pending, send_new_chapter, update_pending_after_failure
from shadow_slave_monitor.parsers import ParseError, check_public_site, check_webnovel
from shadow_slave_monitor.state_manager import StateError, load_state, parse_int, save_state
from shadow_slave_monitor.timeutil import utc_now


class PendingDeliveryOutcome(StrEnum):
    NONE = "none"
    NOT_DELIVERED = "not_delivered"
    DELIVERED = "delivered"

def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def write_result(result: RunResult) -> None:
    MONITOR_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    MONITOR_RESULT_PATH.write_text(json.dumps({"result": result.status.value, "reasons": result.reasons, "degraded_reasons": result.degraded_reasons}, indent=2) + "\n", encoding="utf-8")

def fail_and_save(result: RunResult, state: dict[str, Any] | None, reason: str) -> None:
    result.fail(reason)
    if state is not None:
        try:
            save_state(state)
        except Exception as exc:
            logging.error("Could not persist failed monitor state safely: %s", type(exc).__name__)
            result.fail("repository state could not be safely persisted")

def public_source_name_list(reports: list[ChapterReport]) -> str:
    return ", ".join(report.source for report in reports)

def aggregate_reports_for_chapter(reports: list[ChapterReport], chapter: int) -> ChapterReport | None:
    matching = [r for r in reports if r.chapter == chapter]
    if not matching:
        return None
    matching.sort(key=lambda r: PUBLIC_SITE_ORDER.get(r.source, 999))
    title = next((r.title for r in matching if r.title), None)
    first = matching[0]
    return ChapterReport(
        ", ".join(r.source for r in matching),
        chapter,
        title,
        first.url,
        ",".join(r.strategy for r in matching),
    )

def check_public_sites(
    result: RunResult,
    failure_counts: dict[str, int] | None = None,
    source_positions: dict[str, dict[str, Any]] | None = None,
) -> list[ChapterReport]:
    failure_counts = failure_counts if failure_counts is not None else {}
    source_positions = source_positions if source_positions is not None else {}
    enabled = [s for s in PUBLIC_SITES if s.enabled]
    for site in PUBLIC_SITES:
        if not site.enabled:
            logging.info("Skipping disabled public site: %s.", site.name)
    eligible = []
    for site in enabled:
        if failure_counts.get(site.name, 0) >= PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT:
            logging.info(
                "Skipping %s because it reached the consecutive-failure limit for the current watch cycle.",
                site.name,
            )
        else:
            eligible.append(site)
    if enabled and not eligible:
        result.fail("every enabled public source is suppressed for the current watch cycle")
        return []
    reports_by_name: dict[str, ChapterReport] = {}
    errors: dict[str, Exception] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=min(PUBLIC_SITE_WORKERS, len(eligible) or 1)) as executor:
        futures = {
            executor.submit(
                check_public_site,
                site,
                dict(source_positions[site.name]) if site.name in source_positions else None,
            ) if site.name == "LightNovelUp" else executor.submit(check_public_site, site): site
            for site in eligible
        }
        for future in as_completed(futures):
            site = futures[future]
            try:
                reports_by_name[site.name] = future.result()
            except Exception as exc:
                errors[site.name] = exc
    reports: list[ChapterReport] = []
    for site in eligible:
        if site.name in reports_by_name:
            report = reports_by_name[site.name]
            reports.append(report)
            if report.position_chapter is not None and report.position_url is not None:
                previous = source_positions.get(site.name)
                if previous is None or report.position_chapter > previous["chapter"]:
                    source_positions[site.name] = {
                        "chapter": report.position_chapter,
                        "url": report.position_url,
                    }
            failure_counts.pop(site.name, None)
        else:
            exc = errors[site.name]
            count = min(failure_counts.get(site.name, 0) + 1, PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT)
            failure_counts[site.name] = count
            failures.append(site.name)
            category = safe_exception_category(exc)
            details = safe_exception_details(exc)
            logging.warning(
                "%s check failed safely: category=%s type=%s%s",
                site.name, category, type(exc).__name__, f" {details}" if details else "",
            )
            logging.warning(
                "%s consecutive public-source failures for current watch cycle: %s/%s.",
                site.name, count, PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT,
            )
            if count == PUBLIC_SITE_CONSECUTIVE_FAILURE_LIMIT:
                logging.warning(
                    "%s is now suppressed for the remainder of the current watch cycle.", site.name
                )
    if failures and reports:
        result.degrade("optional public sources failed: " + ", ".join(sorted(failures)))
    if not reports:
        result.fail("every enabled public source failed or produced no trustworthy result")
    reports.sort(key=lambda r: PUBLIC_SITE_ORDER.get(r.source, 999))
    return reports

def set_watch_webnovel(state: dict[str, Any]) -> None:
    state["mode"] = "watch_webnovel"
    state["target_chapter"] = None
    state["target_title"] = None
    state["target_url"] = None
    state["public_source_failures"] = {}

def update_webnovel(state: dict[str, Any], report: ChapterReport, result: RunResult) -> bool:
    previous = parse_int(state.get("latest_webnovel"))
    if previous is not None and report.chapter < previous:
        result.fail(f"WebNovel regression rejected: reported {report.chapter} below stored {previous}")
        logging.error("WebNovel regression rejected: reported=%s stored=%s", report.chapter, previous)
        return False
    state["latest_webnovel"] = report.chapter
    state["latest_webnovel_title"] = report.title
    return True

def force_webnovel_check_enabled() -> bool:
    return os.environ.get("SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK", "").strip().lower() in {"1", "true", "yes", "on"}

def webnovel_check_window_open() -> bool:
    """Return whether the current UTC time is in the deterministic WebNovel check window."""
    now = utc_now()
    elapsed_since_hour = now.minute * 60 + now.second + (now.microsecond / 1_000_000)
    interval_seconds = WEBNOVEL_CHECK_INTERVAL.total_seconds()
    window_seconds = WEBNOVEL_CHECK_WINDOW.total_seconds()
    return elapsed_since_hour % interval_seconds < window_seconds

def webnovel_due() -> bool:
    if force_webnovel_check_enabled():
        logging.info("Forcing WebNovel check because SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK is enabled.")
        return True
    return webnovel_check_window_open()

def required_webnovel_check(state: dict[str, Any], result: RunResult) -> ChapterReport | None:
    try:
        report = check_webnovel(WEBNOVEL_SOURCE)
    except Exception as exc:
        result.fail("WebNovel could not be requested or parsed while needed")
        logging.error("WebNovel check failed safely: category=%s type=%s", safe_exception_category(exc), type(exc).__name__)
        return None
    if not update_webnovel(state, report, result):
        return None
    return report

def try_deliver_pending(state: dict[str, Any], result: RunResult) -> PendingDeliveryOutcome:
    pending = state.get("pending_notification")
    if pending is None:
        return PendingDeliveryOutcome.NONE
    previous_seen, report = report_from_pending(pending)
    latest_seen = parse_int(state.get("latest_seen"))
    if latest_seen is not None and report.chapter <= latest_seen:
        result.fail("pending notification is not newer than latest_seen")
        return PendingDeliveryOutcome.NOT_DELIVERED
    if not pending_due(pending):
        result.fail("required pending chapter notification remains undelivered")
        return PendingDeliveryOutcome.NOT_DELIVERED
    try:
        send_new_chapter(os.environ.get("NTFY_NEWCHAPTER"), previous_seen, report)
    except (NotificationDeliveryError, NotificationConfigError) as exc:
        category = exc.category if isinstance(exc, NotificationDeliveryError) else "configuration_error"
        logging.warning("Pending ntfy delivery failed safely: category=%s status=%s", category, getattr(exc, "status_code", None))
        update_pending_after_failure(pending, exc)
        result.fail("required pending chapter notification remains undelivered")
        return PendingDeliveryOutcome.NOT_DELIVERED
    state["latest_seen"] = report.chapter
    state["latest_title"] = report.title
    state["latest_url"] = report.url
    state["pending_notification"] = None
    set_watch_webnovel(state)
    return PendingDeliveryOutcome.DELIVERED

def queue_or_send(state: dict[str, Any], report: ChapterReport, result: RunResult) -> None:
    previous_seen = parse_int(state.get("latest_seen"))
    try:
        send_new_chapter(os.environ.get("NTFY_NEWCHAPTER"), previous_seen, report)
    except (NotificationDeliveryError, NotificationConfigError) as exc:
        category = exc.category if isinstance(exc, NotificationDeliveryError) else "configuration_error"
        logging.warning("New-chapter ntfy delivery failed safely: category=%s status=%s", category, getattr(exc, "status_code", None))
        state["pending_notification"] = merge_pending(state.get("pending_notification"), previous_seen, report, exc)
        result.fail("required pending chapter notification remains undelivered")
        return
    state["latest_seen"] = report.chapter
    state["latest_title"] = report.title
    state["latest_url"] = report.url
    state["pending_notification"] = None
    set_watch_webnovel(state)

def first_setup(state: dict[str, Any], result: RunResult) -> None:
    logging.info("Running first setup. No notification will be sent.")
    official = required_webnovel_check(state, result)
    if official is None:
        return
    public_reports = check_public_sites(
        result, state.setdefault("public_source_failures", {}), state.setdefault("source_positions", {})
    )
    if result.status == Health.FAILED and not public_reports:
        return
    accepted = [r for r in public_reports if r.chapter <= official.chapter]
    public_latest = max((r.chapter for r in accepted), default=None)
    if public_latest is not None:
        aggregate = aggregate_reports_for_chapter(accepted, public_latest)
        if aggregate:
            state["latest_seen"] = aggregate.chapter
            state["latest_title"] = aggregate.title
            state["latest_url"] = aggregate.url
    if state.get("latest_seen") is None:
        state["latest_seen"] = official.chapter
        state["latest_title"] = official.title
        state["latest_url"] = official.url
    if official.chapter > int(state["latest_seen"]):
        state["mode"] = "watch_free_sites"
        state["target_chapter"] = official.chapter
        state["target_title"] = official.title
        state["target_url"] = official.url
    else:
        set_watch_webnovel(state)

def run_watch_webnovel(state: dict[str, Any], result: RunResult) -> None:
    pending_outcome = try_deliver_pending(state, result)
    if pending_outcome == PendingDeliveryOutcome.DELIVERED:
        return
    if not webnovel_due():
        logging.info("Skipping WebNovel check because this run is outside the deterministic 20-minute UTC check window.")
        return
    official = required_webnovel_check(state, result)
    if official is None:
        return
    latest_seen = parse_int(state.get("latest_seen"))
    pending = state.get("pending_notification")
    pending_latest = parse_int(pending.get("latest_pending_chapter")) if isinstance(pending, dict) else None
    baseline = max(v for v in [latest_seen, pending_latest] if v is not None) if any(v is not None for v in [latest_seen, pending_latest]) else None
    if baseline is not None and official.chapter <= baseline:
        set_watch_webnovel(state)
    else:
        state["public_source_failures"] = {}
        state["mode"] = "watch_free_sites"
        state["target_chapter"] = official.chapter
        state["target_title"] = official.title
        state["target_url"] = official.url
        logging.info("WebNovel is ahead; now watching public sites for chapter %s.", official.chapter)

def run_watch_free_sites(state: dict[str, Any], result: RunResult) -> None:
    pending_outcome = try_deliver_pending(state, result)
    if pending_outcome == PendingDeliveryOutcome.DELIVERED:
        return
    target = parse_int(state.get("target_chapter"))
    if target is None:
        result.fail("critical configuration value target_chapter is missing")
        set_watch_webnovel(state)
        return
    reports = check_public_sites(
        result, state.setdefault("public_source_failures", {}), state.setdefault("source_positions", {})
    )
    if not reports:
        return
    highest = max(r.chapter for r in reports)
    official = None
    if highest > target:
        logging.warning("Public source reported %s above target %s; rechecking WebNovel.", highest, target)
        official = required_webnovel_check(state, result)
        if official is None:
            return
        if official.chapter > target:
            target = official.chapter
            state["target_chapter"] = target
            state["target_title"] = official.title
            state["target_url"] = official.url
        else:
            if highest > target + SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT:
                result.fail("suspicious public chapter jump rejected")
            logging.warning("Rejecting public reports above WebNovel-confirmed target %s.", target)
    exact = aggregate_reports_for_chapter(reports, target)
    if exact is None:
        if all(report.chapter > target for report in reports):
            result.fail("every enabled public source produced no trustworthy result for the WebNovel target")
        logging.info("No public source exactly matches target chapter %s.", target)
        return
    latest_webnovel = parse_int(state.get("latest_webnovel"))
    if latest_webnovel is not None and exact.chapter > latest_webnovel:
        result.fail("public source attempted to advance beyond WebNovel")
        return
    if state.get("pending_notification") is None:
        queue_or_send(state, exact, result)
    else:
        state["pending_notification"] = merge_pending(state.get("pending_notification"), parse_int(state.get("latest_seen")), exact)
        try_deliver_pending(state, result)

def main() -> None:
    configure_logging()
    result = RunResult()
    state: dict[str, Any] | None = None
    try:
        state, first = load_state(STATE_PATH)
        original_state_json = json.dumps(state, sort_keys=True, default=str)
        if first:
            first_setup(state, result)
        elif state.get("mode") == "watch_free_sites":
            run_watch_free_sites(state, result)
        elif state.get("mode") == "watch_webnovel":
            run_watch_webnovel(state, result)
        else:
            result.fail("critical configuration value mode is invalid")
        if state is not None and json.dumps(state, sort_keys=True, default=str) != original_state_json:
            save_state(state)
        elif state is not None:
            logging.info("No operational state changes to persist.")
    except StateError as exc:
        result.fail(f"repository state could not be read or safely persisted: {exc}")
        logging.error("State validation failed closed: %s", exc)
    except Exception as exc:
        result.fail(f"unexpected monitor failure: {type(exc).__name__}")
        logging.error("Unexpected monitor failure safely contained: type=%s", type(exc).__name__)
    finally:
        write_result(result)
    if result.status == Health.FAILED:
        raise SystemExit(2)

if __name__ == "__main__":
    main()
