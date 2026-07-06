# STATE — operational status and runbook

Updated 2026-07-06. The pipeline is **autonomous**: a GitHub Actions cron
(`production.yml`, every 2 hours) discovers new NYC Council meetings, processes
them (transcription, speaker naming, chaptering), and deploys the site. No
routine human involvement is required.

## Live

- Site: https://council-discourse.pages.dev — 36 meetings, list-as-homepage,
  hearing topics on cards and meeting pages, chapter navigation trail,
  edge-rendered chapter pages, R2 video.
- Repo: `umqadir/council-discourse` (public). Registry: `data/registry.db`,
  committed by CI at the end of each run (CI is the single writer).

## Production config (benchmarked; see PLAN.md sections 7-13)

- ASR + diarization: Mistral Voxtral `voxtral-mini-2602` ($0.09/audio-hour).
- Naming: DeepSeek V4 Pro; chaptering + summaries: GLM-5.2 (both via
  OpenRouter, prepaid credits). Verification: Gemini Flash-Lite (best-effort,
  non-fatal). Video: 480p H.264 on R2, zero egress.
- Steady state ≈ $19-22/month at the measured 41 meetings/month.
  Cost projection artifact:
  https://claude.ai/code/artifact/a47c2f79-3779-4b2b-9132-c17ef127a6d3

## Self-protection (added after the July 2026 incident + 4-lens review)

- Preflight (discover job): OpenRouter balance >= $4, Mistral auth, Google
  auth — a run fails before any spend if a provider is unusable.
- Circuit breaker: 3 consecutive failed runs halt the cron. Recovery: fix the
  cause, `gh workflow run production.yml` — one green dispatched run re-arms.
- Stage artifacts (incl. per-chunk transcription and naming checkpoints)
  persist to `r2:council-discourse-videos/artifacts/<key>` and are restored at
  job start: retries resume at the failed stage instead of re-paying.
- Dead-letter: a meeting failing 5 process attempts is parked (excluded from
  selection, listed in discover warnings). Reset: set `process_attempts=0`.
- Registry merges never regress a completed stage status; the commit-step
  rebase fallback rebuilds only registry + site data on top of origin (never
  commits the runner's stale tree).
- Per-meeting `cost_usd` recorded in the registry; `pipeline ci-health`
  prints errors/staleness into each run's step summary.
- Watchdog (`watchdog.yml`, daily 15:30 UTC): opens/updates a GitHub issue if
  no successful production run in 26 hours.

## Returning after a break — checklist

1. Check the watchdog issue list and the Actions page for red runs.
2. If the cron halted (breaker) it is almost always credits:
   - OpenRouter: https://openrouter.ai/settings/credits (keep auto-top-up OFF;
     the prepaid balance is the spend cap; preflight needs >= $4).
   - Mistral: console.mistral.ai billing (monthly limit ~$20 suggested).
3. After refilling: `gh workflow run production.yml` once; green re-arms cron.
4. `sqlite3 data/registry.db "SELECT meeting_key, process_attempts, substr(last_error,1,120) FROM meetings WHERE process_attempts >= 5;"`
   shows parked meetings; reset attempts to retry them.
5. gh CLI: two accounts on this machine; this repo needs
   `gh auth switch --user umqadir`.

## Known open items (all optional, none load-bearing)

- Chapter-slug immutability on reprocess (SEO; reprocessing is rare).
- OG images use a placeholder poster per meeting.
- Legistar token rotation (prefix once echoed to a local log, scrubbed).
- Custom domain (deliberately deferred; pages.dev is fine).
