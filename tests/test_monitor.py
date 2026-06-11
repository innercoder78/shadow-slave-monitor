from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from shadow_slave_monitor import monitor
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


class WebNovelCadenceTests(unittest.TestCase):
    def test_watch_webnovel_outside_window_is_noop(self) -> None:
        state = base_state()
        original = copy.deepcopy(state)
        outside_window = datetime(2026, 6, 11, 12, 5, tzinfo=timezone.utc)

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
        outside_window = datetime(2026, 6, 11, 12, 5, tzinfo=timezone.utc)
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
        reports = [ChapterReport("Light Novel World", 10, "Chapter Ten", "https://public.example/10", "page")]

        with patch.object(monitor, "check_public_sites", return_value=reports) as check_public_sites:
            saved = run_main_with_state(state)

        check_public_sites.assert_called_once()
        self.assertEqual(saved, [])
        self.assertEqual(state["mode"], "watch_free_sites")
        self.assertEqual(state["target_chapter"], 11)

    def test_watch_free_sites_target_found_sends_notification_updates_state_and_saves(self) -> None:
        state = self.watch_free_state()
        reports = [ChapterReport("Light Novel World", 11, "Chapter Eleven", "https://public.example/11", "page")]

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
        reports = [ChapterReport("Light Novel World", 12, "Chapter Twelve", "https://public.example/12", "page")]
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
            "sources": ["Light Novel World"],
            "url": "https://public.example/11",
            "created_at": "2026-06-11T11:00:00+00:00",
            "first_failure_at": "2026-06-11T11:00:00+00:00",
            "last_attempt_at": "2026-06-11T11:00:00+00:00",
            "next_retry_at": "2026-06-11T11:10:00+00:00",
            "attempt_count": 1,
            "last_error_category": "http_error",
            "last_http_status": 503,
        }
        now = datetime(2026, 6, 11, 12, 5, tzinfo=timezone.utc)

        with patch.object(monitor, "pending_due", return_value=True), \
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

    def test_unknown_invalid_state_fields_are_still_rejected(self) -> None:
        state = base_state()
        state["unexpected_field"] = "bad"

        with self.assertRaises(StateError):
            validate_state(state)


if __name__ == "__main__":
    unittest.main()
