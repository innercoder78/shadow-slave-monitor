from __future__ import annotations

import unittest
from unittest.mock import patch

from shadow_slave_monitor.config import MAX_CHAPTER, MIN_CHAPTER, NTFY_BODY_MAX_BYTES, PUBLIC_SITES
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.notifications import (
    NEW_CHAPTER_NOTIFICATION_TITLE,
    merge_pending,
    notification_body,
    pending_from_report,
    report_from_pending,
    send_new_chapter,
    send_watchdog,
    source_names,
)
from shadow_slave_monitor.parsers import ParseError
from shadow_slave_monitor.state_manager import validate_pending


class NewChapterNotificationTests(unittest.TestCase):
    def report(self, source: str = "Chikari", title: str | None = "Old Bones", url: str = "https://chapter.example/3117") -> ChapterReport:
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
        report = self.report(url="https://chikari.moe/novels/shadow-slave/3149")
        body = notification_body(3116, report)
        self.assertEqual(body, "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCE:\nChikari [https://chikari.moe/novels/shadow-slave]")
        self.assertNotIn("/3149", body)

    def test_body_formats_one_chapter_multiple_sources_in_configured_order(self) -> None:
        body = notification_body(3116, self.report(" Telegram , Chikari, Telegram "))
        self.assertEqual(body, "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCES:\nChikari [https://chikari.moe/novels/shadow-slave]\nTelegram [https://t.me/s/shadow_slave_fastes]")

    def test_body_formats_multiple_chapters_one_and_multiple_sources(self) -> None:
        self.assertEqual(notification_body(3115, self.report()), "There are 2 new chapters available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCE:\nChikari [https://chikari.moe/novels/shadow-slave]")
        self.assertEqual(notification_body(3115, self.report("Chikari, Telegram")), "There are 2 new chapters available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCES:\nChikari [https://chikari.moe/novels/shadow-slave]\nTelegram [https://t.me/s/shadow_slave_fastes]")

    def test_body_handles_missing_title_unknown_sources_and_large_jump(self) -> None:
        self.assertEqual(notification_body(None, self.report(title=None)), "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117\n\nSOURCE:\nChikari [https://chikari.moe/novels/shadow-slave]")
        self.assertEqual(notification_body(3000, self.report("Mystery Site, Telegram")), "There are 117 new chapters available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCES:\nTelegram [https://t.me/s/shadow_slave_fastes]\nMystery Site")
        self.assertEqual(notification_body(3116, self.report("Mystery Site", url="https://mystery.example")), "There is 1 new chapter available on the free sites.\nLatest Chapter: 3117 — Old Bones\n\nSOURCE:\nMystery Site [https://mystery.example]")

    def test_known_sources_receive_their_own_notification_urls(self) -> None:
        report = self.report(", ".join(site.name for site in reversed(PUBLIC_SITES)))
        body = notification_body(3116, report)
        expected_lines = [
            f"{'ShadowSlave Space' if site.name == 'ShadowSlave.Space' else site.name} "
            f"[{f'https://rentry.co/{report.chapter}ss' if site.name == 'Rentry' else site.url}]"
            for site in PUBLIC_SITES
        ]
        self.assertEqual(body.splitlines()[4:], expected_lines)
        self.assertIn("\n\nSOURCES:\n", body)
        self.assertNotIn("\nChapters ", body)
        self.assertNotIn("Source:", body)
        self.assertFalse(body.splitlines()[1].endswith("."))

    def test_rentry_uses_verified_chapter_number_instead_of_report_url(self) -> None:
        for chapter in (3224, 3225, 3226, MIN_CHAPTER, MAX_CHAPTER):
            for url in ("https://rentry.co/", "https://rentry.co/9999ss", "https://untrusted.example/"):
                with self.subTest(chapter=chapter, url=url):
                    report = ChapterReport("Rentry", chapter, "Chapter Title", url)
                    with patch("shadow_slave_monitor.notifications._post_ntfy") as post:
                        send_new_chapter("topic", chapter - 1, report)
                    post.assert_called_once_with(
                        "topic", NEW_CHAPTER_NOTIFICATION_TITLE,
                        "There is 1 new chapter available on the free sites.\n"
                        f"Latest Chapter: {chapter} — Chapter Title\n\nSOURCE:\n"
                        f"Rentry [https://rentry.co/{chapter}ss]",
                    )

    def test_rentry_stays_last_with_mixed_sources_ordering_and_deduplication(self) -> None:
        self.assertEqual(PUBLIC_SITES[-1].name, "Rentry")
        source = " Rentry, Mystery Site, ShadowSlave.Space, Chikari, Rentry, Chikari "
        report = ChapterReport(source, 3225, "Chapter Title", "https://chikari.moe/novels/shadow-slave/3225")
        self.assertEqual(notification_body(3223, report),
                         "There are 2 new chapters available on the free sites.\n"
                         "Latest Chapter: 3225 — Chapter Title\n\nSOURCES:\n"
                         "Chikari [https://chikari.moe/novels/shadow-slave]\n"
                         "ShadowSlave Space [https://shadowslave.space]\n"
                         "Rentry [https://rentry.co/3225ss]\nMystery Site")
        self.assertEqual(report.source, source)

    def test_rentry_notification_rejects_invalid_chapter_numbers(self) -> None:
        for chapter in (True, False, "3225", "3225/evil", 3225.0, None, MIN_CHAPTER - 1, MAX_CHAPTER + 1, -1):
            with self.subTest(chapter=chapter), patch("shadow_slave_monitor.notifications._post_ntfy") as post:
                with self.assertRaises(ParseError):
                    send_new_chapter("topic", None, ChapterReport("Rentry", chapter, None, "https://untrusted.example"))
                post.assert_not_called()

    def test_rentry_override_requires_exact_canonical_identity(self) -> None:
        for source in ("rentry", "RENTRY", "Rentry Mirror", "https://rentry.co/"):
            with self.subTest(source=source):
                report = ChapterReport(source, 3225, None, "https://unknown.example/")
                self.assertEqual(notification_body(3224, report).splitlines()[4:],
                                 [f"{source} [https://unknown.example/]"])
                report = ChapterReport(f"{source}, Rentry", 3225, None, "https://untrusted.example/")
                self.assertEqual(notification_body(3224, report).splitlines()[4:],
                                 ["Rentry [https://rentry.co/3225ss]", source])

    def test_novelping_uses_its_general_source_url(self) -> None:
        body = notification_body(3116, self.report("NovelPing"))
        self.assertIn(
            "SOURCE:\nNovelPing [https://novelping.com/book/shadow-slave]",
            body,
        )

    def test_shadowslave_space_uses_its_general_source_url(self) -> None:
        body = notification_body(3116, self.report("ShadowSlave.Space"))
        self.assertIn(
            "SOURCE:\nShadowSlave Space [https://shadowslave.space]",
            body,
        )

    def test_display_label_preserves_canonical_order_and_deduplication(self) -> None:
        source = "Mystery Site, FreeWebNovel.net, ShadowSlave.Space, Telegram, ShadowSlave.Space"
        report = self.report(source)
        self.assertEqual(source_names(source), [
            "Telegram", "ShadowSlave.Space", "FreeWebNovel.net", "Mystery Site",
        ])
        body = notification_body(3116, report)
        self.assertEqual(body.splitlines()[4:], [
            "Telegram [https://t.me/s/shadow_slave_fastes]",
            "ShadowSlave Space [https://shadowslave.space]",
            "FreeWebNovel.net [https://freewebnovel.net/shadow-slave.html]",
            "Mystery Site",
        ])
        self.assertEqual(report.source, source)

    def test_display_label_is_still_an_unknown_source_identifier(self) -> None:
        self.assertEqual(source_names("ShadowSlave Space, ShadowSlave.Space, ShadowSlave Space"),
                         ["ShadowSlave.Space", "ShadowSlave Space"])
        body = notification_body(3116, self.report("ShadowSlave Space", url="https://unknown.example"))
        self.assertEqual(body.splitlines()[4:], ["ShadowSlave Space [https://unknown.example]"])
        body = notification_body(3116, self.report("ShadowSlave Space, Telegram"))
        self.assertEqual(body.splitlines()[4:], [
            "Telegram [https://t.me/s/shadow_slave_fastes]", "ShadowSlave Space",
        ])

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

    def test_novel_phoenix_uses_its_general_chapters_url(self) -> None:
        body = notification_body(
            3147,
            self.report(
                "Novel Phoenix",
                url="https://novelphoenix.com/novel/shadow-slave/chapter-3148",
            ),
        )
        self.assertIn(
            "SOURCE:\nNovel Phoenix [https://novelphoenix.com/novel/shadow-slave/chapters]",
            body,
        )
        self.assertNotIn("chapter-3148", body)

    def test_new_public_source_uses_its_general_book_url(self) -> None:
        body = notification_body(
            3181,
            ChapterReport("NovelFull", 3182, "Plight of Gods",
                          "https://novelfull.com/shadow-slave/chapter-3182-plight-of-gods.html"),
        )
        self.assertIn("NovelFull [https://novelfull.com/shadow-slave.html]", body)
        self.assertNotIn("chapter-3182", body)

    def test_retired_source_provenance_is_preserved_without_a_configured_url(self) -> None:
        body = notification_body(3116, self.report("Novel Buddy, NovelFire", url="https://chapter.example"))
        self.assertIn("Novel Buddy [https://novelbuddy.me/shadow-slave]", body)
        self.assertIn("\nNovelFire", body)
        self.assertNotIn("novelfire.net", body)
        self.assertNotIn("chapter.example", body)

    def test_retired_single_source_keeps_its_recorded_pending_url(self) -> None:
        url = "https://novelfire.net/book/shadow-slave/chapter-3117"
        body = notification_body(3116, self.report("NovelFire", url=url))
        self.assertIn(f"NovelFire [{url}]", body)

    def test_body_size_fallback_stays_within_utf8_limit(self) -> None:
        body = notification_body(1, self.report("Chikari, " + ("界" * 2000), "界" * 2000))
        self.assertLessEqual(len(body.encode("utf-8")), NTFY_BODY_MAX_BYTES)
        self.assertIn("Latest Chapter: 3117", body)

    def test_all_body_size_fallbacks_use_display_label(self) -> None:
        availability = "There is 1 new chapter available on the free sites.\n"
        cases = [
            ("without_urls", self.report("ShadowSlave.Space, Mystery Site"),
             availability + "Latest Chapter: 3117 — Old Bones\n\nSOURCES:\nShadowSlave Space\nMystery Site"),
            ("without_title", self.report("ShadowSlave.Space", title="界" * 2000),
             availability + "Latest Chapter: 3117\n\nSOURCE:\nShadowSlave Space"),
            ("limited_sources", self.report("ShadowSlave.Space, " + "界" * 2000),
             availability + "Latest Chapter: 3117\n\nSOURCES:\nShadowSlave Space"),
        ]
        for name, report, expected in cases:
            with self.subTest(fallback=name):
                limit = len(expected.encode("utf-8"))
                with patch("shadow_slave_monitor.notifications.NTFY_BODY_MAX_BYTES", limit):
                    body = notification_body(3116, report)
                self.assertEqual(body, expected)
                self.assertLessEqual(len(body.encode("utf-8")), limit)

    def test_rentry_body_size_fallbacks_remove_urls_without_using_homepage(self) -> None:
        availability = "There is 1 new chapter available on the free sites.\n"
        cases = [
            ("without_urls", "Rentry, ShadowSlave.Space", "Chapter Title",
             "Latest Chapter: 3225 — Chapter Title\n\nSOURCES:\nShadowSlave Space\nRentry"),
            ("without_title", "Rentry", "界" * 2000,
             "Latest Chapter: 3225\n\nSOURCE:\nRentry"),
            ("limited_sources", "Rentry, " + "界" * 2000, "Chapter Title",
             "Latest Chapter: 3225\n\nSOURCES:\nRentry"),
        ]
        for name, source, title, lines in cases:
            with self.subTest(fallback=name):
                expected = availability + lines
                report = ChapterReport(source, 3225, title, "https://rentry.co/")
                limit = len(expected.encode("utf-8"))
                with patch("shadow_slave_monitor.notifications.NTFY_BODY_MAX_BYTES", limit):
                    body = notification_body(3224, report)
                self.assertEqual(body, expected)
                self.assertLessEqual(len(body.encode("utf-8")), limit)
                self.assertNotIn("https://", body)


class PendingNotificationTests(unittest.TestCase):
    def report(self, source: str, chapter: int, title: str, url: str) -> ChapterReport:
        return ChapterReport(source, chapter, title, url, "page")

    def test_pending_retries_preserve_sources_and_formatting(self) -> None:
        pending = pending_from_report(3115, self.report("Chikari, Telegram", 3117, "Old Bones", "https://chapter.example"))
        previous, reconstructed = report_from_pending(pending)
        self.assertEqual(previous, 3115)
        self.assertEqual(notification_body(previous, reconstructed), notification_body(3115, self.report("Chikari, Telegram", 3117, "Old Bones", "https://chapter.example")))

    def test_pending_retries_display_label_without_changing_source_identity(self) -> None:
        for source, expected_sources, expected_lines in [
            ("ShadowSlave.Space", ["ShadowSlave.Space"],
             ["ShadowSlave Space [https://shadowslave.space]"]),
            ("ShadowSlave.Space, Telegram, ShadowSlave.Space", ["Telegram", "ShadowSlave.Space"],
             ["Telegram [https://t.me/s/shadow_slave_fastes]", "ShadowSlave Space [https://shadowslave.space]"]),
        ]:
            with self.subTest(source=source):
                pending = pending_from_report(3115, self.report(source, 3117, "Old Bones", "https://chapter.example"))
                self.assertEqual(pending["sources"], expected_sources)
                restored = validate_pending(pending, 3115)
                self.assertEqual(restored["sources"], expected_sources)
                previous, report = report_from_pending(restored)
                self.assertEqual(report.source, ", ".join(expected_sources))
                with patch("shadow_slave_monitor.notifications._post_ntfy") as post:
                    send_new_chapter("topic", previous, report)
                post.assert_called_once_with("topic", NEW_CHAPTER_NOTIFICATION_TITLE, notification_body(previous, report))
                self.assertEqual(post.call_args.args[2].splitlines()[4:], expected_lines)
                self.assertEqual(pending["sources"], expected_sources)

    def test_pending_merges_same_chapter_but_replaces_sources_for_newer_chapter(self) -> None:
        pending = pending_from_report(3115, self.report("Chikari", 3116, "Older", "https://old.example"))
        pending = merge_pending(pending, 3115, self.report("Telegram, Chikari", 3116, "Ignored", "https://ignored.example"))
        self.assertEqual(pending["sources"], ["Chikari", "Telegram"])
        pending = merge_pending(pending, 3115, self.report("Telegram", 3117, "Newer", "https://new.example"))
        self.assertEqual(pending["sources"], ["Telegram"])
        self.assertEqual((pending["title"], pending["url"]), ("Newer", "https://new.example"))
        pending = merge_pending(pending, 3115, self.report("Chikari", 3117, "Still ignored", "https://ignored.example"))
        self.assertEqual(pending["sources"], ["Chikari", "Telegram"])

    def test_legacy_pending_state_still_migrates(self) -> None:
        legacy = {
            "previous_seen": 3116, "chapter": 3117, "title": "Old Bones", "source": "Telegram",
            "url": "https://chapter.example", "created_at": "2026-06-11T11:00:00+00:00", "attempts": 1,
        }
        migrated = validate_pending(legacy, 3116)
        self.assertEqual(migrated["sources"], ["Telegram"])
        self.assertEqual(migrated["latest_pending_chapter"], 3117)
