from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


BOT_ISSUE_SCRIPT = Path(__file__).parents[1] / "scripts" / "bot-issue"


def test_bot_issue_ignores_issues_opened_by_outsiders(tmp_path: Path) -> None:
    issues = [
        {"number": 9, "author": {"login": "someone-else", "is_bot": False}},
        {"number": 7, "author": {"login": "app/github-actions", "is_bot": True}},
        {"number": 4, "author": {"login": "app/claude", "is_bot": True}},
    ]
    fixture = tmp_path / "issues.json"
    fixture.write_text(json.dumps(issues))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env bash
set -u
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--jq" ]; then jq -r "$2" "$ISSUES_FILE"; exit 0; fi
  shift
done
exit 1
"""
    )
    fake_gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "ISSUES_FILE": str(fixture),
            "REPO": "owner/repo",
        }
    )

    result = subprocess.run(
        [str(BOT_ISSUE_SCRIPT), "Pipeline needs attention"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.split() == ["7", "4"]
