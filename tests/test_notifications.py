from __future__ import annotations

import unittest
from unittest.mock import patch

from shadow_slave_monitor.config import NTFY_BODY_MAX_BYTES, PUBLIC_SITES
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.notifications import (
    NEW_CHAPTER_NOTIFICATION_TITLE,
    merge_pending,
    notification_body,
    pending_from_report,
    report_from_pending,
    send_new_chapter,
    send_watchdog,
)
from shadow_slave_monitor.state_manager import validate_pending


class NewChapterNotificationTests(unittest.TestCase):
    def report(self, source: str = "Light Novel World", title: str | None = "Old Bones", url: str = "https://chapter.example/3117") -> ChapterReport:
        return ChapterReport(source, 3117, title, url, "page")

    def test_fixed_title_is_used_for_one_and_multiple_chapters(self) -> None:
        with patch("shadow_slave_monitor.notifications._post_ntfy") as post:
            send_new_chapter("topic", 3116, self.report())
            send_new_chapter("topic", 3115, self.report())

        self.assertEqual([call.args[1] for call in post.call_args_list], [NEW_CHAPTER_NOTIFICATION_TITLE] * 2)
        self.assertEqual(NEW_CHAPTER_NOTIFICATION_TITLE, "Shadow Slave Chapter Monitor")
        self.assertNotIn("New Shadow Slave chapter available", NEW_CHAPTER_NOTIFICATION_TITLE)
        self.assertNotIn("New Shadow Slave chapters available", NEW_CHAPTER_NOTIFICATION_TITLE)

    def test_watchdog_title_is_unchanged(self) -> None:
        with patch("shadow_slave_monitor.notifications._post_ntfy") as post:
            send_watchdog("errors", "problem")
        post.assert_called_once_with("errors", "Shadow Slave monitor error", "problem")

    def test_body_formats_one_chapter_one_source(self) -> None:
        self.assertEqual(notification_body(3116, self.report()), "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCE:\nLight Novel World [https://lightnovelworld.org/novel/shadow-slave]")

    def test_body_formats_one_chapter_multiple_sources_in_configured_order(self) -> None:
        body = notification_body(3116, self.report(" Telegram , Light Novel World, Telegram "))
        self.assertEqual(body, "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCES:\nLight Novel World [https://lightnovelworld.org/novel/shadow-slave]\nTelegram [https://t.me/s/shadow_slave_fastes]")

    def test_body_formats_multiple_chapters_one_and_multiple_sources(self) -> None:
        self.assertEqual(notification_body(3115, self.report()), "There are 2 new chapters available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCE:\nLight Novel World [https://lightnovelworld.org/novel/shadow-slave]")
        self.assertEqual(notification_body(3115, self.report("Light Novel World, Telegram")), "There are 2 new chapters available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCES:\nLight Novel World [https://lightnovelworld.org/novel/shadow-slave]\nTelegram [https://t.me/s/shadow_slave_fastes]")

    def test_body_handles_missing_title_unknown_sources_and_large_jump(self) -> None:
        self.assertEqual(notification_body(None, self.report(title=None)), "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117\n\nSOURCE:\nLight Novel World [https://lightnovelworld.org/novel/shadow-slave]")
        self.assertEqual(notification_body(3000, self.report("Mystery Site, Telegram")), "There are 117 new chapters available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCES:\nTelegram [https://t.me/s/shadow_slave_fastes]\nMystery Site")
        self.assertEqual(notification_body(3116, self.report("Mystery Site", url="https://mystery.example")), "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCE:\nMystery Site [https://mystery.example]")

    def test_known_sources_receive_their_own_general_urls(self) -> None:
        report = self.report(", ".join(site.name for site in reversed(PUBLIC_SITES)))
        body = notification_body(3116, report)
        expected_lines = [f"{site.name} [{site.url}]" for site in PUBLIC_SITES]
        self.assertEqual(body.splitlines()[4:], expected_lines)
        self.assertIn("\n\nSOURCES:\n", body)
        self.assertNotIn("Chapters ", body)
        self.assertNotIn("Source:", body)
        self.assertFalse(body.splitlines()[1].endswith("."))

    def test_novelarrow_uses_its_general_source_url(self) -> None:
        body = notification_body(3116, self.report("NovelArrow"))
        self.assertIn(
            "SOURCE:\nNovelArrow [https://novelarrow.com/novel/shadow-slave]",
            body,
        )

    def test_shadowslave_space_uses_its_general_source_url(self) -> None:
        body = notification_body(3116, self.report("ShadowSlave.Space"))
        self.assertIn(
            "SOURCE:\nShadowSlave.Space [https://shadowslave.space]",
            body,
        )

    def test_freewebnovel_uses_its_general_source_url(self) -> None:
        body = notification_body(
            3116,
            self.report(
                "FreeWebNovel",
                url="https://freewebnovel.com/novel/shadow-slave/chapter-3117",
            ),
        )
        self.assertIn(
            "SOURCE:\nFreeWebNovel [https://freewebnovel.com/novel/shadow-slave]",
            body,
        )
        self.assertNotIn("chapter-3117", body)

    def test_updated_sources_use_their_general_source_urls(self) -> None:
        body = notification_body(3116, self.report("Novel Buddy, NovelFire", url="https://chapter.example"))
        self.assertIn("Novel Buddy [https://novelbuddy.me/shadow-slave]", body)
        self.assertIn("NovelFire [https://novelfire.net/book/shadow-slave]", body)
        self.assertNotIn("chapter.example", body)

    def test_body_size_fallback_stays_within_utf8_limit(self) -> None:
        body = notification_body(1, self.report("Light Novel World, " + ("界" * 2000), "界" * 2000))
        self.assertLessEqual(len(body.encode("utf-8")), NTFY_BODY_MAX_BYTES)
        self.assertIn("Latest Chapter: 3117", body)


class PendingNotificationTests(unittest.TestCase):
    def report(self, source: str, chapter: int, title: str, url: str) -> ChapterReport:
        return ChapterReport(source, chapter, title, url, "page")

    def test_pending_retries_preserve_sources_and_formatting(self) -> None:
        pending = pending_from_report(3115, self.report("Light Novel World, Telegram", 3117, "Old Bones", "https://chapter.example"))
        previous, reconstructed = report_from_pending(pending)
        self.assertEqual(previous, 3115)
        self.assertEqual(notification_body(previous, reconstructed), notification_body(3115, self.report("Light Novel World, Telegram", 3117, "Old Bones", "https://chapter.example")))

    def test_pending_merges_same_chapter_but_replaces_sources_for_newer_chapter(self) -> None:
        pending = pending_from_report(3115, self.report("Light Novel World", 3116, "Older", "https://old.example"))
        pending = merge_pending(pending, 3115, self.report("Telegram, Light Novel World", 3116, "Ignored", "https://ignored.example"))
        self.assertEqual(pending["sources"], ["Light Novel World", "Telegram"])
        pending = merge_pending(pending, 3115, self.report("Telegram", 3117, "Newer", "https://new.example"))
        self.assertEqual(pending["sources"], ["Telegram"])
        self.assertEqual((pending["title"], pending["url"]), ("Newer", "https://new.example"))
        pending = merge_pending(pending, 3115, self.report("Light Novel World", 3117, "Still ignored", "https://ignored.example"))
        self.assertEqual(pending["sources"], ["Light Novel World", "Telegram"])

    def test_legacy_pending_state_still_migrates(self) -> None:
        legacy = {
            "previous_seen": 3116, "chapter": 3117, "title": "Old Bones", "source": "Telegram",
            "url": "https://chapter.example", "created_at": "2026-06-11T11:00:00+00:00", "attempts": 1,
        }
        migrated = validate_pending(legacy, 3116)
        self.assertEqual(migrated["sources"], ["Telegram"])
        self.assertEqual(migrated["latest_pending_chapter"], 3117)
