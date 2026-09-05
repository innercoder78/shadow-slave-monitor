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
from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
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
        "public_source_failures": {},
        "source_positions": {},
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
    def test_new_sources_participate_in_order_and_degrade_independently(self) -> None:
        readwn = next(site for site in PUBLIC_SITES if site.name == "Readwn")
        lightnovelup = next(site for site in PUBLIC_SITES if site.name == "LightNovelUp")
        report = ChapterReport("LightNovelUp", 3174, "Tatal's Basilisk",
                               "https://lightnovelup.com/novel/shadow-slave/chapter-3174-tatals-basilisk")

        def check(site: SourceConfig, source_position=None) -> ChapterReport:
            if site.name == "Readwn":
                raise RuntimeError("template changed")
            return report

        result = monitor.RunResult()
        failures: dict[str, int] = {}
        with patch.object(monitor, "PUBLIC_SITES", (readwn, lightnovelup)), \
             patch.object(monitor, "check_public_site", side_effect=check):
            reports = monitor.check_public_sites(result, failures)

        self.assertEqual(reports, [report])
        self.assertEqual(failures, {"Readwn": 1})
        self.assertEqual(result.degraded_reasons, ["optional public sources failed: Readwn"])

    def test_lightnovelup_position_is_updated_only_in_controlling_thread(self) -> None:
        source = next(site for site in PUBLIC_SITES if site.name == "LightNovelUp")
        old = {"chapter": 3173, "url": "https://lightnovelup.com/novel/shadow-slave/chapter-3173-life-goes-on/"}
        positions = {"LightNovelUp": dict(old)}
        report = ChapterReport(
            "LightNovelUp", 3174, "Tatal’s Basilisk",
            "https://lightnovelup.com/novel/shadow-slave/chapter-3174-tatals-basilisk/",
            "LightNovelUp:canonical_navigation", 3174,
            "https://lightnovelup.com/novel/shadow-slave/chapter-3174-tatals-basilisk/",
        )

        def check(site: SourceConfig, supplied: dict) -> ChapterReport:
            self.assertIsNot(supplied, positions["LightNovelUp"])
            self.assertEqual(supplied, old)
            return report

        with patch.object(monitor, "PUBLIC_SITES", (source,)), \
             patch.object(monitor, "check_public_site", side_effect=check):
            self.assertEqual(monitor.check_public_sites(monitor.RunResult(), {}, positions), [report])
        self.assertEqual(positions["LightNovelUp"], {"chapter": 3174, "url": report.position_url})

        with patch.object(monitor, "PUBLIC_SITES", (source,)), \
             patch.object(monitor, "check_public_site", return_value=report):
            monitor.check_public_sites(monitor.RunResult(), {}, positions)
        self.assertEqual(positions["LightNovelUp"], {"chapter": 3174, "url": report.position_url})

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
            failures = {}
            reports = monitor.check_public_sites(result, failures)
        output = "\n".join(logs.output)
        self.assertEqual(reports, [report])
        self.assertIn("category=http_error type=HTTPError status=403 host=example.com attempts=1", output)
        for unsafe in ("/private", "token", "response body", "cookie", "Authorization"):
            self.assertNotIn(unsafe, output)
        self.assertTrue(result.degraded_reasons)
        self.assertEqual(failures, {"Failed": 1})
        self.assertIn("Failed consecutive public-source failures for current watch cycle: 1/4", output)

    def test_source_is_suppressed_after_four_monitor_runs_and_skip_is_not_degraded(self) -> None:
        failed = SourceConfig("Failed", "https://failed.example", True, ("failed.example",))
        good = SourceConfig("Good", "https://good.example", True, ("good.example",))
        report = ChapterReport("Good", 10, None, "https://good.example/chapter-10")
        calls = {"Failed": 0, "Good": 0}

        def check(site):
            calls[site.name] += 1
            if site.name == "Failed":
                raise RuntimeError("unsafe")
            return report

        failures: dict[str, int] = {}
        with patch.object(monitor, "PUBLIC_SITES", (failed, good)), patch.object(monitor, "check_public_site", side_effect=check):
            for expected in range(1, 5):
                result = monitor.RunResult()
                self.assertEqual(monitor.check_public_sites(result, failures), [report])
                self.assertEqual(failures, {"Failed": expected})
                self.assertTrue(result.degraded_reasons)
            result = monitor.RunResult()
            with self.assertLogs(level="INFO") as logs:
                self.assertEqual(monitor.check_public_sites(result, failures), [report])

        self.assertEqual(calls, {"Failed": 4, "Good": 5})
        self.assertFalse(result.degraded_reasons)
        self.assertIn("reached the consecutive-failure limit", "\n".join(logs.output))

    def test_success_resets_failures_and_next_failure_starts_at_one(self) -> None:
        source = SourceConfig("Source", "https://source.example", True, ("source.example",))
        report = ChapterReport("Source", 10, None, "https://source.example/chapter-10")
        failures = {"Source": 3}
        result = monitor.RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (source,)), patch.object(monitor, "check_public_site", return_value=report):
            self.assertEqual(monitor.check_public_sites(result, failures), [report])
        self.assertEqual(failures, {})
        with patch.object(monitor, "PUBLIC_SITES", (source,)), patch.object(monitor, "check_public_site", side_effect=RuntimeError("unsafe")):
            monitor.check_public_sites(monitor.RunResult(), failures)
        self.assertEqual(failures, {"Source": 1})

    def test_all_suppressed_sources_fail_closed_without_requests(self) -> None:
        source = SourceConfig("Source", "https://source.example", True, ("source.example",))
        failures = {"Source": 4}
        result = monitor.RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (source,)), patch.object(monitor, "check_public_site") as check:
            self.assertEqual(monitor.check_public_sites(result, failures), [])
        check.assert_not_called()
        self.assertEqual(result.reasons, ["every enabled public source is suppressed for the current watch cycle"])
        self.assertEqual(failures, {"Source": 4})

    def test_every_source_failure_still_fails_run(self) -> None:
        source = SourceConfig("Failed", "https://example.com", True, ("example.com",))
        result = monitor.RunResult()
        with patch.object(monitor, "PUBLIC_SITES", (source,)), patch.object(monitor, "check_public_site", side_effect=RuntimeError("unsafe")):
            self.assertEqual(monitor.check_public_sites(result), [])
        self.assertTrue(result.reasons)


class UnexpectedFailureLoggingTests(unittest.TestCase):
    def test_top_level_unexpected_failure_does_not_log_exception_contents(self) -> None:
        secret = "https://secret.example/private?token=super-secret cookie=secret-value Authorization: Bearer secret-token response-body-secret"
        captured = []
        with patch.object(monitor, "load_state", side_effect=RuntimeError(secret)), \
             patch.object(monitor, "write_result", side_effect=captured.append), \
             self.assertLogs(level="ERROR") as logs, self.assertRaisesRegex(SystemExit, "2"):
            monitor.main()

        self.assertEqual(captured[0].reasons, ["unexpected monitor failure: RuntimeError"])
        output = "\n".join(logs.output)
        self.assertIn("Unexpected monitor failure safely contained: type=RuntimeError", output)
        self.assertNotIn("Traceback", output)
        for unsafe in ("secret.example", "super-secret", "secret-value", "secret-token", "response-body-secret"):
            self.assertNotIn(unsafe, output)


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

    def test_completing_cycle_clears_public_source_failures(self) -> None:
        state = self.watch_free_state()
        state["public_source_failures"] = {"NovelFire": 4}
        reports = [ChapterReport("Chikari", 11, "Chapter Eleven", "https://public.example/11", "page")]
        with patch.object(monitor, "check_public_sites", return_value=reports), patch.object(monitor, "send_new_chapter"):
            run_main_with_state(state)
        self.assertEqual(state["mode"], "watch_webnovel")
        self.assertEqual(state["public_source_failures"], {})

    def test_confirming_newer_target_inside_cycle_preserves_suppression(self) -> None:
        state = self.watch_free_state()
        state["public_source_failures"] = {"NovelFire": 4}
        reports = [ChapterReport("Chikari", 13, "Chapter Thirteen", "https://public.example/13", "page")]
        official = ChapterReport("WebNovel", 12, "Chapter Twelve", "https://webnovel.example/12", "catalog")
        with patch.object(monitor, "check_public_sites", return_value=reports), \
             patch.object(monitor, "check_webnovel", return_value=official), \
             patch.object(monitor, "send_new_chapter"):
            run_main_with_state(state, allow_system_exit=True)
        self.assertEqual(state["target_chapter"], 12)
        self.assertEqual(state["public_source_failures"], {"NovelFire": 4})

    def test_watch_free_sites_checks_every_run_and_does_not_save_before_target(self) -> None:
        state = self.watch_free_state()
        reports = [ChapterReport("Chikari", 10, "Chapter Ten", "https://public.example/10", "page")]

        with patch.object(monitor, "check_public_sites", return_value=reports) as check_public_sites:
            saved = run_main_with_state(state)

        check_public_sites.assert_called_once()
        self.assertEqual(saved, [])
        self.assertEqual(state["mode"], "watch_free_sites")
        self.assertEqual(state["target_chapter"], 11)

    def test_failure_counter_transition_is_persisted(self) -> None:
        state = self.watch_free_state()
        failed = SourceConfig("NovelFire", "https://novelfire.net/book/shadow-slave", True, ("novelfire.net",))
        good = SourceConfig("Chikari", "https://chikari.moe/novels/shadow-slave", True, ("chikari.moe",))
        report = ChapterReport("Chikari", 10, "Chapter Ten", "https://public.example/10", "page")

        def check(site):
            if site.name == "NovelFire":
                raise RuntimeError("unsafe")
            return report

        with patch.object(monitor, "PUBLIC_SITES", (failed, good)), patch.object(monitor, "check_public_site", side_effect=check):
            saved = run_main_with_state(state)
        self.assertEqual(state["public_source_failures"], {"NovelFire": 1})
        self.assertEqual(len(saved), 1)

    def test_unchanged_suppressed_source_does_not_cause_state_save(self) -> None:
        state = self.watch_free_state()
        state["public_source_failures"] = {"NovelFire": 4}
        suppressed = SourceConfig("NovelFire", "https://novelfire.net/book/shadow-slave", True, ("novelfire.net",))
        good = SourceConfig("Chikari", "https://chikari.moe/novels/shadow-slave", True, ("chikari.moe",))
        report = ChapterReport("Chikari", 10, "Chapter Ten", "https://public.example/10", "page")
        with patch.object(monitor, "PUBLIC_SITES", (suppressed, good)), \
             patch.object(monitor, "check_public_site", return_value=report) as check:
            saved = run_main_with_state(state)
        self.assertEqual([call.args[0].name for call in check.call_args_list], ["Chikari"])
        self.assertEqual(saved, [])

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
    def test_missing_source_positions_migrates_to_empty_mapping(self) -> None:
        state = base_state()
        del state["source_positions"]
        self.assertEqual(validate_state(state)["source_positions"], {})

    def test_source_positions_require_recognized_source_and_canonical_matching_url(self) -> None:
        invalid_positions = (
            [],
            {"Unknown": {"chapter": 3174, "url": "https://lightnovelup.com/novel/shadow-slave/chapter-3174-title/"}},
            {"LightNovelUp": {"chapter": 3174}},
            {"LightNovelUp": {"chapter": 3174, "url": "https://evil.example/novel/shadow-slave/chapter-3174-title/"}},
            {"LightNovelUp": {"chapter": 3174, "url": "https://lightnovelup.com/novel/shadow-slave/chapter-3173-title/"}},
            {"LightNovelUp": {"chapter": 3174, "url": "https://lightnovelup.com:443/novel/shadow-slave/chapter-3174-title/"}},
        )
        for positions in invalid_positions:
            state = base_state()
            state["source_positions"] = positions
            with self.subTest(positions=positions), self.assertRaises(StateError):
                validate_state(state)

    def test_missing_public_source_failures_migrates_to_empty_mapping(self) -> None:
        state = base_state()
        del state["public_source_failures"]
        self.assertEqual(validate_state(state)["public_source_failures"], {})

    def test_invalid_public_source_failure_shapes_and_counts_are_rejected(self) -> None:
        invalid_values = ([], {"NovelFire": True}, {"NovelFire": -1}, {"NovelFire": 0},
                          {"NovelFire": 5}, {"NovelFire": "1"}, {"Unknown": 1})
        for value in invalid_values:
            with self.subTest(value=value):
                state = base_state()
                state["public_source_failures"] = value
                with self.assertRaises(StateError):
                    validate_state(state)

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
