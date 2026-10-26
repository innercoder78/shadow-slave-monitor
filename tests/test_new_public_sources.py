from __future__ import annotations

import unittest
from unittest.mock import call, patch

from bs4 import BeautifulSoup

from shadow_slave_monitor.config import PUBLIC_SITES, PUBLIC_SITE_ORDER, SourceConfig
from shadow_slave_monitor.monitor import check_public_sites
from shadow_slave_monitor.models import ChapterReport, RunResult
from shadow_slave_monitor.notifications import notification_body
from shadow_slave_monitor.parsers import (
    ParseError,
    check_public_site,
    iter_public_candidates,
    parse_freewebnovel_candidates,
    parse_freewebnovel_net_candidates,
    parse_readnovelfull_candidates,
)
from shadow_slave_monitor.state_manager import validate_source_config, validate_state, initial_state


class NewSourceConfigurationTests(unittest.TestCase):
    def test_exact_configs_are_appended_without_reordering_existing_sources(self) -> None:
        validate_source_config()
        names = [site.name for site in PUBLIC_SITES]
        self.assertEqual(names[11:13], ["ReadNovelFull", "FreeWebNovel.net"])
        self.assertEqual(names[:11], [
            "Chikari", "Telegram", "Novel Buddy", "ShadowSlave.Space", "FreeWebNovel",
            "Novel Phoenix", "NovelPing", "NovelFire", "SSNovel", "NovelFull",
            "LightNovelUp",
        ])
        expected = {
            "ReadNovelFull": SourceConfig("ReadNovelFull", "https://readnovelfull.com/shadow-slave.html", True, ("readnovelfull.com", "www.readnovelfull.com")),
            "FreeWebNovel.net": SourceConfig("FreeWebNovel.net", "https://freewebnovel.net/shadow-slave.html", True, ("freewebnovel.net", "www.freewebnovel.net")),
        }
        self.assertEqual({site.name: site for site in PUBLIC_SITES[11:13]}, expected)
        self.assertEqual([PUBLIC_SITE_ORDER[name] for name in names], list(range(len(names))))

    def test_com_and_net_are_distinct_config_and_failure_identities(self) -> None:
        com = next(site for site in PUBLIC_SITES if site.name == "FreeWebNovel")
        net = next(site for site in PUBLIC_SITES if site.name == "FreeWebNovel.net")
        self.assertEqual(com.allowed_hosts, ("freewebnovel.com", "www.freewebnovel.com"))
        self.assertEqual(net.allowed_hosts, ("freewebnovel.net", "www.freewebnovel.net"))
        self.assertNotEqual(com.name, net.name)
        state = initial_state()
        state["public_source_failures"] = {com.name: 1, net.name: 2}
        self.assertEqual(validate_state(state)["public_source_failures"], {com.name: 1, net.name: 2})


class ReadNovelFullTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "ReadNovelFull")

    def parse(self, html: str):
        return parse_readnovelfull_candidates(BeautifulSoup(html, "html.parser"), self.source.url)

    def test_semantic_latest_canonical_link_and_title(self) -> None:
        html = '<p>9999 chapters</p><section><h3>Latest chapter</h3><a href="/shadow-slave/chapter-3186-lots-of-fishes.html">Chapter 3186: Lots\n of Fishes</a></section>'
        candidates = self.parse(html)
        self.assertEqual([(c.chapter, c.title, c.url) for c in candidates], [(3186, "Lots of Fishes", "https://readnovelfull.com/shadow-slave/chapter-3186-lots-of-fishes.html")])
        with patch("shadow_slave_monitor.parsers.fetch_html", return_value=html) as fetch:
            self.assertEqual(check_public_site(self.source).chapter, 3186)
        fetch.assert_called_once_with(self.source)

    def test_url_only_evidence_preserves_chapter_without_inventing_title(self) -> None:
        candidates = self.parse('<h3>Latest chapter</h3><div><a href="/shadow-slave/chapter-3186-lots-of-fishes.html">Read now</a></div>')
        self.assertEqual((candidates[0].chapter, candidates[0].title), (3186, None))

    def test_mismatch_missing_or_ambiguous_semantics_fail_closed(self) -> None:
        cases = [
            '<h3>Latest chapter</h3><a href="/shadow-slave/chapter-3186-title.html">Chapter 3185 Wrong</a>',
            '<a href="/shadow-slave/chapter-3186-title.html">Chapter 3186 Good</a>',
            '<h3>Latest chapter</h3><a href="/shadow-slave/chapter-3186-a.html">Chapter 3186 A</a><h3>Latest chapter</h3><a href="/shadow-slave/chapter-3187-b.html">Chapter 3187 B</a>',
            '<section><h3>Latest chapter</h3><a href="/shadow-slave/chapter-3186-a.html">Chapter 3186 A</a><a href="/shadow-slave/chapter-3187-b.html">Chapter 3187 B</a></section>',
        ]
        for html in cases:
            with self.subTest(html=html): self.assertEqual(self.parse(html), [])

    def test_unsafe_and_noncanonical_links_rejected(self) -> None:
        invalid = [
            'http://readnovelfull.com/shadow-slave/chapter-3186-title.html',
            'https://evil.example/shadow-slave/chapter-3186-title.html',
            'https://user@readnovelfull.com/shadow-slave/chapter-3186-title.html',
            'https://readnovelfull.com:443/shadow-slave/chapter-3186-title.html',
            '/other/chapter-3186-title.html', '/shadow-slave/chapter-3186.html',
            '/shadow-slave/chapter-3186-title.html?x=1', '/shadow-slave/chapter-3186-title.html#x',
            '/shadow-slave/chapter-3186-title.html;session=1',
        ]
        for href in invalid:
            with self.subTest(href=href):
                self.assertEqual(self.parse(f'<h3>Latest chapter</h3><a href="{href}">Chapter 3186: Title</a>'), [])

    def test_authoritative_target_confirms_title_only_latest(self) -> None:
        listing = '<section><h3>Latest chapter</h3><a href="/shadow-slave/chapter-entertaining-guest.html">Chapter Entertaining Guest</a></section>'
        self.assertEqual(self.parse(listing), [])
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[listing, '<h1>Chapter Entertaining Guest</h1>']):
            report = check_public_site(self.source, None, 3189, "Entertaining Guest")
        self.assertEqual((report.chapter, report.title), (3189, "Entertaining Guest"))
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[listing, '<h1>Chapter Wrong Guest</h1>']):
            with self.assertRaises(ParseError):
                check_public_site(self.source, None, 3189, "Entertaining Guest")

    def test_title_only_latest_rejects_wrong_target_and_ambiguity(self) -> None:
        link = '<a href="/shadow-slave/chapter-entertaining-guest.html">Chapter Entertaining Guest</a>'
        self.assertEqual(parse_readnovelfull_candidates(
            BeautifulSoup(f'<h3>Latest chapter</h3>{link}', "html.parser"), self.source.url,
            3189, "Different Title"), [])
        self.assertEqual(parse_readnovelfull_candidates(
            BeautifulSoup(f'<h3>Latest chapter</h3>{link}<h3>Latest chapter</h3>{link}', "html.parser"),
            self.source.url, 3189, "Entertaining Guest"), [])


class FreeWebNovelNetTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "FreeWebNovel.net")

    def parse(self, html: str):
        return parse_freewebnovel_net_candidates(BeautifulSoup(html, "html.parser"), self.source.url)

    def test_canonical_url_visible_agreement_title_cleaning_and_noise(self) -> None:
        html = '<p>9999 chapters page 88</p><a href="/shadow-slave/chapter-3167-a-small-memento.html">Chapter 3167: A\n Small Memento</a>'
        candidate = self.parse(html)[0]
        self.assertEqual((candidate.chapter, candidate.title), (3167, "A Small Memento"))

    def test_missing_title_is_allowed_but_mismatch_is_not(self) -> None:
        candidate = self.parse('<a href="/shadow-slave/chapter-3167-a-small-memento.html">Read</a>')[0]
        self.assertIsNone(candidate.title)
        self.assertEqual(self.parse('<a href="/shadow-slave/chapter-3167-a.html">Chapter 3168 Wrong</a>'), [])

    def test_com_and_other_unsafe_links_are_rejected_by_net(self) -> None:
        invalid = [
            'https://freewebnovel.com/shadow-slave/chapter-3167-title.html',
            'http://freewebnovel.net/shadow-slave/chapter-3167-title.html',
            'https://evil.example/shadow-slave/chapter-3167-title.html',
            'https://user@freewebnovel.net/shadow-slave/chapter-3167-title.html',
            'https://freewebnovel.net:443/shadow-slave/chapter-3167-title.html',
            '/other/chapter-3167-title.html', '/shadow-slave/chapter-3167.html',
            '/shadow-slave/chapter-3167-title.html?x=1', '/shadow-slave/chapter-3167-title.html#x',
            '/shadow-slave/chapter-3167-title.html;session=1',
        ]
        for href in invalid:
            with self.subTest(href=href): self.assertEqual(self.parse(f'<a href="{href}">Chapter 3167 Title</a>'), [])

    def test_existing_com_parser_rejects_net(self) -> None:
        soup = BeautifulSoup('<h2>Latest Chapters</h2><a href="https://freewebnovel.net/shadow-slave/chapter-3167-title.html">Chapter 3167 Title</a>', "html.parser")
        self.assertEqual(parse_freewebnovel_candidates(soup, "https://freewebnovel.com/novel/shadow-slave"), [])

    def test_authoritative_target_requires_first_item_and_predecessor(self) -> None:
        def listing(predecessor: int = 3188) -> str:
            return f'''<section><h2>6 Latest Chapters [ Updated an hour ago ]</h2>
            <a href="/shadow-slave/chapter-entertaining-guest.html">Chapter Entertaining Guest</a>
            <a href="/shadow-slave/chapter-{predecessor}-lost-soul.html">Chapter {predecessor} Lost Soul</a></section>'''
        self.assertEqual(max(c.chapter for c in self.parse(listing())), 3188)
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[listing(), '<h2>Chapter Entertaining Guest</h2>']):
            report = check_public_site(self.source, None, 3189, "Entertaining Guest")
        self.assertEqual(report.chapter, 3189)
        rejected = parse_freewebnovel_net_candidates(
            BeautifulSoup(listing(3187), "html.parser"), self.source.url, 3189, "Entertaining Guest")
        self.assertEqual(rejected, [])
        for html, title in (
            (listing().replace("6 Latest Chapters [ Updated an hour ago ]", "This is the latest news"), "Entertaining Guest"),
            (listing(), "Wrong Title"),
        ):
            with self.subTest(html=html, title=title):
                reports = parse_freewebnovel_net_candidates(
                    BeautifulSoup(html, "html.parser"), self.source.url, 3189, title)
                self.assertFalse(any(c.chapter == 3189 for c in reports))
        with patch("shadow_slave_monitor.parsers.fetch_html",
                   side_effect=[listing(), "<h2>Chapter Wrong Title</h2>"]), self.assertRaises(ParseError):
            check_public_site(self.source, None, 3189, "Entertaining Guest")

    def test_target_context_accepts_numbered_first_latest_entry(self) -> None:
        html = '''
          <a href="/shadow-slave/chapter-3999-untrusted.html">Chapter 3999 Untrusted</a>
          <section><h2>6 Latest Chapters [ Updated an hour ago ]</h2>
            <a href="/shadow-slave/chapter-3191-there-and-back-again.html">Chapter 3191 There and Back Again</a>
            <a href="/shadow-slave/chapter-3190-freedom-of-choice.html">Chapter 3190 Freedom of Choice</a>
          </section>
        '''
        contexts = (
            (3192, "A Hypothetical Future", 3191, "There and Back Again"),
            (3191, "There and Back Again", 3190, "Freedom of Choice"),
        )
        for context in contexts:
            with self.subTest(context=context):
                candidates = parse_freewebnovel_net_candidates(
                    BeautifulSoup(html, "html.parser"), self.source.url, *context
                )
                self.assertEqual([candidate.chapter for candidate in candidates], [3191])

    def test_target_context_rejects_numbered_first_visible_url_mismatch(self) -> None:
        html = '''
          <section><h2>6 Latest Chapters [ Updated an hour ago ]</h2>
            <a href="/shadow-slave/chapter-3191-there-and-back-again.html">Chapter 3192 There and Back Again</a>
            <a href="/shadow-slave/chapter-3190-freedom-of-choice.html">Chapter 3190 Freedom of Choice</a>
          </section>
        '''
        candidates = parse_freewebnovel_net_candidates(
            BeautifulSoup(html, "html.parser"), self.source.url,
            3192, "A Hypothetical Future", 3191, "There and Back Again",
        )
        self.assertEqual(candidates, [])

    def test_target_context_accepts_single_numbered_latest_but_not_single_title_only(self) -> None:
        marker = "<h2>6 Latest Chapters [ Updated an hour ago ]</h2>"
        numbered = '<a href="/shadow-slave/chapter-3191-there-and-back-again.html">Chapter 3191 There and Back Again</a>'
        title_only = '<a href="/shadow-slave/chapter-there-and-back-again.html">Chapter There and Back Again</a>'
        context = (3192, "A Hypothetical Future", 3191, "There and Back Again")

        candidates = parse_freewebnovel_net_candidates(
            BeautifulSoup(f"<section>{marker}{numbered}</section>", "html.parser"),
            self.source.url, *context,
        )
        self.assertEqual([candidate.chapter for candidate in candidates], [3191])
        self.assertEqual(parse_freewebnovel_net_candidates(
            BeautifulSoup(f"<section>{marker}{title_only}</section>", "html.parser"),
            self.source.url, *context,
        ), [])


class NewSourceIntegrationTests(unittest.TestCase):
    def test_dispatches_each_new_source_to_dedicated_parser(self) -> None:
        soup = BeautifulSoup("<html></html>", "html.parser")
        targets = {
            "ReadNovelFull": "parse_readnovelfull_candidates",
            "FreeWebNovel.net": "parse_freewebnovel_net_candidates",
        }
        for name, target in targets.items():
            with self.subTest(name=name), patch(f"shadow_slave_monitor.parsers.{target}", return_value=[]) as parser:
                iter_public_candidates(soup, "https://example.invalid", name)
                parser.assert_called_once()

    def test_one_new_source_failure_degrades_while_another_succeeds(self) -> None:
        selected = [site for site in PUBLIC_SITES if site.name in {"ReadNovelFull", "FreeWebNovel.net"}]
        success = ChapterReport("FreeWebNovel.net", 3186, None, "https://freewebnovel.net/shadow-slave/chapter-3186-title.html")
        result = RunResult()
        with patch("shadow_slave_monitor.monitor.PUBLIC_SITES", tuple(selected)), patch("shadow_slave_monitor.monitor.check_public_site", side_effect=lambda site: success if site.name == success.source else (_ for _ in ()).throw(ParseError("no result"))):
            reports = check_public_sites(result, {})
        self.assertEqual(reports, [success])
        self.assertIn("optional public sources failed", result.degraded_reasons[0])

    def test_notifications_use_configured_general_urls(self) -> None:
        for site in PUBLIC_SITES[11:13]:
            report = ChapterReport(site.name, 3186, "Title", "https://chapter.invalid/private")
            body = notification_body(3185, report)
            self.assertIn(f"{site.name} [{site.url}]", body)
            self.assertNotIn("chapter.invalid", body)


if __name__ == "__main__":
    unittest.main()
