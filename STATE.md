# STATE — current build status

Snapshot at model handoff. Supervising context switched **Fable 5 → Opus 4.8** at
commit `2e5da13` (2026-07-03), mid supervised-run hardening. Nothing was lost; this
file records exactly where things stand so work continues cleanly.

## Where we are
Production pipeline and site are built, benchmarked, and deployed. The remaining
task is getting one **green end-to-end GitHub Actions run** (`production.yml`,
`workflow_dispatch`), after which the cron owns the pipeline and the only open item
is the domain.

### Live
- Site: https://council-discourse.pages.dev — 16 meetings, list-as-homepage,
  edge-rendered chapters, R2 video with seek, OG cards, footer disclosure.
- Repo: `umqadir/council-discourse` (PUBLIC — unlimited free Actions).
- All secrets set on repo: LEGISTAR_TOKEN, MISTRAL_API_KEY, GOOGLE_API_KEY,
  OPENROUTER_API_KEY, R2_* (S3), ELEVENLABS/ASSEMBLYAI (unused in prod),
  CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID; var VIDEO_BASE_URL.

### Production config (final, benchmarked)
- ASR + diarization: Mistral Voxtral `voxtral-mini-2602`, batch API.
- Speaker naming: DeepSeek V4 Pro via OpenRouter. Chaptering + summaries: GLM-5.2
  via OpenRouter. Split saves 42% on the LLM line; both clear quality gates.
  Config: per-stage resolvers in `pipeline/config.py`
  (`naming_llm_config` / `chaptering_llm_config`; env overrides
  `COUNCIL_NAMING_LLM_*` / `COUNCIL_CHAPTER_LLM_*`).
- Verification: Gemini 3.1 Flash-Lite (Google Search grounding).
- Video: 480p CRF32 H.264, faststart, on R2. Steady-state ~$21/mo total.

## Supervised-run history (each failure = a real hardening gap, all fixed)
1. Workflow YAML heredoc indentation → triggers never registered. Fixed `1bd0890`.
2. Dead InSite detail page (410 Gone) aborted discovery. Fixed `c2df4d2`.
3. Malformed event date crashed sync. Per-event boundary `b76d263`.
4. History sweep (13,781 events) → arg-list-too-long. Forward-only coverage floor
   + matrix-via-file + per-run cap `9ddd43f`. **First run to reach process jobs:
   6 meetings processed end-to-end in CI successfully.**
5. pnpm/corepack version pin mismatch. Run pnpm inside `site/` `3412ca7`.
6. Discovery httpx ConnectTimeout. Retry-with-backoff `46d2b9b`.
7. Build failed: `no completed meetings found to export` — prebuild hook re-ran
   export in the site build step (which has no registry). Guarded behind
   SKIP_EXPORT + absolute registry path `d53135e`. **Run 28667740163 in flight.**

8. Run 28667740163 went **all-green** (discover, 6 process, export, deploy) — but
   published nothing: `merged_results=0`. `upload-artifact@v4` flattened each
   result to bare `<key>.json`; the download extracted to `.` while `merge-results`
   globbed `data/processed-results/*.json` → zero matches, so the 6 processed
   meetings' status never merged and the committed registry regressed to all-stubbed.
   Fix `2094835`: merge-results uses `rglob` (robust to any flattening) + download
   targets `data/processed-results`. Validation run **28674934125** in flight.

Lesson: a mechanically-green run is not a working run — verify it PUBLISHES new
content, not just that jobs exit 0.

Pattern: pipeline through **process → R2 sync** is proven green in CI. The
merge→export→commit→deploy tail is now fixed and under validation.

### Known: CI-processed meetings not yet persisted
The export-site job commits the registry only at its END. Every failed run so far
died before that step, so the 6 meetings processed in CI runs 4 and 6 were
discarded (local registry still shows 14 chapterized). This self-resolves on the
first fully-green run.

## Immediate next step
Watch run **28667740163** (`/tmp/supervised8.log`, SUP7_PASS/FAIL). If it fails,
read `--log-failed`, fix the next gap, re-dispatch. If it passes: cron is live,
verify the deploy reflects new meetings, then the pipeline is autonomous.

## Open (user decisions)
- Domain name (only blocker to public launch).
- Codex robustness round (queued, non-blocking): chapter-slug immutability so
  reprocessing never breaks published URLs; JSON-repair fallback for mid-gen LLM
  breaks; per-stage cost logging into the registry (GPT Pro's invoice-audit rec).
