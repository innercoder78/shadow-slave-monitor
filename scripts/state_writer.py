#!/usr/bin/env python3
"""Validate and persist permitted repository state files."""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path

ALLOWED = {"state.json", "watchdog_state.json"}

def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

def validate_json_object(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")

def changed_paths() -> set[str]:
    out = run(["git", "status", "--porcelain=v1"], check=True).stdout.splitlines()
    paths: set[str] = set()
    for line in out:
        if not line: continue
        path = line[3:]
        if " -> " in path: path = path.split(" -> ",1)[1]
        paths.add(path)
    return paths

def abort_rebase() -> None:
    git_dir = Path(run(["git", "rev-parse", "--git-dir"]).stdout.strip())
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        run(["git", "rebase", "--abort"], check=False)

def main() -> None:
    requested = [p for p in sys.argv[1:] if p]
    if not requested:
        print("No state files requested."); return
    for item in requested:
        if item not in ALLOWED: raise SystemExit(f"unexpected state file requested: {item}")
        path = Path(item)
        if not path.exists(): raise SystemExit(f"requested state file is missing: {item}")
        validate_json_object(path)
    paths = changed_paths()
    unexpected = paths - set(requested)
    if unexpected: raise SystemExit(f"unexpected changed or untracked paths: {sorted(unexpected)}")
    if not paths:
        print("No state changes to commit."); return
    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run(["git", "add", *requested])
    if run(["git", "diff", "--cached", "--quiet", "--", *requested], check=False).returncode == 0:
        print("Permitted state files unchanged."); return
    run(["git", "commit", "-m", "Update monitor state"])
    for attempt in range(1, 4):
        abort_rebase()
        pull = run(["git", "pull", "--rebase", "origin", "main"], check=False)
        if pull.returncode == 0:
            push = run(["git", "push", "origin", "HEAD:main"], check=False)
            if push.returncode == 0:
                print("State commit pushed."); return
            print(push.stdout)
        else:
            print(pull.stdout)
        abort_rebase()
        time.sleep(5 * attempt)
    raise SystemExit("Failed to push permitted state files after 3 attempts")

if __name__ == "__main__":
    main()
