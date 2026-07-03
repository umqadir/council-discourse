# Production remote mode: GitHub Actions cron pipeline + deploy

## Context
PLAN.md = source of truth (read sections 7-11). The pipeline runs locally today; production must run fully in the cloud ("prod profile" — NO per-component local/remote mixing). A workflow skeleton may exist at .github/workflows/pipeline.yml — replace/finish it. All secrets are ALREADY set on the repo: LEGISTAR_TOKEN, MISTRAL_API_KEY, GOOGLE_API_KEY, OPENROUTER_API_KEY, R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_ENDPOINT (S3 API, bucket council-discourse-videos), ELEVENLABS_API_KEY, ASSEMBLYAI_API_KEY (unused in prod), plus you must have the site deploy via wrangler (CLOUDFLARE_API_TOKEN is NOT set — use `wrangler pages deploy` with... it's not available; instead structure deploy as a separate job and note that CLOUDFLARE_API_TOKEN secret is REQUIRED — leave a clear TODO; the supervisor will set it).

## Design (decided)
- Schedule: cron every 2h 13:00-03:00 UTC (NYC meeting hours + evening publish), plus workflow_dispatch.
- Jobs: discover (viebit RSS + legistar sync) -> per-meeting processing matrix (fetch mp4 -> remux/transcode 480p CRF32 64k mono faststart -> upload R2 via rclone/S3 -> voxtral transcribe (diarize) -> name-speakers -> chapterize) -> export-site + pages deploy. Registry state: data/registry.db committed to the repo by the workflow (single-writer, concurrency group; commit with [skip ci]).
- Prod LLM/config: whatever pipeline config defaults say (a concurrent agent may flip default to glm-5.2; read the config, don't hardcode).
- Disk: runners have ~14GB free; process ONE meeting per job (matrix fan-out, max-parallel 2), delete mp4 after remux+upload.
- Timeouts: meeting job 120 min. Failure of one meeting must not block others; failed meetings stay pending in registry for retry next run.

## Task
1. Implement the workflow + any pipeline CLI glue needed (e.g. `discover --emit-pending-json` for the matrix, `process-one --meeting-key`). Idempotency: re-running any stage on a done meeting is a no-op (registry-gated).
2. Local validation: you can't run Actions, but dry-run every command path locally (which IS the same code) + `uv run pytest` green + `actionlint` if installable via uv/brew (skip if not).
3. Keep local dev profile fully working (parakeet path untouched).
4. Update PLAN.md remote-mode section. List changed files; commits allowed if .git writable, else list.

## Hard constraints
Only this repo; no browser/MCP; no Metal jobs; never print secrets. Careful: another agent is concurrently editing pipeline/speakers.py + transcribe.py (spelling/GLM round) — do NOT edit those two files; if your glue needs them, use existing interfaces only.

## Addendum: voxtral production wiring bug (found 2026-07-02 21:15)
When run as production backend, voxtral transcribe writes utterances-voxtral*.jsonl
but stages.name_speakers requires utterances-labeled.jsonl -> pipeline breaks
end-to-end. Fix properly: voxtral as production backend writes canonical
utterances.jsonl + utterances-labeled.jsonl (+ transcribe-meta.json); keep the
-voxtral- names only for benchmark/eval mode (flag or dir-based). Add an
end-to-end test. Also: Mistral returned 429 service_tier_capacity_exceeded on a
5.9h meeting (~80 burst chunk requests) — add exponential backoff + inter-chunk
delay + resume-partial to the voxtral splitter.
Acceptance meeting for the 429/backoff fix: NYCC-PV-CH-CHA_260528-100627 (5.9h,
needs full transcribe->name->chapterize; expect off-peak capacity to succeed).
