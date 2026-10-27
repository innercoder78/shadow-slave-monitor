"""Emergency authority tests using synthetic messages and mocked delivery only."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup

from shadow_slave_monitor import monitor, parsers, watchdog
from shadow_slave_monitor.config import MAX_CHAPTER, MAX_WEBNOVEL_CYCLE, PUBLIC_SITES
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import NotificationDeliveryError, notification_body, pending_from_report
from shadow_slave_monitor.state_manager import (
    StateError, initial_state, initial_watchdog_state, load_state, save_state, validate_state,
)
from test_state_writer import state_writer

SOURCE = next(site for site in PUBLIC_SITES if site.name == "Telegram")
START = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


def observed_messages():
    """Sanitized live structure, not the unavailable incident response body."""
    return BeautifulSoup(
        (Path(__file__).parent / "fixtures/telegram_emergency_grouped.html").read_text(encoding="utf-8"),
        "html.parser",
    )


def message(chapter=3226, title="Synthetic Chapter", *, doc=True, link=True):
    return (f'<div class="tgme_widget_message" data-post="shadow_slave_fastes/{chapter}">'
            + (f'<a class="tgme_widget_message_document_wrap" href="https://t.me/shadow_slave_fastes/{chapter}">'
               f'<span class="tgme_widget_message_document_title">{chapter} {title}.docx</span></a>' if doc else "")
            + (f'<a href="https://telegra.ph/{chapter}-{title.replace(" ", "-")}-10-25">Read chapter</a>' if link else "")
            + '</div>')


def multi_message(*chapters, doc=True, link=True):
    parts = [message(chapter, f"Synthetic Chapter {chapter}", doc=doc, link=link)
             .partition(">")[2].removesuffix("</div>") for chapter in chapters]
    return ('<div class="tgme_widget_message" data-post="shadow_slave_fastes/9000">'
            + "".join(parts) + '</div>')


def initialized():
    state = initial_state()
    state.update(latest_seen=3225, latest_webnovel=3225, latest_title="Baseline",
                 latest_webnovel_title="Baseline", latest_url=SOURCE.url)
    return state


def active():
    state = initialized()
    state.update(webnovel_failures=4, webnovel_failure_cycle=int(START.timestamp() // 1200) - 1,
                 emergency_last_check_ok=True)
    return state


class EmergencyMonitorTests(unittest.TestCase):
    def setUp(self):
        # Fail the test before any unexpected network/delivery dispatch.
        self.fetch = self.enterContext(patch.object(parsers, "fetch_html", return_value=message(3225)))
        self.official = self.enterContext(patch.object(monitor, "check_webnovel", side_effect=requests.Timeout("private")))
        self.send = self.enterContext(patch.object(monitor, "send_new_chapter"))
        self.public = self.enterContext(patch.object(monitor, "check_public_sites", side_effect=AssertionError("unexpected fanout")))
        self.enterContext(patch.object(monitor, "check_public_site", side_effect=AssertionError("unexpected public dispatch")))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")))
        self.enterContext(patch.dict(monitor.os.environ, {"NTFY_NEWCHAPTER": "test-topic"}))

    def tick(self, state, minute=0, *, force=False, official=None, html=None, error=None):
        now = START + timedelta(minutes=minute)
        self.official.side_effect = None if official is not None else (error or requests.Timeout("private"))
        if official is not None:
            self.official.return_value = ChapterReport("WebNovel", official, "Baseline", "https://official.example/catalog")
        if html is not None:
            self.fetch.return_value = html
        result = RunResult()
        with patch.object(monitor, "utc_now", return_value=now), \
             patch("shadow_slave_monitor.notifications.utc_now", return_value=now), \
             patch.dict(monitor.os.environ, {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": str(force)}):
            monitor.run_watch_webnovel(state, result)
        # Every independent run must be serializable and accepted on reload.
        self.assertEqual(validate_state(json.loads(json.dumps(state))), state)
        return result

    def test_observed_grouped_history_then_new_release_notifies_once_and_recovers(self):
        soup = observed_messages()
        newest = soup.select_one('[data-post="shadow_slave_fastes/2342"]').extract()
        state = active()
        state.update(latest_seen=3226, latest_webnovel=3226)
        self.assertEqual(self.tick(state, html=str(soup)).status, Health.DEGRADED)
        self.send.assert_not_called()
        self.assertEqual(self.tick(state, 20, html=str(soup) + str(newest)).status, Health.DEGRADED)
        self.assertEqual(state["latest_seen"], 3227)
        self.assertEqual(state["latest_webnovel"], 3226)
        provenance = copy.deepcopy(state["emergency_provenance"])
        self.assertEqual(provenance["chapter"], 3227)
        self.tick(state, 40)
        self.tick(state, 45, official=3227)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(state["webnovel_failures"], 0)
        self.assertEqual(state["emergency_provenance"], provenance)

    def test_single_media_document_provenance_survives_save_load(self):
        soup = observed_messages()
        grouped = soup.select_one('[data-post="shadow_slave_fastes/2331"]')
        grouped.select_one('.tgme_widget_message_text').decompose()
        state = active()
        state.update(latest_seen=3216, latest_webnovel=3216)
        self.tick(state, html=str(grouped))
        self.assertEqual(state["latest_seen"], 3218)
        self.assertEqual(state["emergency_provenance"]["evidence_url"],
                         "https://t.me/shadow_slave_fastes/2332?single")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(state, path)
            loaded, _ = load_state(path)
        self.assertIsNotNone(loaded["updated_at"])
        self.assertEqual(loaded, {**state, "updated_at": loaded["updated_at"]})
        self.tick(loaded, 20)
        self.assertEqual(self.send.call_count, 1)

    def test_four_distinct_failed_cycles_activate_in_same_run_and_only_one_poll_per_cycle(self):
        state = initialized()
        for index, minute in enumerate((0, 20, 40, 60), 1):
            result = self.tick(state, minute)
            self.assertEqual(state["webnovel_failures"], index)
            self.assertEqual(self.fetch.call_count, int(index == 4))
            self.assertEqual(result.status, Health.DEGRADED if index == 4 else Health.FAILED)
            before = copy.deepcopy(state)
            for delta in (5, 10, 15):
                skipped = self.tick(state, minute + delta)
                self.assertEqual(state, before)
                self.assertEqual(skipped.status, result.status)
            self.assertEqual(self.official.call_count, index * 2)
        self.assertEqual(self.tick(state, 80).status, Health.DEGRADED)
        self.assertEqual(self.official.call_count, 9)
        self.assertEqual(self.fetch.call_count, 2)
        self.public.assert_not_called()
        self.send.assert_not_called()

    def test_failure_count_persists_across_real_save_load_boundaries(self):
        state = initialized()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for index in range(4):
                self.tick(state, index * 20)
                save_state(state, path)
                state, first = load_state(path)
                self.assertFalse(first)
                self.assertEqual(state["webnovel_failures"], index + 1)
        self.assertTrue(monitor.emergency_active(state))

    def test_healthy_equal_report_resets_before_four_and_never_dispatches(self):
        state = initialized()
        for minute in (0, 20, 40):
            self.tick(state, minute)
        self.assertEqual(self.tick(state, 60, official=3225).status, Health.HEALTHY)
        self.assertEqual(state["webnovel_failures"], 0)
        self.tick(state, 80)
        self.assertEqual(state["webnovel_failures"], 1)
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_http_network_response_and_parse_errors_all_count_as_genuine_failed_cycles(self):
        errors = [requests.ConnectionError("private"), requests.Timeout("private"),
                  parsers.ParseError("chapter_candidates_ambiguous")]
        from shadow_slave_monitor.http_client import HttpFetchError
        errors.append(HttpFetchError("unexpected_content_type"))
        for status in (403, 429, 500, 503):
            response = requests.Response()
            response.status_code = status
            errors.append(requests.HTTPError("private", response=response))
        for error in errors:
            state = initialized()
            with self.subTest(error=type(error).__name__), self.assertLogs(level="ERROR") as logs:
                self.tick(state, error=error)
            self.assertEqual(state["webnovel_failures"], 1)
            self.assertNotIn("private", "\n".join(logs.output))

    def test_main_saves_failed_cycle_and_does_not_save_duplicate_or_off_window_runs(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(initialized(), path)
            with patch.object(monitor, "STATE_PATH", path), \
                 patch.object(monitor, "save_state", side_effect=lambda state: save_state(state, path)) as saved, \
                 patch.object(monitor, "write_result"), \
                 patch.dict(monitor.os.environ, {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": "false"}):
                for minute in (0, 5, 10, 15):
                    with patch.object(monitor, "utc_now", return_value=START + timedelta(minutes=minute)), \
                         self.assertRaisesRegex(SystemExit, "2"):
                        monitor.main()
            self.assertEqual(saved.call_count, 1)
            self.assertEqual(load_state(path)[0]["webnovel_failures"], 1)

    def test_success_marker_changes_once_per_cycle_rechecks_and_skips_are_unchanged(self):
        state = initialized()
        self.tick(state, 0, official=3225)
        self.assertEqual(state["webnovel_success_cycle"], int(START.timestamp() // 1200))
        before = copy.deepcopy(state)
        for minute in (5, 10, 15):
            self.tick(state, minute, official=3225)
        self.assertEqual(state, before)
        self.tick(state, 20, official=3225)
        self.assertEqual(state["webnovel_success_cycle"], before["webnovel_success_cycle"] + 1)
        before = copy.deepcopy(state)
        self.tick(state, 25, official=3225)
        self.assertEqual(state, before)
        self.fetch.assert_not_called()

    def test_success_then_failure_in_same_cycle_cannot_extend_prior_failure_streak(self):
        state = initialized()
        state.update(webnovel_failures=3, webnovel_failure_cycle=int(START.timestamp() // 1200) - 1)
        self.tick(state, 0, official=3225)
        self.tick(state, 5)
        self.assertEqual(state["webnovel_failures"], 0)
        self.fetch.assert_not_called()
        for index, minute in enumerate((20, 40, 60), 1):
            self.tick(state, minute)
            self.assertEqual(state["webnovel_failures"], index)
            self.tick(state, minute + 5)
            self.assertEqual(state["webnovel_failures"], index)
            self.fetch.assert_not_called()
        self.tick(state, 80)
        self.assertEqual(self.fetch.call_count, 1)

    def test_failure_then_success_resets_streak_and_later_cycles_start_from_one(self):
        state = initialized()
        self.tick(state, 0)
        self.assertEqual(state["webnovel_failures"], 1)
        self.tick(state, 5, official=3225)
        self.assertEqual(state["webnovel_failures"], 0)
        self.tick(state, 6, force=True)
        self.assertEqual(state["webnovel_failures"], 0)
        self.tick(state, 20)
        self.assertEqual(state["webnovel_failures"], 1)
        self.assertEqual(self.official.call_count, 4)
        self.fetch.assert_not_called()

    def test_ordinary_recheck_recovers_active_emergency_inside_failed_cycle(self):
        state = active()
        self.tick(state, 0, html=message())
        self.assertEqual(self.send.call_count, 1)
        provenance = copy.deepcopy(state["emergency_provenance"])
        self.tick(state, 5, official=3225)
        self.assertEqual(state["webnovel_failures"], 0)
        self.assertEqual(state["latest_seen"], 3226)
        self.assertEqual(state["emergency_provenance"], provenance)
        self.tick(state, 6, force=True)
        self.assertEqual(state["webnovel_failures"], 0)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.send.call_count, 1)

    def test_success_and_failure_cycle_semantics_survive_process_restarts_in_either_order(self):
        for success_first in (True, False):
            with self.subTest(success_first=success_first), TemporaryDirectory() as tmp:
                path = Path(tmp) / "state.json"
                state = initialized()
                self.tick(state, 0, official=3225 if success_first else None)
                save_state(state, path)
                state, _ = load_state(path)
                self.tick(state, 5, official=None if success_first else 3225)
                save_state(state, path)
                state, _ = load_state(path)
                self.assertEqual(state["webnovel_failures"], 0)
                self.assertEqual(state["webnovel_success_cycle"], int(START.timestamp() // 1200))
                self.tick(state, 20)
                self.assertEqual(state["webnovel_failures"], 1)
        self.fetch.assert_not_called()

    def test_success_marker_is_persisted_once_per_cycle_without_five_minute_writes(self):
        self.official.side_effect = None
        self.official.return_value = ChapterReport("WebNovel", 3225, "Baseline", "https://official.example/catalog")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(initialized(), path)
            with patch.object(monitor, "STATE_PATH", path), \
                 patch.object(monitor, "save_state", side_effect=lambda state: save_state(state, path)) as saved, \
                 patch.object(monitor, "write_result"), \
                 patch.dict(monitor.os.environ, {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": "false"}):
                for minute in (0, 5, 10, 15, 20, 25):
                    with patch.object(monitor, "utc_now", return_value=START + timedelta(minutes=minute)):
                        monitor.main()
            self.assertEqual(saved.call_count, 2)
            self.assertEqual(self.official.call_count, 4)

    def test_end_of_eight_minute_window_can_recover_without_changing_window(self):
        state = active()
        self.tick(state, 0)
        self.tick(state, 7.99, official=3225)
        self.assertEqual(state["webnovel_failures"], 0)
        before = copy.deepcopy(state)
        self.tick(state, 8)
        self.assertEqual(state, before)
        self.assertEqual(self.official.call_count, 2)

    def test_force_off_window_fails_without_counting_and_can_recover(self):
        state = active()
        self.tick(state)
        self.fetch.reset_mock()
        before = copy.deepcopy(state)
        for minute in (10, 15):
            self.assertEqual(self.tick(state, minute, force=True).status, Health.DEGRADED)
            self.assertEqual(state, before)
        self.fetch.assert_not_called()
        self.tick(state, 15, force=True, official=3225)
        self.assertEqual(state["webnovel_failures"], 0)
        self.assertIsNone(state["emergency_last_check_ok"])

    def test_no_current_cycle_check_does_not_reuse_stale_fallback_health(self):
        state = active()
        self.tick(state)
        before = copy.deepcopy(state)
        self.assertEqual(self.tick(state, 30).status, Health.FAILED)
        self.assertEqual(state, before)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.official.call_count, 1)

    def test_untrusted_report_at_dispatch_boundary_cannot_notify(self):
        for report in (ChapterReport("Other", 3226, "Synthetic", "https://telegra.ph/3226-Synthetic"),
                       ChapterReport("Telegram", True, "Synthetic", SOURCE.url),
                       ChapterReport("Telegram", 3226, "Synthetic", "https://evil.example/3226")):
            state = active()
            with self.subTest(report=report), patch.object(monitor, "check_telegram_emergency", return_value=report):
                self.assertEqual(self.tick(state).status, Health.FAILED)
                self.assertEqual(state["latest_seen"], 3225)
        self.send.assert_not_called()

    def test_force_inside_same_cycle_does_not_recount_or_repoll(self):
        state = active()
        self.tick(state)
        self.tick(state, 5, force=True)
        self.assertEqual(self.official.call_count, 2)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(state["webnovel_failures"], 4)
        self.tick(state, 5, force=True, official=3225)
        self.tick(state, 6, force=True)
        self.assertEqual(state["webnovel_failures"], 0)
        self.assertEqual(self.fetch.call_count, 1)

    def test_missing_runs_do_not_count_as_failed_cycles(self):
        state = initialized()
        self.tick(state)
        self.tick(state, 7 * 24 * 60)
        self.assertEqual(state["webnovel_failures"], 2)
        self.fetch.assert_not_called()

    def test_multiday_outage_has_no_expiration_counter_growth_or_total_chapter_cap(self):
        state = active()
        for index in range(101):
            result = self.tick(state, index * 24 * 60, html=message(3226 + index))
            self.assertEqual(result.status, Health.DEGRADED)
            self.assertEqual(state["webnovel_failures"], 4)
            self.assertEqual(state["latest_seen"], 3226 + index)
        self.assertEqual(state["latest_webnovel"], 3225)
        self.assertEqual(self.send.call_count, 101)
        self.assertEqual(self.official.call_count, 101)

    def test_multi_chapter_releases_during_extended_outage_notify_only_new_ranges(self):
        state = active()
        for day in range(40):
            first = 3226 + day * 2
            # The post also repeats a previously notified chapter.
            html = multi_message(first - 1, first, first + 1)
            self.tick(state, day * 24 * 60, html=html)
            self.assertEqual(state["latest_seen"], first + 1)
            self.assertEqual(self.send.call_args.args[1], first - 1)
            self.assertIn("2 new chapters", notification_body(*self.send.call_args.args[1:]))
            self.tick(state, day * 24 * 60 + 5, html=html)
            self.assertEqual(self.send.call_count, day + 1)
            self.assertEqual(state["webnovel_failures"], 4)
        self.assertEqual(state["latest_webnovel"], 3225)

    def test_multiple_releases_merge_into_pending_range_without_retry_timer_reset(self):
        state = active()
        self.send.side_effect = NotificationDeliveryError("http_error", 503)
        self.tick(state, 0, html=multi_message(3226, 3227))
        pending = copy.deepcopy(state["pending_notification"])
        with patch.object(monitor, "pending_due", return_value=False):
            self.tick(state, 20, html=multi_message(3227, 3228, 3229))
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(state["pending_notification"]["latest_pending_chapter"], 3229)
        self.assertEqual(state["pending_notification"]["next_retry_at"], pending["next_retry_at"])
        self.send.side_effect = None
        self.tick(state, 25)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(state["latest_seen"], 3229)

    def test_new_then_equal_old_and_delayed_chapter_notifications_keep_source_and_gap(self):
        state = active()
        for minute, chapter in ((0, 3225), (20, 3226), (40, 3226), (60, 3225), (80, 3228)):
            self.tick(state, minute, html=message(chapter))
        self.assertEqual(self.send.call_count, 2)
        topic, previous, report = self.send.call_args.args
        self.assertEqual((topic, previous, report.source, report.chapter, report.url),
                         ("test-topic", 3226, "Telegram", 3228, SOURCE.url))
        self.assertIn("2 new chapters", notification_body(previous, report))
        self.assertIn(f"Telegram [{SOURCE.url}]", notification_body(previous, report))
        self.assertEqual(state["emergency_provenance"]["chapter"], 3228)

    def test_invalid_or_failed_emergency_check_is_failed_and_remains_failed_on_skips(self):
        for html in ("<p>Chapter 9999 unrelated text</p>", message(9999)):
            with self.subTest(html=html[:50]):
                state = active()
                self.assertEqual(self.tick(state, html=html).status, Health.FAILED)
                self.assertFalse(state["emergency_last_check_ok"])
                self.assertEqual(self.tick(state, 5).status, Health.FAILED)
                self.assertEqual(self.tick(state, 10).status, Health.FAILED)
                self.assertEqual(state["latest_seen"], 3225)
        self.send.assert_not_called()

    def test_pending_failure_merges_new_chapter_keeps_backoff_and_retries(self):
        state = active()
        self.send.side_effect = NotificationDeliveryError("http_error", 503)
        self.assertEqual(self.tick(state, html=message()).status, Health.FAILED)
        self.assertEqual(state["latest_seen"], 3225)
        pending = copy.deepcopy(state["pending_notification"])
        self.assertEqual(pending["sources"], ["Telegram"])
        self.assertEqual(pending["url"], SOURCE.url)
        with patch.object(monitor, "pending_due", return_value=False):
            self.tick(state, 20, html=message(3227))
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(state["pending_notification"]["latest_pending_chapter"], 3227)
        for key in ("created_at", "first_failure_at", "next_retry_at", "attempt_count"):
            self.assertEqual(state["pending_notification"][key], pending[key])
        self.send.side_effect = None
        self.tick(state, 25)
        self.assertEqual(state["latest_seen"], 3227)
        self.assertIsNone(state["pending_notification"])
        self.assertEqual(self.send.call_count, 2)

    def test_recovery_behind_then_catchup_then_advance_preserves_history(self):
        state = active()
        self.tick(state, html=message(3227))
        provenance = copy.deepcopy(state["emergency_provenance"])
        self.assertEqual(self.tick(state, 20, official=3226).status, Health.HEALTHY)
        self.assertEqual((state["latest_webnovel"], state["latest_seen"], state["webnovel_failures"]), (3226, 3227, 0))
        self.tick(state, 40, official=3227)
        self.tick(state, 60, official=3228)
        self.assertEqual(state["emergency_provenance"], provenance)
        self.assertEqual((state["mode"], state["target_chapter"]), ("watch_free_sites", 3228))
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.send.call_count, 1)

    def test_regression_and_invalid_chapters_are_failures_not_recovery(self):
        for chapter in (3224, True, False, -1, MAX_CHAPTER + 1, "3226", 3226.0):
            with self.subTest(chapter=chapter):
                state = active()
                self.assertEqual(self.tick(state, official=chapter).status, Health.DEGRADED)
                self.assertTrue(monitor.emergency_active(state))
                self.assertEqual(state["latest_webnovel"], 3225)

    def test_recovery_while_pending_does_not_drop_delivery_or_provenance(self):
        state = active()
        self.send.side_effect = NotificationDeliveryError("http_error", 503)
        self.tick(state, html=message(3227))
        pending = copy.deepcopy(state["pending_notification"])
        with patch.object(monitor, "pending_due", return_value=False):
            self.assertEqual(self.tick(state, 20, official=3226).status, Health.FAILED)
        self.assertEqual(state["pending_notification"], pending)
        self.assertEqual(state["webnovel_failures"], 0)
        self.send.side_effect = None
        self.tick(state, 25)
        self.assertEqual((state["latest_webnovel"], state["latest_seen"]), (3226, 3227))
        self.assertIsNotNone(state["emergency_provenance"])
        self.assertEqual(self.fetch.call_count, 1)

    def test_first_setup_cannot_fall_back_or_emit_history(self):
        state = initial_state()
        for _ in range(5):
            monitor.first_setup(state, RunResult())
        self.assertEqual(state["webnovel_failures"], 0)
        self.assertIsNone(state["latest_seen"])
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_partly_completed_first_setup_resumes_without_notifying_history(self):
        state = initial_state()
        state.update(latest_webnovel=3225, latest_webnovel_title="Baseline")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(state, path)
            state, first = load_state(path)
        self.assertTrue(first)
        monitor.first_setup(state, RunResult())
        self.assertEqual(state["webnovel_failures"], 0)
        self.assertIsNone(state["latest_seen"])
        self.official.side_effect = None
        self.official.return_value = ChapterReport("WebNovel", 3225, "Baseline", "https://official.example/catalog")
        self.public.side_effect = None
        self.public.return_value = [ChapterReport("Telegram", 3225, "Baseline", SOURCE.url)]
        monitor.first_setup(state, RunResult())
        self.assertEqual(state["latest_seen"], 3225)
        self.send.assert_not_called()
        self.fetch.assert_not_called()

    def test_optional_free_cycle_recheck_does_not_accumulate_emergency_failures(self):
        state = initialized()
        state.update(mode="watch_free_sites", latest_webnovel=3226, target_chapter=3226)
        self.public.side_effect = None
        self.public.return_value = [ChapterReport("Telegram", 3227, "Synthetic", SOURCE.url)]
        for _ in range(5):
            monitor.run_watch_free_sites(state, RunResult())
        self.assertEqual(state["webnovel_failures"], 0)
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_failure_logs_are_sanitized_and_unrelated_errors_stay_failed(self):
        state = active()
        result = RunResult()
        result.fail("unrelated critical error")
        with patch.object(monitor, "utc_now", return_value=START), \
             patch.dict(monitor.os.environ, {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": "false"}), \
             self.assertLogs(level="WARNING") as logs:
            monitor.run_watch_webnovel(state, result)
        self.assertEqual(result.status, Health.FAILED)
        self.assertIn("unrelated critical error", result.reasons)
        self.assertNotIn("private", "\n".join(logs.output))


class EmergencyParserTests(unittest.TestCase):
    def test_observed_grouped_history_does_not_block_newest_release(self):
        soup = observed_messages()
        for doc, link in ((True, True), (True, False), (False, True)):
            variant = BeautifulSoup(str(soup), "html.parser")
            if not doc:
                for node in variant.select('.tgme_widget_message_document_wrap'):
                    node.decompose()
            if not link:
                for node in variant.select('.tgme_widget_message_text'):
                    node.decompose()
            with self.subTest(doc=doc, link=link):
                report = self.check(str(variant), baseline=3226)
                self.assertEqual((report.chapter, report.title),
                                 (3227, "A Soul and a Shadow Walk Into a Bar"))
        ordinary = parsers.parse_telegram_candidates(soup, SOURCE.url)
        self.assertEqual(max(r.chapter for r in ordinary), 3227)

    def test_single_media_query_is_exact_and_only_for_configured_posts(self):
        from shadow_slave_monitor.emergency import telegram_emergency_evidence_url
        base = "https://t.me/shadow_slave_fastes/2332"
        for url in (base, base + '?single', base.replace('/shadow_', '/s/shadow_') + '?single'):
            with self.subTest(url=url):
                self.assertTrue(telegram_emergency_evidence_url(url, 3218, SOURCE.url))
        invalid = [base + suffix for suffix in (
            '?', '?single=', '?single=1', '?Single', '?single&x=1', '?x=1&single',
            '?single&single', '?%73ingle', '?single#x', '?single#', '?single/',
            '?single\n', '/?single', '?single?x', '?single%00',
        )]
        invalid += [base.replace('https:', 'http:') + '?single',
                    base.replace('t.me/', 't.me.evil.example/') + '?single',
                    base.replace('t.me/', 'u@t.me/') + '?single',
                    base.replace('t.me/', 't.me:443/') + '?single',
                    base.replace('shadow_slave_fastes', 'another_channel') + '?single',
                    base.replace('2332', '%32%33%33%32') + '?single',
                    'https://telegra.ph/3218-Empty-Darkness-10-17?single']
        html = str(observed_messages())
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(telegram_emergency_evidence_url(url, 3218, SOURCE.url))
                with self.assertRaises(parsers.ParseError):
                    self.check(html.replace(base + '?single', url))

    def test_grouped_history_conflicts_still_reject_newer_valid_release(self):
        html = str(observed_messages())
        for changed in (html.replace('3218-Empty-Darkness', '3218-Contradictory-Title'),
                        html.replace('3218-Empty-Darkness', '3219-Empty-Darkness'),
                        html.replace('3218 Empty Darkness.docx', '9999 Empty Darkness.docx')):
            with self.subTest(html=changed), self.assertRaises(parsers.ParseError):
                self.check(changed, baseline=3226)

    def check(self, html, baseline=3225):
        with patch.object(parsers, "fetch_html", return_value=html) as fetch:
            report = parsers.check_telegram_emergency(SOURCE, baseline)
        fetch.assert_called_once_with(SOURCE)
        return report

    def test_numbered_documents_and_links_work_independently(self):
        for doc, link in ((True, True), (False, True), (True, False)):
            with self.subTest(doc=doc, link=link):
                self.assertEqual(self.check(message(doc=doc, link=link)).chapter, 3226)

    def test_two_numbered_chapters_in_one_message_documents_links_and_both(self):
        for doc, link in ((True, False), (False, True), (True, True)):
            with self.subTest(doc=doc, link=link):
                report = self.check(multi_message(3217, 3218, doc=doc, link=link), baseline=3216)
                self.assertEqual(report.chapter, 3218)
                self.assertEqual(report.title, "Synthetic Chapter 3218")

    def test_multiple_posts_history_duplicates_and_newer_single_chapter_coexist(self):
        history = "".join(message(chapter, f"Synthetic Chapter {chapter}")
                          for chapter in (3220, 3223, 3224))
        pair = multi_message(3226, 3227)
        self.assertEqual(self.check(history + pair).chapter, 3227)
        self.assertEqual(self.check(pair + history + pair + message(3228)).chapter, 3228)

    def test_conflicting_signals_in_multi_chapter_message_reject_entire_response(self):
        pair = multi_message(3226, 3227)
        cases = [pair.replace("3227-Synthetic-Chapter-3227", "3228-Synthetic-Chapter-3227"),
                 pair.replace("3227-Synthetic-Chapter-3227", "3227-Contradictory-Title"),
                 pair + message(3227, "Conflicting Title"),
                 pair.replace(">Read chapter<", ">Chapter 3229<", 1),
                 pair.replace("https://t.me/shadow_slave_fastes/3227", "https://evil.example/file"),
                 # Unlinked numbered document evidence cannot be silently lost
                 # behind a valid different chapter.
                 pair.replace('<a class="tgme_widget_message_document_wrap" href="https://t.me/shadow_slave_fastes/3227">', '<span>')
                     .replace('</span></a><a href="https://telegra.ph/3227-Synthetic-Chapter-3227-10-25">Read chapter</a>', '</span></span>')]
        for html in cases:
            with self.subTest(html=html), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_invalid_link_for_one_chapter_cannot_hide_behind_valid_chapter(self):
        for url in ("http://telegra.ph/3227-Synthetic-Chapter-3227", "https://evil.example/3227-Synthetic",
                    "https://telegra.ph:443/3227-Synthetic", "https://u@telegra.ph/3227-Synthetic",
                    "https://telegra.ph/3227-Synthetic?x=1", "https://telegra.ph/3227-Synthetic#x",
                    "https://telegra.ph/3227-Synthetic%20Chapter", "https://telegra.ph/10001-Synthetic",
                    "https://telegra.ph/%33%32%32%37-Synthetic"):
            html = multi_message(3226, 3227).replace("https://telegra.ph/3227-Synthetic-Chapter-3227-10-25", url)
            with self.subTest(url=url), self.assertRaises(parsers.ParseError):
                self.check(html)
        with self.assertRaises(parsers.ParseError):
            self.check(multi_message(3226, 3227).replace(
                'href="https://telegra.ph/3227-Synthetic-Chapter-3227-10-25">Read chapter',
                'href="https://telegra.ph/Unnumbered">Chapter 3227'))

    def test_multi_chapter_posts_ignore_noise_without_title_only_inference(self):
        pair = multi_message(3226, 3227)
        noise = '<p>9999 views 2026-10-25 rating 5 page 7777 Chapter 8888</p>'
        self.assertEqual(self.check(pair.replace('</div>', noise + '</div>')).chapter, 3227)
        title_only = message(3228).replace("3228 Synthetic Chapter.docx", "Synthetic Chapter.docx").replace(
            "3228-Synthetic-Chapter-10-25", "Synthetic-Chapter-10-25")
        self.assertEqual(self.check(pair + title_only).chapter, 3227)

    def test_title_only_document_must_match_unique_independent_number_in_multi_post(self):
        pair = multi_message(3226, 3227)
        self.assertEqual(self.check(pair.replace("3227 Synthetic Chapter 3227.docx", "Synthetic Chapter 3227.docx")).chapter, 3227)
        # Use a genuinely title-only filename with no embedded numeric token.
        pair = pair.replace("Synthetic Chapter 3227", "Second Synthetic").replace("Synthetic-Chapter-3227", "Second-Synthetic")
        self.assertEqual(self.check(pair.replace("3227 Second Synthetic.docx", "Second Synthetic.docx")).chapter, 3227)
        with self.assertRaises(parsers.ParseError):
            self.check(pair.replace("3227 Second Synthetic.docx", "Unmatched Title.docx"))

    def test_multiple_correctly_paired_chapters_may_share_title(self):
        pair = multi_message(3226, 3227)
        for chapter in (3226, 3227):
            pair = pair.replace(f"Synthetic Chapter {chapter}", "Shared Title").replace(f"Synthetic-Chapter-{chapter}", "Shared-Title")
        self.assertEqual(self.check(pair).chapter, 3227)

    def test_large_contiguous_advance_can_be_in_one_post_but_gaps_and_bounds_fail_closed(self):
        self.assertEqual(self.check(multi_message(*range(3226, 3256))).chapter, 3255)
        for chapters in ([3226, 9999], [*range(3226, 3240), *range(3241, 3256)], [3226, 10001]):
            with self.subTest(chapters=chapters), self.assertRaises(parsers.ParseError):
                self.check(multi_message(*chapters))
        self.assertEqual(self.check(multi_message(9999, 10000), baseline=9998).chapter, 10000)

    def test_multiple_sequential_chapters_and_corroborated_large_gap(self):
        html = "".join(message(chapter) for chapter in range(3226, 3256))
        self.assertEqual(self.check(html).chapter, 3255)
        with self.assertRaises(parsers.ParseError):
            self.check(html.replace(message(3230), ""))
        with self.assertRaises(parsers.ParseError):
            self.check(message(9999))

    def test_document_text_alone_and_title_only_context_cannot_authorize(self):
        cases = ("<p>Chapter 9999</p>",
                 '<div class="tgme_widget_message" data-post="shadow_slave_fastes/9999">3226 Synthetic Chapter.docx</div>',
                 message().replace("3226 Synthetic Chapter.docx", "Synthetic Chapter.docx")
                          .replace("3226-Synthetic-Chapter-10-25", "Synthetic-Chapter-10-25"),
                 '<div class="tgme_widget_message" data-post="shadow_slave_fastes/9999">'
                 '<span class="tgme_widget_message_document_title">3226 Synthetic Chapter.docx</span></div>',
                 message().replace("shadow_slave_fastes/3226", "another_channel/3226"))
        for html in cases:
            with self.subTest(html=html), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_unsafe_links_and_contradictory_signals_fail_closed(self):
        cases = [message().replace("3226-Synthetic", "3227-Synthetic"),
                 message().replace(">Read chapter<", ">Chapter 3227<"),
                 message().replace("3226-Synthetic-Chapter", "3226-Other-Title"),
                 message() + message(title="Conflicting Title"),
                 message(10001), message(0),
                 message().replace("3226 Synthetic Chapter.docx", "Contradictory Title.docx"),
                 message().replace(">Read chapter<", ">Chapter 3226 Contradictory Title<")]
        for url in ("http://telegra.ph/3226-Synthetic-Chapter", "https://evil.example/3226-Synthetic-Chapter",
                    "https://telegra.ph:443/3226-Synthetic-Chapter", "https://u@telegra.ph/3226-Synthetic-Chapter",
                    "https://telegra.ph/3226-Synthetic-Chapter?secret=1", "https://telegra.ph/3226-Synthetic%20Chapter",
                    "https://telegra.ph/3226-Synthetic-Chapter#x"):
            cases.append(message(doc=False).replace("https://telegra.ph/3226-Synthetic-Chapter-10-25", url))
        cases.append(message(link=False).replace("https://t.me/shadow_slave_fastes/3226", "https://evil.example/file"))
        for html in cases:
            with self.subTest(html=html), self.assertRaises(parsers.ParseError):
                self.check(html)

    def test_unrelated_numbers_do_not_change_trusted_report(self):
        self.assertEqual(self.check(message() + '<p>9999 views 2026-10-25 rating 5 page 7777 Chapter 8888</p>').chapter, 3226)


class EmergencyStateTests(unittest.TestCase):
    def test_legacy_state_migrates_without_rewriting_chapters(self):
        # A pre-emergency payload deliberately omits all five emergency fields.
        raw = {
            "latest_seen": 3225,
            "latest_webnovel": 3225,
            "latest_title": "Baseline",
            "latest_webnovel_title": "Baseline",
            "latest_url": "https://reader.example/chapter/3225",
            "mode": "watch_webnovel",
        }
        before = copy.deepcopy(raw)
        clean = validate_state(raw)
        self.assertEqual(raw, before)
        for key, value in before.items():
            self.assertEqual(clean[key], value)
        self.assertEqual(clean["webnovel_failures"], 0)
        self.assertIsNone(clean["webnovel_failure_cycle"])
        self.assertIsNone(clean["emergency_last_check_ok"])
        self.assertIsNone(clean["emergency_provenance"])
        self.assertIsNone(clean["webnovel_success_cycle"])
        with TemporaryDirectory() as tmp, patch("shadow_slave_monitor.state_manager.iso_now", return_value=START.isoformat()):
            path = Path(tmp) / "state.json"
            save_state(clean, path)
            reloaded, first = load_state(path)
        self.assertFalse(first)
        self.assertEqual(reloaded, dict(clean, updated_at=START.isoformat()))

    def test_invalid_fields_and_combinations_rejected(self):
        cases = [("webnovel_failures", v) for v in (True, False, -1, 5, "4", 4.0, None)]
        cases += [("webnovel_failure_cycle", v) for v in (True, -1, "1", 1.0, MAX_WEBNOVEL_CYCLE + 1)]
        cases += [("webnovel_success_cycle", v) for v in (True, False, -1, "1", 1.0, MAX_WEBNOVEL_CYCLE + 1)]
        cases += [("emergency_last_check_ok", v) for v in (True, False, 1, "true")]
        cases += [("emergency_provenance", v) for v in ({}, [], True,
            {"source": "Other", "chapter": 3226, "evidence_url": "https://telegra.ph/3226-Synthetic"},
            {"source": "Telegram", "chapter": True, "evidence_url": "https://telegra.ph/3226-Synthetic"},
            {"source": "Telegram", "chapter": 3226, "evidence_url": "https://evil.example/3226-Synthetic"})]
        for key, value in cases:
            state = initialized()
            state[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(StateError):
                validate_state(state)
        for updates in (dict(webnovel_failures=1), dict(webnovel_failures=4, webnovel_failure_cycle=1),
                        dict(latest_seen=3226), dict(latest_seen=3226, latest_webnovel=None),
                        dict(webnovel_failures=1, webnovel_failure_cycle=1, mode="watch_free_sites", target_chapter=3225)):
            state = initialized()
            state.update(updates)
            with self.subTest(updates=updates), self.assertRaises(StateError):
                validate_state(state)

    def test_success_marker_cannot_coexist_with_failure_history_it_supersedes(self):
        state = active()
        for cycle in (state["webnovel_failure_cycle"], state["webnovel_failure_cycle"] + 1):
            with self.subTest(cycle=cycle), self.assertRaises(StateError):
                validate_state(dict(state, webnovel_success_cycle=cycle))
        state["webnovel_success_cycle"] = state["webnovel_failure_cycle"] - 1
        self.assertEqual(validate_state(state), state)
        with self.assertRaises(StateError):
            validate_state(dict(initial_state(), webnovel_success_cycle=1))

    def test_unchanged_save_preserves_bytes_and_does_not_write(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(initialized(), path)
            state, _ = load_state(path)
            before = path.read_bytes()
            with patch("shadow_slave_monitor.state_manager.atomic_write_json") as write:
                save_state(state, path)
            write.assert_not_called()
            self.assertEqual(path.read_bytes(), before)

    def test_only_valid_provenance_can_support_delivered_and_pending_authority_exception(self):
        state = initialized()
        state.update(latest_seen=3226, emergency_provenance={"source": "Telegram", "chapter": 3226,
                                                          "evidence_url": "https://telegra.ph/3226-Synthetic"})
        self.assertEqual(validate_state(state), state)
        for changes in (dict(source="Other"), dict(chapter="3226"), dict(chapter=3227),
                        dict(evidence_url="https://telegra.ph/3227-Synthetic"), dict(extra=True)):
            bad = copy.deepcopy(state)
            bad["emergency_provenance"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(StateError):
                validate_state(bad)
        for source, url in (("Other", SOURCE.url), ("Telegram", "https://evil.example")):
            bad = copy.deepcopy(state)
            bad["latest_seen"] = 3225
            bad["pending_notification"] = pending_from_report(3225, ChapterReport(source, 3226, "Synthetic", url))
            with self.subTest(source=source, url=url), self.assertRaises(StateError):
                validate_state(bad)

    def test_conflicting_pending_delivery_metadata_cannot_be_dropped(self):
        state = initialized()
        state["latest_webnovel"] = 3226
        state["pending_notification"] = pending_from_report(3225, ChapterReport("Telegram", 3226, "Synthetic", SOURCE.url))
        newer = copy.deepcopy(state)
        newer["pending_notification"]["attempt_count"] += 1
        self.assertFalse(state_writer.monitor_artifact_superseded(state, newer))
        self.assertFalse(state_writer.monitor_artifact_superseded(newer, state))

    def test_stale_authority_transitions_conflict_in_both_directions(self):
        recovered = active()
        recovered.update(webnovel_failures=0, emergency_last_check_ok=None)
        for current, artifact in ((recovered, active()), (active(), recovered),
                                  (initialized(), active()), (active(), initialized())):
            with self.subTest(current=current["webnovel_failures"], artifact=artifact["webnovel_failures"]):
                self.assertFalse(state_writer.monitor_artifact_superseded(current, artifact))
                self.assertIsNone(state_writer.reconcile_monitor_cursor_advancement(current, artifact))
                with TemporaryDirectory() as tmp:
                    current_path, artifact_path = Path(tmp) / "current", Path(tmp) / "artifact"
                    current_path.write_text(json.dumps(current))
                    artifact_path.write_text(json.dumps(artifact))
                    self.assertEqual(state_writer.classify_artifact("state/state.json", current_path, current,
                        artifact_path, artifact, {"base_sha256": "0" * 64}), "conflicting")
                    base = hashlib.sha256(current_path.read_bytes()).hexdigest()
                    self.assertEqual(state_writer.classify_artifact("state/state.json", current_path, current,
                        artifact_path, artifact, {"base_sha256": base}), "applicable")

    def test_new_provenance_and_new_cycle_cannot_be_semantically_discarded(self):
        older = active()
        for updates in (dict(webnovel_failure_cycle=older["webnovel_failure_cycle"] + 1),
                        dict(emergency_last_check_ok=False),
                        dict(emergency_provenance={"source": "Telegram", "chapter": 3225,
                                                  "evidence_url": "https://telegra.ph/3225-Synthetic"})):
            newer = copy.deepcopy(older)
            newer.update(updates)
            self.assertFalse(state_writer.monitor_artifact_superseded(newer, older))
            self.assertFalse(state_writer.monitor_artifact_superseded(older, newer))

    def test_stale_success_marker_cannot_erase_recovery_or_reset_newer_failure_history(self):
        failed = active()
        recovered = dict(failed, webnovel_failures=0, emergency_last_check_ok=None,
                         webnovel_success_cycle=failed["webnovel_failure_cycle"])
        later_failure = dict(recovered, webnovel_failures=1,
                             webnovel_failure_cycle=failed["webnovel_failure_cycle"] + 1)
        healthy = initialized()
        healthy["webnovel_success_cycle"] = 100
        older_healthy = dict(healthy, webnovel_success_cycle=99)
        for current, artifact in ((failed, recovered), (recovered, failed),
                                  (later_failure, recovered), (recovered, later_failure),
                                  (healthy, older_healthy), (older_healthy, healthy)):
            self.assertEqual(validate_state(current), current)
            self.assertEqual(validate_state(artifact), artifact)
            self.assertFalse(state_writer.monitor_artifact_superseded(current, artifact))
            self.assertIsNone(state_writer.reconcile_monitor_cursor_advancement(current, artifact))

    def test_watchdog_labels_degraded_and_failed_emergency_runs_distinctly(self):
        now = START.isoformat()
        degraded = {"id": 1, "status": "completed", "conclusion": "success", "monitor_result": "degraded", "updated_at": now}
        failed = dict(degraded, id=2, monitor_result="failed")
        self.assertEqual(watchdog.completion_label(degraded), "degraded")
        self.assertEqual(watchdog.completion_label(failed), "failed")
        self.assertEqual(watchdog.latest_success([degraded, failed]), degraded)
        self.assertIsNone(watchdog.latest_success([failed]))
        with patch.object(watchdog, "utc_now", return_value=START), patch.object(watchdog, "send_watchdog") as send:
            self.assertEqual(watchdog.evaluate([degraded], initial_watchdog_state())[2], "fresh")
            self.assertEqual(watchdog.evaluate([failed], initial_watchdog_state())[2], "alert_sent")
        self.assertEqual(send.call_count, 1)
