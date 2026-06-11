from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("state_writer", ROOT / "scripts" / "state_writer.py")
assert SPEC is not None and SPEC.loader is not None
state_writer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state_writer)


@contextmanager
def chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class StateWriterValidationTests(unittest.TestCase):
    def write_metadata(self, artifact_dir: Path, state_file: str = "state/state.json", *, base_sha256: str | None = None) -> None:
        artifact_type = "monitor" if state_file == "state/state.json" else "watchdog"
        state_name = "state.json" if state_file == "state/state.json" else "watchdog_state.json"
        if base_sha256 is None:
            base_sha256 = "0" * 64
        (artifact_dir / "state_artifact_metadata.json").write_text(
            json.dumps(
                {
                    "artifact_type": artifact_type,
                    "state_file": state_file,
                    "base_sha256": base_sha256,
                    "checkout_sha": "a" * 40,
                    "workflow_run_id": "1",
                    "workflow_run_attempt": "1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertIn(state_name, {"state.json", "watchdog_state.json"})

    def write_monitor_result(self, artifact_dir: Path) -> None:
        (artifact_dir / "run_result.json").write_text(
            json.dumps({"result": "failed", "reasons": ["target chapter not found yet"], "degraded_reasons": []}) + "\n",
            encoding="utf-8",
        )

    def write_watchdog_result(self, artifact_dir: Path) -> None:
        (artifact_dir / "watchdog_result.json").write_text(
            json.dumps({"changed": False, "status": "fresh"}) + "\n",
            encoding="utf-8",
        )

    def write_valid_state(self, artifact_dir: Path) -> None:
        (artifact_dir / "state.json").write_text((ROOT / "state" / "state.json").read_text(encoding="utf-8"), encoding="utf-8")

    def test_rejects_invalid_monitor_result_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            self.write_metadata(artifact_dir)
            self.write_valid_state(artifact_dir)
            (artifact_dir / "run_result.json").write_text(
                json.dumps({"result": "unknown", "reasons": [], "degraded_reasons": []}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                state_writer.validate_artifact_boundary(artifact_dir, "state/state.json")

    def test_rejects_invalid_state_file_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            self.write_metadata(artifact_dir)
            self.write_monitor_result(artifact_dir)
            invalid_state = json.loads((ROOT / "state" / "state.json").read_text(encoding="utf-8"))
            invalid_state["latest_seen"] = -1
            (artifact_dir / "state.json").write_text(json.dumps(invalid_state) + "\n", encoding="utf-8")

            with self.assertRaises(SystemExit):
                state_writer.validate_artifact_boundary(artifact_dir, "state/state.json")

    def test_rejects_unexpected_artifact_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            self.write_metadata(artifact_dir)
            self.write_monitor_result(artifact_dir)
            self.write_valid_state(artifact_dir)
            (artifact_dir / "unexpected.txt").write_text("nope\n", encoding="utf-8")

            with self.assertRaises(SystemExit):
                state_writer.validate_artifact_boundary(artifact_dir, "state/state.json")


class StateWriterPersistenceTests(unittest.TestCase):
    def run_git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def initialize_repo(self, repo: Path) -> None:
        (repo / "state").mkdir()
        (repo / "state" / "state.json").write_text((ROOT / "state" / "state.json").read_text(encoding="utf-8"), encoding="utf-8")
        (repo / "state" / "watchdog_state.json").write_text((ROOT / "state" / "watchdog_state.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.run_git(repo, "init", "-b", "main")
        self.run_git(repo, "config", "user.name", "Test")
        self.run_git(repo, "config", "user.email", "test@example.invalid")
        self.run_git(repo, "add", "state/state.json", "state/watchdog_state.json")
        self.run_git(repo, "commit", "-m", "initial state")

    def write_artifact(self, repo: Path, artifact_dir: Path, state_file: str, state_data: dict, base_sha256: str) -> None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_type = "monitor" if state_file == "state/state.json" else "watchdog"
        state_name = "state.json" if state_file == "state/state.json" else "watchdog_state.json"
        result_name = "run_result.json" if state_file == "state/state.json" else "watchdog_result.json"
        self.write_json(artifact_dir / state_name, state_data)
        self.write_json(
            artifact_dir / "state_artifact_metadata.json",
            {
                "artifact_type": artifact_type,
                "state_file": state_file,
                "base_sha256": base_sha256,
                "checkout_sha": "b" * 40,
                "workflow_run_id": "123",
                "workflow_run_attempt": "1",
            },
        )
        if result_name == "run_result.json":
            self.write_json(artifact_dir / result_name, {"result": "failed", "reasons": ["not found yet"], "degraded_reasons": []})
        else:
            self.write_json(artifact_dir / result_name, {"changed": False, "status": "fresh"})

    def persist(self, artifact_dir: Path, state_file: str, push_calls: list[list[str]]) -> None:
        original_run = state_writer.run

        def fake_run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
            if cmd[:3] == ["git", "push", "origin"]:
                push_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "pushed\n")
            return original_run(cmd, check=check)

        with patch.object(state_writer, "fetch_and_reset_main", return_value=None), patch.object(state_writer, "run", side_effect=fake_run):
            state_writer.persist_with_retries(artifact_dir, state_file)

    def test_applicable_monitor_artifact_identical_to_current_state_exits_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.initialize_repo(repo)
            state_file = "state/state.json"
            state_data = json.loads((repo / state_file).read_text(encoding="utf-8"))
            artifact_dir = Path(tmp) / "artifact"
            self.write_artifact(repo, artifact_dir, state_file, state_data, self.sha256(repo / state_file))
            push_calls: list[list[str]] = []

            with chdir(repo):
                self.persist(artifact_dir, state_file, push_calls)

            self.assertEqual(push_calls, [])
            self.assertEqual(subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, check=True, stdout=subprocess.PIPE, text=True).stdout, "")

    def test_applicable_watchdog_artifact_identical_to_current_state_exits_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.initialize_repo(repo)
            state_file = "state/watchdog_state.json"
            state_data = json.loads((repo / state_file).read_text(encoding="utf-8"))
            artifact_dir = Path(tmp) / "artifact"
            self.write_artifact(repo, artifact_dir, state_file, state_data, self.sha256(repo / state_file))
            push_calls: list[list[str]] = []

            with chdir(repo):
                self.persist(artifact_dir, state_file, push_calls)

            self.assertEqual(push_calls, [])
            self.assertEqual(subprocess.run(["git", "status", "--porcelain=v1"], cwd=repo, check=True, stdout=subprocess.PIPE, text=True).stdout, "")

    def test_real_state_change_commits_and_reaches_push_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.initialize_repo(repo)
            state_file = "state/state.json"
            state_data = json.loads((repo / state_file).read_text(encoding="utf-8"))
            state_data["updated_at"] = "2026-06-11T12:00:00+00:00"
            artifact_dir = Path(tmp) / "artifact"
            self.write_artifact(repo, artifact_dir, state_file, state_data, self.sha256(repo / state_file))
            push_calls: list[list[str]] = []

            with chdir(repo):
                self.persist(artifact_dir, state_file, push_calls)

            self.assertEqual(push_calls, [["git", "push", "origin", "HEAD:main"]])
            self.assertEqual(json.loads((repo / state_file).read_text(encoding="utf-8"))["updated_at"], "2026-06-11T12:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
