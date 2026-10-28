"""Offline checks for historical-schema validation and one-time retirement."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from shadow_slave_monitor import monitor, parsers, state_manager
from shadow_slave_monitor.config import PUBLIC_SITES, SourceConfig
from shadow_slave_monitor.models import ChapterReport
from shadow_slave_monitor.notifications import pending_from_report
from shadow_slave_monitor.state_manager import StateError, initial_state, validate_state


ROOT = Path(__file__).resolve().parents[1]
ENABLED = (
    "Chikari", "Telegram", "Novel Buddy", "ShadowSlave.Space", "FreeWebNovel",
    "SSNovel", "NovelFull", "ReadNovelFull", "FreeWebNovel.net", "Rentry",
)


class StateSchemaRetirementTests(unittest.TestCase):
    def initialized(self):
        state = initial_state()
        state.update(latest_seen=3228, latest_title="Delivered title",
                     latest_url="https://rentry.co/3228ss", latest_webnovel=3228,
                     latest_webnovel_title="Authoritative title", webnovel_success_cycle=99,
                     updated_at="2026-10-28T05:00:29+00:00")
        return state

    def inventory(self):
        inventory = {site.name: (site.url, site.enabled, site.allowed_hosts) for site in PUBLIC_SITES}
        inventory["Archived Source"] = ("https://archived.example/novel/shadow-slave/", True,
                                        ("archived.example",))
        return inventory

    def migrate(self, state):
        with patch("shadow_slave_monitor.state_manager.historical_public_inventory", return_value=self.inventory()):
            return validate_state(state)

    def test_exact_enabled_inventory_and_order(self):
        self.assertEqual(tuple(site.name for site in PUBLIC_SITES if site.enabled), ENABLED)
        self.assertEqual(len(ENABLED), 10)
        self.assertEqual(tuple(site.name for site in PUBLIC_SITES if not site.enabled),
                         ("Novel Phoenix", "NovelPing"))

    def test_unconfigured_dispatch_fails_before_network(self):
        source = SourceConfig("Archived Source", "https://archived.example/", True,
                              ("archived.example",))
        with patch.object(parsers, "fetch_html") as html, patch.object(parsers, "fetch_json") as api:
            with self.assertRaises(parsers.ParseError):
                parsers.check_public_site(source)
        html.assert_not_called()
        api.assert_not_called()

    def test_repository_state_uses_current_schema_without_changing_history(self):
        state = json.loads((ROOT / "state/state.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_positions", state)
        self.assertEqual(validate_state(state), state)

    def test_immutable_reference_migrates_previous_production_shape(self):
        raw = subprocess.check_output(
            ["git", "show", f"{state_manager.LEGACY_MONITOR_SCHEMA_REF}:state/state.json"],
            cwd=ROOT, encoding="utf-8")
        historical = json.loads(raw)
        expected = copy.deepcopy(historical)
        del expected["source_positions"]
        self.assertEqual(validate_state(historical), expected)
        self.assertEqual(json.loads(raw), historical)

    def test_unknown_or_malformed_legacy_cursors_fail_closed(self):
        for value in ([], None, {"Unknown": {}},
                      {"Archived Source": {"chapter": 3193, "url": "https://archived.example/novel/shadow-slave/chapter-3193-baseline/"}}):
            state = self.initialized()
            state["source_positions"] = value
            before = copy.deepcopy(state)
            with self.subTest(value=value), self.assertRaises(StateError):
                validate_state(state)
            self.assertEqual(state, before)

    def test_unknown_failure_identity_fails_closed_at_every_supported_revision(self):
        for revision in (6, 7, 8):
            state = self.initialized()
            state.update(public_source_failure_revision=revision,
                         public_source_failures={"Unknown": 4})
            with self.subTest(revision=revision), self.assertRaises(StateError):
                validate_state(state)

    def test_cursor_retirement_preserves_newer_state_only_update(self):
        proposed = self.initialized()
        base = copy.deepcopy(proposed)
        base["source_positions"] = {"Archived Source": {
            "chapter": 3193, "url": "https://archived.example/novel/shadow-slave/chapter-3193-baseline/"}}
        current = copy.deepcopy(base)
        current.update(latest_seen=3229, latest_title="New delivered title",
                       latest_url="https://rentry.co/3229ss", latest_webnovel=3231,
                       latest_webnovel_title="New authoritative title", mode="watch_free_sites",
                       target_chapter=3231, target_title="Current target title",
                       target_url="https://www.webnovel.com/target", webnovel_failure_cycle=100,
                       webnovel_success_cycle=101, public_source_failures={"Chikari": 3, "NovelFull": 2},
                       updated_at="2026-10-28T12:00:00+00:00")
        current["pending_notification"] = pending_from_report(
            3229, ChapterReport("Telegram", 3230, "Pending title", "https://t.me/s/shadow_slave_fastes"))
        current["pending_notification"].update(attempt_count=3, last_error_category="http_error",
                                               last_http_status=503)
        expected = copy.deepcopy(current)
        del expected["source_positions"]
        self.assertEqual(self.migrate(current), expected)

    def test_cursor_retirement_preserves_newer_emergency_and_recovery_state(self):
        proposed = self.initialized()
        base = copy.deepcopy(proposed)
        base["source_positions"] = {"Archived Source": {
            "chapter": 3193, "url": "https://archived.example/novel/shadow-slave/chapter-3193-baseline/"}}
        current = copy.deepcopy(base)
        current.update(webnovel_failures=4, webnovel_failure_cycle=100,
                       emergency_last_check_ok=True, public_source_failures={"Chikari": 4},
                       emergency_provenance={"source": "Telegram", "chapter": 3228,
                                             "evidence_url": "https://telegra.ph/3228-Synthetic"})
        for recovered in (False, True):
            if recovered:
                current.update(webnovel_failures=0, emergency_last_check_ok=None,
                               webnovel_success_cycle=100)
            expected = copy.deepcopy(current)
            del expected["source_positions"]
            with self.subTest(recovered=recovered):
                self.assertEqual(self.migrate(current), expected)

    def test_newer_historical_cursor_is_retired_without_regressing_chapters(self):
        proposed = self.initialized()
        base = copy.deepcopy(proposed)
        base["source_positions"] = {"Archived Source": {
            "chapter": 3193, "url": "https://archived.example/novel/shadow-slave/chapter-3193-baseline/"}}
        current = copy.deepcopy(base)
        current["source_positions"]["Archived Source"].update(
            chapter=3194, url="https://archived.example/novel/shadow-slave/chapter-3194-next/")
        self.assertEqual(self.migrate(current), proposed)

    def test_unchanged_monitor_run_neither_persists_nor_notifies(self):
        state = self.initialized()
        original = copy.deepcopy(state)
        with patch.object(monitor, "load_state", return_value=(state, False)), \
             patch.object(monitor, "webnovel_due", return_value=False), \
             patch.object(monitor, "save_state") as save, \
             patch.object(monitor, "send_new_chapter") as send, patch.object(monitor, "write_result"):
            monitor.main()
        save.assert_not_called()
        send.assert_not_called()
        self.assertEqual(state, original)

    def test_read_only_load_and_quiet_monitor_persist_retirement_exactly_once(self):
        state = self.initialized()
        state["source_positions"] = {"Archived Source": {
            "chapter": 3193,
            "url": "https://archived.example/novel/shadow-slave/chapter-3193-baseline/"}}
        state["public_source_failures"] = {"Archived Source": 4, "Chikari": 3}
        expected = copy.deepcopy(state)
        del expected["source_positions"]
        del expected["public_source_failures"]["Archived Source"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            original = path.read_bytes()
            real_load, real_save = state_manager.load_state, state_manager.save_state
            with patch.object(state_manager, "historical_public_inventory", return_value=self.inventory()):
                loaded, first = real_load(path)
                self.assertFalse(first)
                self.assertTrue(loaded.needs_persistence)
                self.assertEqual(loaded, expected)
                self.assertEqual(path.read_bytes(), original)
                with patch.object(monitor, "load_state", side_effect=lambda _: real_load(path)), \
                     patch.object(monitor, "save_state", side_effect=lambda value: real_save(value, path)) as save, \
                     patch.object(state_manager, "iso_now", return_value=state["updated_at"]), \
                     patch.object(monitor, "webnovel_due", return_value=False), \
                     patch.object(monitor, "send_new_chapter") as send, patch.object(monitor, "write_result"):
                    monitor.main()
                    save.assert_called_once()
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
                    monitor.main()
                    save.assert_called_once()
                    send.assert_not_called()

    def test_missing_history_fails_closed_instead_of_discarding_unknown_data(self):
        state = self.initialized()
        state["source_positions"] = {}
        with patch.object(state_manager.subprocess, "run", side_effect=FileNotFoundError), \
             self.assertRaisesRegex(StateError, "historical state schema is unavailable"):
            validate_state(state)

    def test_retired_failure_counter_requires_a_valid_count(self):
        for count in (True, 0, 5, "4", None):
            state = self.initialized()
            state["public_source_failures"] = {"Archived Source": count}
            with self.subTest(count=count), self.assertRaises(StateError):
                self.migrate(state)

    def test_contradictory_noncanonical_and_unknown_cursors_fail_closed(self):
        canonical = "https://archived.example/novel/shadow-slave/chapter-3193-baseline/"
        cases = [
            {"chapter": 3194, "url": canonical},
            {"chapter": True, "url": canonical},
            {"chapter": 3193, "url": canonical + "?query=bad"},
            {"chapter": 3193, "url": canonical.replace("https:", "http:")},
            {"chapter": 3193, "url": canonical.replace("archived.example", "evil.example")},
            {"chapter": 3193, "url": canonical.replace("archived.example", "archived.example:443")},
            {"chapter": 3193, "url": canonical.replace("baseline", "%62aseline")},
            {"chapter": 3193},
        ]
        for position in cases:
            state = self.initialized()
            state["source_positions"] = {"Archived Source": position}
            with self.subTest(position=position), self.assertRaises(StateError):
                self.migrate(state)
