from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from shadow_slave_monitor import monitor
from shadow_slave_monitor.config import SourceConfig
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.notifications import NotificationDeliveryError
from shadow_slave_monitor.state_manager import StateError, save_state, validate_state


def base_state() -> dict:
    return {
        "latest_seen": 10,
        "latest_title": "Chapter Ten",
        "latest_url": "https://public.example/10",
        "latest_webnovel": 10,
        "latest_webnovel_title": "Chapter Ten",
        "mode": "watch_webnovel",
        "target_chapter": None,
        "target_title": None,
        "target_url": None,
        "pending_notification": None,
        "updated_at": "2026-06-11T00:00:00+00:00",
    }


def run_main_with_state(state: dict, *, first: bool = False, allow_system_exit: bool = False):
    saved: list[dict] = []

    def fake_save(new_state: dict) -> None:
        saved.append(copy.deepcopy(new_state))

    patches = [
        patch.object(monitor, "load_state", return_value=(state, first)),
        patch.object(monitor, "save_state", side_effect=fake_save),
        patch.object(monitor, "write_result"),
    ]
    for p in patches:
        p.start()
    try:
        try:
            monitor.main()
        except SystemExit:
            if not allow_system_exit:
                raise
    finally:
        for p in reversed(patches):
            p.stop()
    return saved


class PublicSourceFailureLoggingTests(unittest.TestCase):
    def test_http_failure_logs_sanitized_diagnostics_and_degrades(self) -> None:
        failed = SourceConfig("Failed", "https://example.com/private?token=secret", True, ("example.com",))
        good = SourceConfig("Good", "https://good.example", True, ("good.example",))
        response = requests.Response()
        response.status_code = 403
        response.url = failed.url
        request = requests.Request("GET", failed.url, headers={"Authorization": "secret"}).prepare()
        error = requests.HTTPError("response body cookie=secret", response=response, request=request)
        error.host = "example.com"
        error.attempts = 1
        report = ChapterReport("Good", 10, None, "https://good.example/chapter-10")

        def check(site):
            if site.name == "Failed":
                raise error
            return report

        result = monitor.RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (failed, good)), patch.object(monitor, "check_public_site", side_effect=check), self.assertLogs(level="WARNING") as logs:
            reports = monitor.check_public_sites(result)
        output = "\n".join(logs.output)
        self.assertEqual(reports, [report])
        self.assertIn("category=http_error type=HTTPError status=403 host=example.com attempts=1", output)
        for unsafe in ("/private", "token", "response body", "cookie", "Authorization"):
            self.assertNotIn(unsafe, output)
        self.assertTrue(result.degraded_reasons)

    def test_every_source_failure_still_fails_run(self) -> None:
        source = SourceConfig("Failed", "https://example.com", True, ("example.com",))
        result = monitor.RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (source,)), patch.object(monitor, "check_public_site", side_effect=RuntimeError("unsafe")):
            self.assertEqual(monitor.check_public_sites(result), [])
        self.assertTrue(result.reasons)


class WebNovelCadenceTests(unittest.TestCase):
    def test_every_five_minute_cadence_hits_each_twenty_minute_cycle(self) -> None:
        for seconds in (0, 30):
            for start_minute in range(20):
                hits_by_cycle = []
                for cycle in range(3):
                    cycle_hits = []
                    for offset in range(0, 20, 5):
                        minute = cycle * 20 + start_minute + offset
                        now = datetime(2026, 6, 11, 12 + (minute // 60), minute % 60, seconds, tzinfo=timezone.utc)
                        with patch.object(monitor, "utc_now", return_value=now):
                            cycle_hits.append(monitor.webnovel_check_window_open())
                    hits_by_cycle.append(any(cycle_hits))
                self.assertTrue(
                    all(hits_by_cycle),
                    f"5-minute cadence starting at :{start_minute:02d}:{seconds:02d} missed a 20-minute WebNovel window",
                )

    def test_edge_cadences_hit_window_after_boundary_delay(self) -> None:
        for minute in (4, 9, 14, 19):
            hits = []
            for offset in range(0, 20, 5):
                current_minute = minute + offset
                now = datetime(2026, 6, 11, 12 + (current_minute // 60), current_minute % 60, 30, tzinfo=timezone.utc)
                with patch.object(monitor, "utc_now", return_value=now):
                    hits.append(monitor.webnovel_check_window_open())
            self.assertTrue(any(hits), f"edge cadence :{minute:02d}:30 missed the WebNovel window")

    def test_watch_webnovel_outside_window_is_noop(self) -> None:
        state = base_state()
        original = copy.deepcopy(state)
        outside_window = datetime(2026, 6, 11, 12, 12, tzinfo=timezone.utc)

        with patch.dict("os.environ", {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": "false"}, clear=False), \
             patch.object(monitor, "utc_now", return_value=outside_window), \
             patch.object(monitor, "check_webnovel") as check_webnovel:
            saved = run_main_with_state(state)

        check_webnovel.assert_not_called()
        self.assertEqual(state, original)
        self.assertEqual(saved, [])

    def test_watch_webnovel_inside_window_same_chapter_does_not_save(self) -> None:
        state = base_state()
        inside_window = datetime(2026, 6, 11, 12, 2, tzinfo=timezone.utc)
        report = ChapterReport("WebNovel", 10, "Chapter Ten", "https://webnovel.example/10", "catalog")

        with patch.dict("os.environ", {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": "false"}, clear=False), \
             patch.object(monitor, "utc_now", return_value=inside_window), \
             patch.object(monitor, "check_webnovel", return_value=report) as check_webnovel:
            saved = run_main_with_state(state, allow_system_exit=True)

        check_webnovel.assert_called_once()
        self.assertEqual(saved, [])

    def test_watch_webnovel_new_chapter_updates_target_and_saves(self) -> None:
        state = base_state()
        inside_window = datetime(2026, 6, 11, 12, 20, tzinfo=timezone.utc)
        report = ChapterReport("WebNovel", 11, "Chapter Eleven", "https://webnovel.example/11", "catalog")

        with patch.dict("os.environ", {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": "false"}, clear=False), \
             patch.object(monitor, "utc_now", return_value=inside_window), \
             patch.object(monitor, "check_webnovel", return_value=report):
            saved = run_main_with_state(state)

        self.assertEqual(len(saved), 1)
        self.assertEqual(state["latest_webnovel"], 11)
        self.assertEqual(state["latest_webnovel_title"], "Chapter Eleven")
        self.assertEqual(state["mode"], "watch_free_sites")
        self.assertEqual(state["target_chapter"], 11)
        self.assertEqual(state["target_title"], "Chapter Eleven")
        self.assertEqual(state["target_url"], "https://webnovel.example/11")

    def test_force_env_allows_manual_webnovel_check_outside_window(self) -> None:
        state = base_state()
        outside_window = datetime(2026, 6, 11, 12, 12, tzinfo=timezone.utc)
        report = ChapterReport("WebNovel", 10, "Chapter Ten", "https://webnovel.example/10", "catalog")

        with patch.dict("os.environ", {"SHADOW_SLAVE_FORCE_WEBNOVEL_CHECK": "true"}, clear=False), \
             patch.object(monitor, "utc_now", return_value=outside_window), \
             patch.object(monitor, "check_webnovel", return_value=report) as check_webnovel:
            saved = run_main_with_state(state)

        check_webnovel.assert_called_once()
        self.assertEqual(saved, [])


class WatchFreeSitesTests(unittest.TestCase):
    def watch_free_state(self) -> dict:
        state = base_state()
        state.update({
            "latest_webnovel": 11,
            "latest_webnovel_title": "Chapter Eleven",
            "mode": "watch_free_sites",
            "target_chapter": 11,
            "target_title": "Chapter Eleven",
            "target_url": "https://webnovel.example/11",
        })
        return state

    def test_watch_free_sites_checks_every_run_and_does_not_save_before_target(self) -> None:
        state = self.watch_free_state()
        reports = [ChapterReport("Chikari", 10, "Chapter Ten", "https://public.example/10", "page")]

        with patch.object(monitor, "check_public_sites", return_value=reports) as check_public_sites:
            saved = run_main_with_state(state)

        check_public_sites.assert_called_once()
        self.assertEqual(saved, [])
        self.assertEqual(state["mode"], "watch_free_sites")
        self.assertEqual(state["target_chapter"], 11)

    def test_watch_free_sites_target_found_sends_notification_updates_state_and_saves(self) -> None:
        state = self.watch_free_state()
        reports = [ChapterReport("Chikari", 11, "Chapter Eleven", "https://public.example/11", "page")]

        with patch.object(monitor, "check_public_sites", return_value=reports) as check_public_sites, \
             patch.object(monitor, "send_new_chapter") as send_new_chapter:
            saved = run_main_with_state(state)

        check_public_sites.assert_called_once()
        send_new_chapter.assert_called_once()
        self.assertEqual(len(saved), 1)
        self.assertEqual(state["latest_seen"], 11)
        self.assertEqual(state["latest_title"], "Chapter Eleven")
        self.assertEqual(state["latest_url"], "https://public.example/11")
        self.assertIsNone(state["pending_notification"])
        self.assertEqual(state["mode"], "watch_webnovel")

    def test_public_report_above_target_rechecks_webnovel_and_rejects_unconfirmed_jump(self) -> None:
        state = self.watch_free_state()
        reports = [ChapterReport("Chikari", 12, "Chapter Twelve", "https://public.example/12", "page")]
        official = ChapterReport("WebNovel", 11, "Chapter Eleven", "https://webnovel.example/11", "catalog")

        with patch.object(monitor, "check_public_sites", return_value=reports), \
             patch.object(monitor, "check_webnovel", return_value=official) as check_webnovel:
            saved = run_main_with_state(state, allow_system_exit=True)

        check_webnovel.assert_called_once()
        self.assertEqual(saved, [])
        self.assertEqual(state["target_chapter"], 11)
        self.assertEqual(state["latest_webnovel"], 11)
        self.assertEqual(state["mode"], "watch_free_sites")


class PendingNotificationTests(unittest.TestCase):
    def test_due_pending_notification_failure_updates_retry_state_and_saves(self) -> None:
        state = base_state()
        state["pending_notification"] = {
            "previous_seen": 10,
            "first_pending_chapter": 11,
            "latest_pending_chapter": 11,
            "title": "Chapter Eleven",
            "sources": ["Chikari"],
            "url": "https://public.example/11",
            "created_at": "2026-06-11T11:00:00+00:00",
            "first_failure_at": "2026-06-11T11:00:00+00:00",
            "last_attempt_at": "2026-06-11T11:00:00+00:00",
            "next_retry_at": "2026-06-11T11:10:00+00:00",
            "attempt_count": 1,
            "last_error_category": "http_error",
            "last_http_status": 503,
        }
        now = datetime(2026, 6, 11, 12, 12, tzinfo=timezone.utc)

        with patch.object(monitor, "pending_due", return_value=True), \
             patch.object(monitor, "webnovel_due", return_value=False), \
             patch.object(monitor, "send_new_chapter", side_effect=NotificationDeliveryError("http_error", 503)), \
             patch("shadow_slave_monitor.notifications.utc_now", return_value=now):
            saved = run_main_with_state(state, allow_system_exit=True)

        self.assertEqual(len(saved), 1)
        self.assertEqual(state["pending_notification"]["attempt_count"], 2)
        self.assertEqual(state["pending_notification"]["last_http_status"], 503)


class StateMigrationTests(unittest.TestCase):
    def test_legacy_timing_fields_are_tolerated_and_omitted_on_save(self) -> None:
        state = base_state()
        state["last_webnovel_check"] = "2026-06-11T12:00:00+00:00"
        state["webnovel_skip_count"] = 2

        clean = validate_state(state)
        self.assertNotIn("last_webnovel_check", clean)
        self.assertNotIn("webnovel_skip_count", clean)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(state, path)
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertNotIn("last_webnovel_check", persisted)
        self.assertNotIn("webnovel_skip_count", persisted)

    def test_current_main_state_with_legacy_timing_fields_is_migrated_safely(self) -> None:
        # Represents latest main after a timing-only state update: semantic fields survive, legacy timing does not persist.
        state = {
            "latest_seen": 3036,
            "latest_title": "A Feast in Time of Plague",
            "latest_url": "https://telegra.ph/3036-A-Feast-in-Time-of-Plague-06-11",
            "latest_webnovel": 3036,
            "latest_webnovel_title": "A Feast in Time of Plague",
            "mode": "watch_webnovel",
            "target_chapter": None,
            "target_title": None,
            "target_url": None,
            "last_webnovel_check": "2026-06-11T23:50:17+00:00",
            "webnovel_skip_count": 1,
            "pending_notification": None,
            "updated_at": "2026-06-11T23:55:14+00:00",
        }

        clean = validate_state(state)

        self.assertEqual(clean["latest_seen"], 3036)
        self.assertEqual(clean["latest_title"], "A Feast in Time of Plague")
        self.assertEqual(clean["latest_url"], "https://telegra.ph/3036-A-Feast-in-Time-of-Plague-06-11")
        self.assertEqual(clean["latest_webnovel"], 3036)
        self.assertEqual(clean["latest_webnovel_title"], "A Feast in Time of Plague")
        self.assertEqual(clean["mode"], "watch_webnovel")
        self.assertNotIn("last_webnovel_check", clean)
        self.assertNotIn("webnovel_skip_count", clean)

    def test_unknown_invalid_state_fields_are_still_rejected(self) -> None:
        state = base_state()
        state["unexpected_field"] = "bad"

        with self.assertRaises(StateError):
            validate_state(state)


if __name__ == "__main__":
    unittest.main()
