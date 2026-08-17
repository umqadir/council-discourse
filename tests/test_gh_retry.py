from __future__ import annotations

import os
import subprocess
from pathlib import Path


RETRY_SCRIPT = Path(__file__).parents[1] / "scripts" / "gh-retry"


def _fake_command(tmp_path: Path, body: str) -> Path:
    command = tmp_path / "fake-command"
    command.write_text("#!/usr/bin/env bash\nset -u\n" + body)
    command.chmod(0o755)
    return command


def _run(command: Path, counter: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "COUNTER_FILE": str(counter),
            "GH_RETRY_ATTEMPTS": "4",
            "GH_RETRY_BASE_SECONDS": "0",
        }
    )
    return subprocess.run(
        [str(RETRY_SCRIPT), str(command)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_gh_retry_retries_transient_failures_until_success(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    command = _fake_command(
        tmp_path,
        """
count=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNTER_FILE"
if [ "$count" -lt 3 ]; then
  echo "HTTP 503: Service Unavailable" >&2
  exit 1
fi
echo '{"ok":true}'
""",
    )

    result = _run(command, counter)

    assert result.returncode == 0
    assert result.stdout == '{"ok":true}\n'
    assert counter.read_text().strip() == "3"
    assert result.stderr.count("retrying") == 2


def test_gh_retry_does_not_retry_permanent_failures(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    command = _fake_command(
        tmp_path,
        """
count=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
echo $((count + 1)) > "$COUNTER_FILE"
echo "HTTP 403: Forbidden" >&2
exit 1
""",
    )

    result = _run(command, counter)

    assert result.returncode == 1
    assert "HTTP 403" in result.stderr
    assert counter.read_text().strip() == "1"


def test_gh_retry_stops_after_configured_attempts(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    command = _fake_command(
        tmp_path,
        """
count=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
echo $((count + 1)) > "$COUNTER_FILE"
echo "HTTP 502: Bad Gateway" >&2
exit 7
""",
    )

    result = _run(command, counter)

    assert result.returncode == 7
    assert "still failing after 4 attempts" in result.stderr
    assert counter.read_text().strip() == "4"
