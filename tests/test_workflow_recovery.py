from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "production.yml"


def _job_block(text: str, job_name: str, next_job_name: str | None = None) -> str:
    start = text.index(f"  {job_name}:\n")
    end = text.index(f"  {next_job_name}:\n", start) if next_job_name else len(text)
    return text[start:end]


def test_jobs_using_repository_helpers_check_out_the_repository() -> None:
    text = WORKFLOW.read_text()
    discover = _job_block(text, "discover", "process")
    export_site = _job_block(text, "export-site", "deploy")
    deploy = _job_block(text, "deploy")

    for job in (discover, export_site, deploy):
        assert "scripts/gh-retry" in job
        assert "uses: actions/checkout@v5" in job


def test_deploy_digest_uses_a_sized_file_upload() -> None:
    deploy = _job_block(WORKFLOW.read_text(), "deploy")

    assert 'rclone copyto "$digest_file"' in deploy
    assert "rclone rcat" not in deploy
