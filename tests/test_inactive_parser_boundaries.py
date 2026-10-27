"""Offline trust contracts, not captured or freshly verified site templates.

The JSON fixture records provenance of every source's historical labels/URLs.
DOM wrappers and mutations below exercise conservative policy only. They cannot
establish that a current source renders these semantic relationships.
"""
from __future__ import annotations

from html import escape
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from shadow_slave_monitor import parsers
from shadow_slave_monitor.config import PUBLIC_SITES, SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT


OBSERVATIONS = json.loads((Path(__file__).parent / "fixtures/inactive_source_observations.json").read_text())
PARSERS = {
    "Novel Phoenix": parsers.parse_novel_phoenix_candidates,
    "NovelPing": parsers.parse_novelping_candidates,
    "NovelFire": parsers.parse_novelfire_candidates,
}
VALIDATORS = {
    "Novel Phoenix": parsers.novel_phoenix_candidate_from_anchor,
    "NovelPing": parsers.novelping_candidate_from_anchor,
    "NovelFire": parsers.novelfire_candidate_from_anchor,
}
SITES = {site.name: site for site in PUBLIC_SITES if site.name in PARSERS}


def link(name, *, href=None, label=None, attribute=""):
    evidence = OBSERVATIONS[name]
    href = evidence["href"] if href is None else href
    label = evidence["label"] if label is None else label
    return f'<a href="{escape(href, quote=True)}" {attribute}>{escape(label)}</a>'


def page(name, *, links=None, marker=None, identity="Shadow Slave"):
    evidence = OBSERVATIONS[name]
    links = link(name) if links is None else links
    heading = f"<h1>{identity}</h1>" if identity is not None else ""
    if name == "NovelFire":
        return heading + links
    marker = evidence["marker"] if marker is None else marker
    return heading + f"<section><b>{marker}</b>{links}</section>"


def parse(name, html, **kwargs):
    return PARSERS[name](BeautifulSoup(html, "html.parser"), SITES[name].url, **kwargs)


class InactiveParserBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.blocked = [self.enterContext(patch(target, side_effect=AssertionError("unexpected side effect")))
                        for target in ("requests.sessions.Session.request", "socket.socket.connect",
                                       "socket.getaddrinfo", "shadow_slave_monitor.parsers.fetch_html",
                                       "shadow_slave_monitor.parsers.fetch_json",
                                       "shadow_slave_monitor.state_manager.load_state",
                                       "shadow_slave_monitor.state_manager.save_state",
                                       "shadow_slave_monitor.state_manager.atomic_write_json",
                                       "shadow_slave_monitor.notifications._post_ntfy")]

    def tearDown(self):
        for blocked in self.blocked:
            blocked.assert_not_called()

    def assert_rejected(self, name, html, **kwargs):
        try:
            candidates = parse(name, html, **kwargs)
        except parsers.ParseError:
            return
        self.assertEqual(candidates, [], name)

    def test_provenance_never_claims_raw_html_or_current_releases(self):
        self.assertIs(OBSERVATIONS["raw_html_verified"], False)
        for name in PARSERS:
            self.assertIn("evidence", OBSERVATIONS[name])

    def test_historical_labels_and_urls_under_explicit_semantic_contract(self):
        for name in PARSERS:
            with self.subTest(source=name):
                result, = parse(name, page(name))
                evidence = OBSERVATIONS[name]
                self.assertEqual((result.source, result.chapter, result.title),
                                 (name, evidence["chapter"], evidence["title"]))
                self.assertEqual(parsers._safe_success_url(SITES[name], result), result.url)

    def test_unchanged_new_and_older_observations_are_pure_and_repeatable(self):
        for name in PARSERS:
            chapter = OBSERVATIONS[name]["chapter"]
            for reference in (chapter, chapter - 1, chapter + 1):
                with self.subTest(source=name, reference=reference):
                    soup = BeautifulSoup(page(name), "html.parser")
                    before = str(soup)
                    first = PARSERS[name](soup, SITES[name].url, reference_chapter=reference)
                    second = PARSERS[name](soup, SITES[name].url, reference_chapter=reference)
                    self.assertEqual(first, second)
                    self.assertEqual(first[0].chapter, chapter)
                    self.assertEqual(str(soup), before)

    def test_empty_malformed_and_metadata_only_pages_never_yield_chapters(self):
        for name in PARSERS:
            for html in ("", "<html><a href='><broken", "<h1>Shadow Slave</h1>",
                         "<h1>Shadow Slave</h1><p>9999 Chapters · 2026-10-27 · 123456 views · 4.9 ratings</p>"
                         "<nav>1 2 3 Next</nav>"):
                with self.subTest(source=name, html=html):
                    self.assert_rejected(name, html)

    def test_latest_section_is_required_for_the_two_semantic_adapters(self):
        for name in ("Novel Phoenix", "NovelPing"):
            self.assert_rejected(name, "<h1>Shadow Slave</h1>" + link(name))
            self.assert_rejected(name, page(name, marker="Chapter total:"))
            self.assert_rejected(name, page(name, links="<p>Chapter 9999</p>"))

    def test_catalog_and_recommendations_do_not_supply_latest_evidence(self):
        for name in ("Novel Phoenix", "NovelPing"):
            chapter = OBSERVATIONS[name]["chapter"]
            bogus = link(name).replace(str(chapter), "9999")
            html = page(name) + f'<section><h2>Recommendations</h2>{bogus}</section>'
            self.assertEqual([result.chapter for result in parse(name, html)], [chapter])
            # The old parent scan could pick up a sibling catalog after an empty marker.
            empty = page(name, links="") + f'<section><h2>Catalog</h2>{bogus}</section>'
            self.assert_rejected(name, empty)

    def test_latest_marker_does_not_expand_into_parent_catalog(self):
        name = "Novel Phoenix"
        html = '<main><h1>Shadow Slave</h1><span>Latest Release:</span><p>No release</p>' + link(name) + '</main>'
        self.assert_rejected(name, html)

    def test_conflicting_markers_fail_independently_of_document_order(self):
        for name in ("Novel Phoenix", "NovelPing"):
            good = page(name)
            different = page(name).replace(str(OBSERVATIONS[name]["chapter"]), "4000")
            for html in (good + different, different + good):
                with self.subTest(source=name):
                    self.assert_rejected(name, html)

    def test_later_invalid_marker_cannot_be_hidden_by_an_earlier_success(self):
        for name in ("Novel Phoenix", "NovelPing"):
            self.assert_rejected(name, page(name) + page(name, links=link(name, href="https://wrong.example/fake")))

    def test_duplicate_evidence_is_deduplicated_without_confusing_titles(self):
        for name in PARSERS:
            html = page(name, links=link(name) * 3)
            self.assertEqual(len(parse(name, html)), 1)
            self.assertEqual(len(parse(name, page(name) * 2)), 1)

    def test_contradictory_titles_fail_closed(self):
        for name in PARSERS:
            wrong = link(name).replace(OBSERVATIONS[name]["title"], "Different Title")
            self.assert_rejected(name, page(name, links=link(name) + wrong))
            self.assert_rejected(name, page(name, links=link(name, attribute='title="Different Title"')))

    def test_visible_number_must_equal_url_number_even_beside_a_good_candidate(self):
        for name in PARSERS:
            chapter = OBSERVATIONS[name]["chapter"]
            wrong_label = OBSERVATIONS[name]["label"].replace(str(chapter), str(chapter + 1))
            self.assert_rejected(name, page(name, links=link(name, label=wrong_label)))
            self.assert_rejected(name, page(name, links=link(name) + link(name, label=wrong_label)))

    def test_title_attribute_number_is_independently_checked(self):
        for name in PARSERS:
            self.assert_rejected(name, page(name, links=link(name, attribute='title="Chapter 1 Different Title"')))

    def test_missing_optional_titles_keep_independent_numbers_and_urls(self):
        for name in ("NovelPing", "NovelFire"):
            chapter = OBSERVATIONS[name]["chapter"]
            result, = parse(name, page(name, links=link(name, label=f"Chapter {chapter}")))
            self.assertEqual((result.chapter, result.title), (chapter, None))
        self.assert_rejected("Novel Phoenix", page("Novel Phoenix", links=link("Novel Phoenix", label="Chapter 3148")))

    def test_ping_title_slug_must_agree_and_number_is_not_guessed_from_text(self):
        name = "NovelPing"
        for label in ("Chapter 3227 Wrong Title", "3227 A Soul and a Shadow Walk Into a Bar",
                      "3227 Chapters", "Updated 2026-10-27", "Chapter 3227 Chapter 3228 Wrong"):
            self.assert_rejected(name, page(name, links=link(name, label=label)))
        self.assert_rejected(name, page(name, links=link(name, href=OBSERVATIONS[name]["href"] + "-wrong")))

    def test_ping_identity_is_required_and_cannot_be_a_recommendation(self):
        for identity in (None, "Another Novel", "Shadow Slave Impostor"):
            self.assert_rejected("NovelPing", page("NovelPing", identity=identity))
        self.assert_rejected("NovelPing", page("NovelPing") + "<h1>Another Novel</h1>")

    def test_explicit_wrong_series_headings_reject_the_retained_sources(self):
        for name in ("Novel Phoenix", "NovelFire"):
            self.assert_rejected(name, page(name, identity="Another Novel"))

    def test_navigation_and_explicitly_hidden_evidence_are_not_visible_releases(self):
        wrappers = ('<nav>{}</nav>', '<aside>{}</aside>', '<footer>{}</footer>',
                    '<div hidden>{}</div>', '<div aria-hidden="true">{}</div>',
                    '<div style="display: none">{}</div>', '<template>{}</template>')
        for name in PARSERS:
            for wrapper in wrappers:
                with self.subTest(source=name, wrapper=wrapper):
                    self.assert_rejected(name, wrapper.format(page(name)))

    def test_challenge_page_cannot_be_a_success_even_with_embedded_old_evidence(self):
        challenge = '<title>Just a moment...</title><form id="challenge-form"></form>'
        for name in PARSERS:
            self.assert_rejected(name, challenge + page(name))

    def test_hidden_child_text_and_comments_cannot_supply_visible_evidence(self):
        for name in PARSERS:
            evidence = OBSERVATIONS[name]
            hidden = link(name).replace(escape(evidence["label"]), '<span hidden>' + escape(evidence["label"]) + '</span>')
            self.assert_rejected(name, page(name, links=hidden))
            if name != "NovelFire":
                self.assert_rejected(name, '<h1>Shadow Slave</h1><section><!--' + evidence["marker"] + '-->' + link(name) + '</section>')
            # Hidden contradictory text must not poison a valid visible label.
            good = link(name).replace('</a>', '<span hidden>Chapter 9999 Wrong</span></a>')
            self.assertEqual(parse(name, page(name, links=good))[0].chapter, evidence["chapter"])

    def test_configured_hosts_and_root_relative_paths_share_canonical_validation(self):
        for name in PARSERS:
            evidence = OBSERVATIONS[name]
            original = evidence["href"]
            path = original.removeprefix("https://" + SITES[name].allowed_hosts[0])
            for href in (path, *("https://" + host + path for host in SITES[name].allowed_hosts)):
                with self.subTest(source=name, href=href):
                    result, = parse(name, page(name, links=link(name, href=href)))
                    self.assertEqual(result.chapter, evidence["chapter"])

    def test_wrong_series_path_is_rejected(self):
        for name in PARSERS:
            self.assert_rejected(name, page(name, links=link(name, href=OBSERVATIONS[name]["href"].replace("shadow-slave", "another-novel"))))

    def test_unsafe_authorities_schemes_ports_and_delimiters_are_rejected_raw(self):
        for name in PARSERS:
            evidence = OBSERVATIONS[name]
            site = SITES[name]
            path = evidence["href"].removeprefix(site.url.split("/book/")[0]) if name == "NovelPing" else evidence["href"]
            host = site.allowed_hosts[0]
            bad = [f"http://{host}{path}", f"https://wrong.example{path}",
                   f"https://user:password@{host}{path}", f"https://{host}:443{path}",
                   f"https://{host}:8443{path}", f"//{host}{path}", "javascript:alert(1)",
                   "data:text/html,fake", path + "?", path + "#", path + "?page=1", path + "#chapter",
                   path + ";session=1", path + "/extra", " " + path, path + "\n",
                   path.replace("chapter-", "chapter-%33"), path.replace("shadow-slave/", "shadow-slave/../shadow-slave/"),
                   path.replace(str(evidence["chapter"]), "%" + format(ord(str(evidence["chapter"])[0]), "02x") + str(evidence["chapter"])[1:], 1),
                   path.replace("shadow-slave", "%73hadow-slave"),
                   path.replace("/chapter-", "\\chapter-"), "https://[malformed" + path]
            for href in bad:
                with self.subTest(source=name, href=href):
                    anchor = BeautifulSoup(link(name, href=href), "html.parser").a
                    self.assertIsNone(VALIDATORS[name](anchor, site.url))
                    self.assert_rejected(name, page(name, links=link(name, href=href)))

    def test_integer_range_is_enforced_before_candidate_acceptance(self):
        for name in PARSERS:
            chapter = str(OBSERVATIONS[name]["chapter"])
            for value in ("0", "10001", "100001", "-1", "1.5", "true", "03227", "٣٢٢٧"):
                with self.subTest(source=name, value=value):
                    self.assert_rejected(name, page(name).replace(chapter, value))

    def test_suspicious_jumps_need_an_explicit_independent_reference(self):
        for name in PARSERS:
            chapter = OBSERVATIONS[name]["chapter"]
            self.assertEqual(parse(name, page(name), reference_chapter=chapter - SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT)[0].chapter, chapter)
            with self.assertRaisesRegex(parsers.ParseError, "inactive_suspicious_jump"):
                parse(name, page(name), reference_chapter=chapter - SUSPICIOUS_PUBLIC_CHAPTER_JUMP_LIMIT - 1)
            for invalid in (True, 0, "3227", 10001):
                with self.subTest(source=name, reference=invalid), self.assertRaises(parsers.ParseError):
                    parse(name, page(name), reference_chapter=invalid)

    def test_outage_and_recovery_do_not_cache_a_stale_success_or_failure(self):
        for name in PARSERS:
            first = parse(name, page(name))
            self.assert_rejected(name, "<p>Temporarily unavailable</p>")
            self.assertEqual(parse(name, page(name)), first)

    def test_fire_listing_never_becomes_a_candidate_with_or_without_a_number(self):
        evidence = OBSERVATIONS["NovelFire"]
        for label in (evidence["listing_label"], "Chapter 3098 Catch 22", "3098 Chapters"):
            anchor = BeautifulSoup(link("NovelFire", href=evidence["listing_href"], label=label), "html.parser").a
            self.assertIsNone(parsers.novelfire_candidate_from_anchor(anchor, SITES["NovelFire"].url))
            self.assert_rejected("NovelFire", str(anchor))

    def test_fire_summary_conflicts_in_both_number_and_title_are_fatal(self):
        evidence = OBSERVATIONS["NovelFire"]
        for summary in (evidence["listing_label"], "Novel Chapters Chapter 3098 Different Title"):
            html = page("NovelFire") + link("NovelFire", href=evidence["listing_href"], label=summary)
            with self.assertRaisesRegex(parsers.ParseError, "inactive_release_conflict"):
                parse("NovelFire", html)

    def test_fire_agreeing_summary_does_not_replace_individual_evidence(self):
        name = "NovelFire"
        summary = link(name, href=OBSERVATIONS[name]["listing_href"], label="Novel Chapters Chapter 3098 Catch 22")
        result, = parse(name, summary + page(name))
        self.assertTrue(result.url.endswith("/chapter-3098"))
        self.assert_rejected(name, summary)

    def test_fire_retains_numeric_titles_metadata_removal_and_ordered_chapter_lists(self):
        name = "NovelFire"
        recent = link(name, label="Chapter 3098 Catch 22 Updated 2 hours ago")
        earlier = link(name, href="/book/shadow-slave/chapter-3097", label="Chapter 3097 Earlier")
        candidates = parse(name, earlier + recent + earlier)
        self.assertEqual([(result.chapter, result.title) for result in candidates], [(3098, "Catch 22"), (3097, "Earlier")])

    def test_fire_unlinked_latest_label_cannot_be_hidden_by_an_older_canonical_list(self):
        self.assert_rejected("NovelFire", '<p>Latest Release: Chapter 3122 Field of Dishonor</p>' + page("NovelFire"))

    def test_overlong_titles_are_not_truncated_before_conflict_validation(self):
        for name in PARSERS:
            evidence = OBSERVATIONS[name]
            long_label = evidence["label"].replace(evidence["title"], "Long title " * 30 + "Chapter 9999")
            self.assert_rejected(name, page(name, links=link(name, label=long_label)))
        for name in ("Novel Phoenix", "NovelFire"):
            label = f'Chapter {OBSERVATIONS[name]["chapter"]} ' + 'A' * 180
            attribute = 'title="' + 'A' * 180 + ' Contradiction"'
            self.assert_rejected(name, page(name, links=link(name, label=label, attribute=attribute)))


if __name__ == "__main__":
    unittest.main()
