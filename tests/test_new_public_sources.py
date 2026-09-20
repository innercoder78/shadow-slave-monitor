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
    parse_rechapters_candidates,
)
from shadow_slave_monitor.state_manager import validate_source_config, validate_state, initial_state


class NewSourceConfigurationTests(unittest.TestCase):
    def test_exact_configs_are_appended_without_reordering_existing_sources(self) -> None:
        validate_source_config()
        names = [site.name for site in PUBLIC_SITES]
        self.assertEqual(names[-3:], ["ReadNovelFull", "ReChapters", "FreeWebNovel.net"])
        self.assertEqual(names[:13], [
            "Chikari", "Telegram", "Novel Buddy", "ShadowSlave.Space", "FreeWebNovel",
            "Novel Phoenix", "NovelArrow", "NovelFire", "SSNovel", "NovelFull",
            "Novel Live", "Readwn", "LightNovelUp",
        ])
        expected = {
            "ReadNovelFull": SourceConfig("ReadNovelFull", "https://readnovelfull.com/shadow-slave.html", True, ("readnovelfull.com", "www.readnovelfull.com")),
            "ReChapters": SourceConfig("ReChapters", "https://www.rechapters.com/book/shadow-slave-r2k2ivbd6ez4", True, ("rechapters.com", "www.rechapters.com")),
            "FreeWebNovel.net": SourceConfig("FreeWebNovel.net", "https://freewebnovel.net/shadow-slave.html", True, ("freewebnovel.net", "www.freewebnovel.net")),
        }
        self.assertEqual({site.name: site for site in PUBLIC_SITES[-3:]}, expected)
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


class ReChaptersTests(unittest.TestCase):
    source = next(site for site in PUBLIC_SITES if site.name == "ReChapters")

    def parse(self, html: str):
        return parse_rechapters_candidates(BeautifulSoup(html, "html.parser"), self.source.url)

    def test_stale_metadata_ignored_and_highest_visible_chapter_selected(self) -> None:
        html = '''<p>3181 chapters</p><p>Ch. 3101–3181</p><p>Newest first</p>
          <a href="/book/shadow-slave-r2k2ivbd6ez4/abc123xyz">Ch 3185: Mundane Fortune</a>
          <a href="/book/shadow-slave-r2k2ivbd6ez4/ggwdia9jih">Ch 3186: Lots of Fishes</a>'''
        candidates = self.parse(html)
        self.assertEqual([c.chapter for c in candidates], [3185, 3186])
        detail = '<html><h1>Ch 3186: lots   OF fishes</h1><p>Chapter 9999 noise</p></html>'
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[html, detail]) as fetch:
            report = check_public_site(self.source)
        self.assertEqual((report.chapter, report.title, report.url), (3186, "lots OF fishes", "https://www.rechapters.com/book/shadow-slave-r2k2ivbd6ez4/ggwdia9jih"))
        self.assertEqual(fetch.call_args_list, [call(self.source), call(self.source, report.url)])

    def test_opaque_id_and_continuations_never_supply_chapter(self) -> None:
        html = '''<a href="/book/shadow-slave-r2k2ivbd6ez4/9999">Read</a>
        <a href="/book/shadow-slave-r2k2ivbd6ez4/abc123xyz/2">Ch 3187 Wrong</a>
        <a href="/book/shadow-slave-r2k2ivbd6ez4/abc123xyz/2/more">Ch 3188 Wrong</a>'''
        self.assertEqual(self.parse(html), [])

    def test_namespace_authority_and_url_syntax_are_strict(self) -> None:
        invalid = [
            'http://www.rechapters.com/book/shadow-slave-r2k2ivbd6ez4/abc123xyz',
            'https://evil.example/book/shadow-slave-r2k2ivbd6ez4/abc123xyz',
            'https://user@rechapters.com/book/shadow-slave-r2k2ivbd6ez4/abc123xyz',
            'https://rechapters.com:443/book/shadow-slave-r2k2ivbd6ez4/abc123xyz',
            '/book/other-r2k2ivbd6ez4/abc123xyz', '/book/shadow-slave-r2k2ivbd6ez4/a!',
            '/book/shadow-slave-r2k2ivbd6ez4/abc123xyz?x=1', '/book/shadow-slave-r2k2ivbd6ez4/abc123xyz#x',
        ]
        for href in invalid:
            with self.subTest(href=href): self.assertEqual(self.parse(f'<a href="{href}">Ch 3186: Lots of Fishes</a>'), [])

    def test_verification_fails_on_wrong_missing_malformed_or_contradictory_heading(self) -> None:
        listing = '<a href="/book/shadow-slave-r2k2ivbd6ez4/ggwdia9jih">Ch 3186: Lots of Fishes</a>'
        pages = ['<h1>Ch 3185: Lots of Fishes</h1>', '<div>Ch 3186: Lots of Fishes</div>', '<h1>3186 Lots of Fishes</h1>', '<h1>Login</h1>', '<h1>Ch 3186: Different Title</h1>']
        for page in pages:
            with self.subTest(page=page), patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[listing, page]):
                with self.assertRaises(ParseError): check_public_site(self.source)

    def test_authoritative_target_confirms_title_only_newest_first_entry(self) -> None:
        listing = '''<nav><a href="/">Home</a><a href="/discover">Discover</a>
        <a href="/account">Account</a></nav>
        <a href="/book/shadow-slave-r2k2ivbd6ez4/outside9x">Chapter Wrong Outside</a>
        <section><h2>Chapter list</h2><p>3184 chapters · Updated today</p>
        <p>Newest first</p><p>Ch. 3101–3184</p>
        <a href="/book/shadow-slave-r2k2ivbd6ez4/newest9xyz">Chapter Entertaining Guest</a>
        <a href="/book/shadow-slave-r2k2ivbd6ez4/previous8x">Ch 3188: Lost Soul</a>
        <a href="/book/shadow-slave-r2k2ivbd6ez4/previous7x">Ch 3187: Pursuit of Light</a></section>'''
        self.assertEqual([c.chapter for c in self.parse(listing)], [3188, 3187])
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[listing, '<h1>Chapter Entertaining Guest</h1>']):
            report = check_public_site(self.source, None, 3189, "Entertaining Guest")
        self.assertEqual((report.chapter, report.title), (3189, "Entertaining Guest"))
        no_order = listing.replace("Newest first", "Chapter list")
        candidates = parse_rechapters_candidates(
            BeautifulSoup(no_order, "html.parser"), self.source.url, 3189, "Entertaining Guest")
        self.assertEqual([c.chapter for c in candidates], [3188, 3187])

    def test_title_only_inference_requires_first_entry_and_exact_predecessor(self) -> None:
        template = '''<section><h2>Chapter list</h2><p>Newest first</p>{entries}</section>'''
        title = '<a href="/book/shadow-slave-r2k2ivbd6ez4/newest9xyz">Chapter Entertaining Guest</a>'
        predecessor = '<a href="/book/shadow-slave-r2k2ivbd6ez4/previous8x">Ch 3188: Lost Soul</a>'
        wrong = '<a href="/book/shadow-slave-r2k2ivbd6ez4/previous7x">Ch 3187: Pursuit of Light</a>'
        cases = [title, predecessor + title, title + wrong]
        for entries in cases:
            with self.subTest(entries=entries):
                candidates = parse_rechapters_candidates(
                    BeautifulSoup(template.format(entries=entries), "html.parser"),
                    self.source.url, 3189, "Entertaining Guest",
                )
                self.assertNotIn(3189, [candidate.chapter for candidate in candidates])

    def test_title_only_inference_rejects_title_page_and_unsafe_url_mismatches(self) -> None:
        def listing(href: str, title: str = "Entertaining Guest") -> str:
            return f'''<section><h2>Chapter list</h2><p>Newest first</p>
            <a href="{href}">Chapter {title}</a>
            <a href="/book/shadow-slave-r2k2ivbd6ez4/previous8x">Ch 3188: Lost Soul</a></section>'''

        valid_href = "/book/shadow-slave-r2k2ivbd6ez4/newest9xyz"
        candidates = parse_rechapters_candidates(
            BeautifulSoup(listing(valid_href), "html.parser"), self.source.url,
            3189, "Different Title",
        )
        self.assertNotIn(3189, [candidate.chapter for candidate in candidates])
        for href in ("http://rechapters.com/book/shadow-slave-r2k2ivbd6ez4/newest9xyz",
                     "/book/shadow-slave-r2k2ivbd6ez4/newest9xyz?bad=1"):
            with self.subTest(href=href):
                candidates = parse_rechapters_candidates(
                    BeautifulSoup(listing(href), "html.parser"), self.source.url,
                    3189, "Entertaining Guest",
                )
                self.assertNotIn(3189, [candidate.chapter for candidate in candidates])
        with patch("shadow_slave_monitor.parsers.fetch_html", side_effect=[
            listing(valid_href), '<h1>Chapter Different Guest</h1>',
        ]):
            with self.assertRaises(ParseError):
                check_public_site(self.source, None, 3189, "Entertaining Guest")


class NewSourceIntegrationTests(unittest.TestCase):
    def test_dispatches_each_new_source_to_dedicated_parser(self) -> None:
        soup = BeautifulSoup("<html></html>", "html.parser")
        targets = {
            "ReadNovelFull": "parse_readnovelfull_candidates",
            "ReChapters": "parse_rechapters_candidates",
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
        for site in PUBLIC_SITES[-3:]:
            report = ChapterReport(site.name, 3186, "Title", "https://chapter.invalid/private")
            body = notification_body(3185, report)
            self.assertIn(f"{site.name} [{site.url}]", body)
            self.assertNotIn("chapter.invalid", body)


if __name__ == "__main__":
    unittest.main()
