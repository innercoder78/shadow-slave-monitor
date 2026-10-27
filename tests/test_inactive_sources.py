"""Inactive inventory boundaries and compatibility with the previous inventory."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from shadow_slave_monitor import diagnose_source, monitor, parsers, state_manager
from shadow_slave_monitor.config import PUBLIC_SITES, PUBLIC_SITE_ORDER, PUBLIC_SOURCE_PARSER_REVISION, SourceConfig
from shadow_slave_monitor.models import ChapterReport, Health, RunResult
from shadow_slave_monitor.notifications import merge_pending, NotificationDeliveryError


# Exact previous identities are retained only to exercise migration and rejection.
OBSOLETE = ("Novel Phoenix", "NovelArrow", "NovelFire", "Novel Live", "Readwn", "ReChapters")
DISABLED = ("Novel Phoenix", "NovelPing")
ENABLED = [
    "Chikari", "Telegram", "Novel Buddy", "ShadowSlave.Space", "FreeWebNovel",
    "SSNovel", "NovelFull", "LightNovelUp", "ReadNovelFull", "FreeWebNovel.net", "Rentry",
]


class InactiveInventoryTests(unittest.TestCase):
    def test_retired_parser_entry_points_are_absent(self):
        for name in ("novelfire_candidate_from_anchor", "parse_novelfire_candidates",
                     "_strip_novelfire_metadata"):
            self.assertFalse(hasattr(parsers, name), name)

    def test_exact_inventory_order_and_disabled_references(self):
        state_manager.validate_source_config()
        self.assertEqual([site.name for site in PUBLIC_SITES], [
            *ENABLED[:5], *DISABLED, *ENABLED[5:],
        ])
        self.assertEqual([site.name for site in PUBLIC_SITES if site.enabled], ENABLED)
        self.assertEqual(PUBLIC_SITE_ORDER, {site.name: i for i, site in enumerate(PUBLIC_SITES)})
        self.assertEqual([site for site in PUBLIC_SITES if not site.enabled], [
            SourceConfig("Novel Phoenix", "https://novelphoenix.com/novel/shadow-slave/chapters", False,
                         ("novelphoenix.com", "www.novelphoenix.com")),
            SourceConfig("NovelPing", "https://novelping.com/book/shadow-slave", False,
                         ("novelping.com", "www.novelping.com")),
        ])

    def test_regular_and_recovery_checks_only_schedule_enabled_sources(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                failures = {name: 4 for name in ENABLED} if recovery else {}
                positions = {}
                result = RunResult()

                def check(site, *args):
                    self.assertTrue(site.enabled)
                    return ChapterReport(site.name, 3224, "Baseline", site.url)

                with patch.object(monitor, "public_source_recovery_window_open", return_value=recovery), \
                     patch.object(monitor, "check_public_site", side_effect=check) as checker, \
                     patch.object(monitor, "send_new_chapter") as notify:
                    reports = monitor.check_public_sites(result, failures, positions, 3225, "Target", 3224, "Baseline")
                self.assertCountEqual([call.args[0].name for call in checker.call_args_list], ENABLED)
                self.assertEqual([report.source for report in reports], ENABLED)
                self.assertEqual(failures, {})
                self.assertEqual(positions, {})
                self.assertEqual(result.status, Health.HEALTHY)
                aggregate = monitor.aggregate_reports_for_chapter(reports, 3224)
                self.assertEqual(aggregate.source, ", ".join(ENABLED))
                self.assertEqual(aggregate.url, PUBLIC_SITES[0].url)
                self.assertIsNone(monitor.aggregate_reports_for_chapter(reports, 3225))
                notify.assert_not_called()

    def test_inactive_names_cannot_enter_network_or_candidate_dispatch(self):
        soup = BeautifulSoup('<a href="/chapter-9999">Chapter 9999 Bogus</a>', "html.parser")
        for name in (*OBSOLETE, "NovelPing", "Unknown"):
            with self.subTest(name=name), patch.object(parsers, "fetch_html") as html, \
                 patch.object(parsers, "fetch_json") as api, \
                 patch.object(parsers, "parse_novelarrow_candidates") as old_parser:
                configured = next((site for site in PUBLIC_SITES if site.name == name),
                                  SourceConfig(name, "https://retired.example/", True, ("retired.example",)))
                # Even a stale caller setting enabled=True cannot activate an identity.
                for site in (configured, replace(configured, enabled=True)):
                    with self.assertRaises(parsers.ParseError):
                        parsers.check_public_site(site, None, 3225, "Target")
                self.assertEqual(parsers.iter_public_candidates(soup, configured.url, name, 3225, "Target"), [])
                html.assert_not_called()
                api.assert_not_called()
                old_parser.assert_not_called()
        self.assertTrue(monitor.TARGET_AWARE_PUBLIC_SOURCES.isdisjoint((*OBSOLETE, "NovelPing")))

    def test_manual_diagnostics_reject_inactive_entries_before_loading_state(self):
        for name in (*OBSOLETE, "NovelPing"):
            with self.subTest(name=name), patch.object(diagnose_source, "load_state") as load, \
                 patch.object(diagnose_source, "check_public_site") as check:
                self.assertEqual(diagnose_source.diagnose_source(name), 2)
            load.assert_not_called()
            check.assert_not_called()


class PreviousInventoryMigrationTests(unittest.TestCase):
    def previous_state(self):
        state = state_manager.initial_state()
        state.update(latest_seen=3224, latest_title="Baseline", latest_url=PUBLIC_SITES[0].url,
                     latest_webnovel=3226, latest_webnovel_title="Target", mode="watch_free_sites",
                     target_chapter=3226, target_title="Target", target_url="https://www.webnovel.com/target",
                     public_source_failure_revision=7, webnovel_success_cycle=99,
                     updated_at="2026-10-25T12:00:00+00:00")
        state["public_source_failures"] = {**dict.fromkeys(OBSOLETE, 4), "Chikari": 2, "LightNovelUp": 4}
        state["source_positions"] = {"LightNovelUp": {
            "chapter": 3224, "url": "https://lightnovelup.com/novel/shadow-slave/chapter-3224-baseline/",
        }}
        state["pending_notification"] = merge_pending(
            None, 3224, ChapterReport("Novel Live", 3225, "Pending", "https://retired.example/"),
            NotificationDeliveryError("http_error", 503, 600),
        )
        return state

    def test_previous_state_load_preserves_all_other_fields_and_pending_retry(self):
        previous = self.previous_state()
        before = copy.deepcopy(previous)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(previous), encoding="utf-8")
            original = path.read_bytes()
            clean, first = state_manager.load_state(path)
            self.assertFalse(first)
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(previous, before)
        self.assertEqual(clean["public_source_failures"], {"Chikari": 2, "LightNovelUp": 4})
        self.assertEqual(clean["public_source_failure_revision"], PUBLIC_SOURCE_PARSER_REVISION)
        for key in previous.keys() - {"public_source_failures", "public_source_failure_revision"}:
            self.assertEqual(clean[key], previous[key], key)
        self.assertEqual(state_manager.validate_state(clean), clean)

    def test_current_revision_preserves_enabled_failures_and_retired_pending_provenance_without_writes(self):
        state = state_manager.validate_state(self.previous_state())
        state["pending_notification"].update(
            sources=["NovelFire"], url="https://novelfire.net/book/shadow-slave/chapter-3225",
        )
        before = copy.deepcopy(state)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            original = path.read_bytes()
            with patch.object(state_manager, "atomic_write_json") as write:
                clean, first = state_manager.load_state(path)
                state_manager.save_state(clean, path)
            write.assert_not_called()
            self.assertFalse(first)
            self.assertEqual(clean, before)
            self.assertEqual(path.read_bytes(), original)

    def test_older_parser_revisions_keep_existing_reset_behavior(self):
        for revision in (None, 1, 6):
            with self.subTest(revision=revision):
                previous = self.previous_state()
                if revision is None:
                    previous.pop("public_source_failure_revision")
                else:
                    previous["public_source_failure_revision"] = revision
                clean = state_manager.validate_state(previous)
                self.assertEqual(clean["public_source_failures"], {})
                self.assertEqual(clean["pending_notification"], previous["pending_notification"])

    def test_malformed_or_unknown_counters_fail_even_during_migration(self):
        for revision in (None, 6, 7, PUBLIC_SOURCE_PARSER_REVISION):
            for name in (*OBSOLETE, "NovelPing", "Unknown", "Chikari"):
                for count in (True, False, 0, -1, 5, "1", 1.0, None, [], {}):
                    with self.subTest(revision=revision, name=name, count=count):
                        state = self.previous_state()
                        if revision is None:
                            state.pop("public_source_failure_revision")
                        else:
                            state["public_source_failure_revision"] = revision
                        state["public_source_failures"] = {name: count}
                        with self.assertRaises(state_manager.StateError):
                            state_manager.validate_state(state)
            for name in ("NovelPing", "Unknown"):
                state = self.previous_state()
                if revision is None:
                    state.pop("public_source_failure_revision")
                else:
                    state["public_source_failure_revision"] = revision
                state["public_source_failures"] = {name: 1}
                with self.assertRaises(state_manager.StateError):
                    state_manager.validate_state(state)

    def test_current_schema_rejects_valid_counts_for_obsolete_names(self):
        clean = state_manager.validate_state(self.previous_state())
        for name in (*OBSOLETE, "NovelPing", "Unknown"):
            with self.subTest(name=name):
                state = copy.deepcopy(clean)
                state["public_source_failures"][name] = 1
                with self.assertRaises(state_manager.StateError):
                    state_manager.validate_state(state)

    def test_emergency_state_and_provenance_survive(self):
        previous = self.previous_state()
        previous.update(mode="watch_webnovel", target_chapter=None, target_title=None, target_url=None,
                        webnovel_failures=4, webnovel_failure_cycle=100, emergency_last_check_ok=True,
                        latest_webnovel=3224)
        previous["pending_notification"].update(sources=["Telegram"], url="https://t.me/s/shadow_slave_fastes")
        previous["emergency_provenance"] = {
            "source": "Telegram", "chapter": 3225,
            "evidence_url": "https://telegra.ph/3225-Pending-10-25",
        }
        clean = state_manager.validate_state(previous)
        for key in previous.keys() - {"public_source_failures", "public_source_failure_revision"}:
            self.assertEqual(clean[key], previous[key], key)

    def test_migration_does_not_write_until_operational_change(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(self.previous_state()), encoding="utf-8")
            before = path.read_bytes()
            clean, _ = state_manager.load_state(path)
            with patch.object(state_manager, "atomic_write_json") as write:
                state_manager.save_state(clean, path)
            write.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            clean["public_source_failures"]["Chikari"] = 3
            state_manager.save_state(clean, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["public_source_failure_revision"], PUBLIC_SOURCE_PARSER_REVISION)
            self.assertEqual(saved["public_source_failures"], {"Chikari": 3, "LightNovelUp": 4})
            self.assertEqual(saved["pending_notification"], clean["pending_notification"])

    def test_monitor_startup_migration_does_not_advance_or_duplicate_pending_delivery(self):
        state = self.previous_state()
        state["pending_notification"]["next_retry_at"] = "2099-01-01T00:00:00+00:00"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            loaded, _ = state_manager.load_state(path)
            before = copy.deepcopy(loaded)
            reports = [ChapterReport("Chikari", 3224, "Baseline", PUBLIC_SITES[0].url)]
            with patch.object(monitor, "STATE_PATH", path), \
                 patch.object(monitor, "check_public_sites", return_value=reports), \
                 patch.object(monitor, "check_webnovel") as official, \
                 patch.object(monitor, "send_new_chapter") as notify, \
                 patch.object(monitor, "save_state") as save, \
                 patch.object(monitor, "write_result"):
                with self.assertRaises(SystemExit):
                    monitor.main()
            notify.assert_not_called()
            official.assert_not_called()
            save.assert_not_called()
            self.assertEqual(state_manager.load_state(path)[0], before)


if __name__ == "__main__":
    unittest.main()
