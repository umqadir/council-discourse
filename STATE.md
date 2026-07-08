# STATE — operational status and runbook

Updated 2026-07-08 (result-transport redesign). The pipeline is **autonomous**:
a GitHub Actions cron (`production.yml`, every 2 hours) discovers new NYC
Council meetings, processes them (transcription, speaker naming, chaptering),
and deploys the site. No routine human involvement is required.

## State model — keep these sentences true

- **Registry** (`data/registry.db`, committed by CI): the only record of stage
  statuses. Discover is the only row creator; merge-results the only CI writer.
- **R2 is the single artifact store**: `/<key>/video-web.mp4` (published
  video), `/artifacts/<key>/…` (stage outputs, persisted after every process
  job, restored before the next), `/artifacts/<key>/result.json` (the durable
  outcome record — written by process-one itself, even for failures), and
  `/data/…` (content-hashed chapter JSON).
- **Actions artifacts carry only intra-run plumbing** (the registry snapshot).
  Never transport results through them: their internal layout depends on which
  files a run happened to produce (least-common-ancestor rooting), which
  caused two green-but-empty incidents (2026-07-03 and 2026-07-08).
- Export-site re-syncs ALL result records from R2 every run and the merge is
  idempotent (done statuses never regress; unknown keys skipped), so a lost
  registry commit self-heals on the next cycle instead of silently re-paying.
- The `verify-run-results` step shares its file-discovery code with
  `merge_results` — one definition of "a result", checked per matrix key.

## Live

- Site: https://council-discourse.pages.dev — 36 meetings, list-as-homepage,
  hearing topics on cards and meeting pages, chapter navigation trail,
  edge-rendered chapter pages, R2 video.
- Repo: `umqadir/council-discourse` (public). Registry: `data/registry.db`,
  committed by CI at the end of each run (CI is the single writer).

## Production config (benchmarked; see PLAN.md sections 7-13)

- ASR + diarization: Mistral Voxtral `voxtral-mini-2602`, sync endpoint at
  $0.18/audio-hour (see ASR pricing note below for why not batch).
- Naming: DeepSeek V4 Pro; chaptering + summaries: GLM-5.2 (both via
  OpenRouter, prepaid credits). Verification: Gemini Flash-Lite (best-effort,
  non-fatal). Video: 480p H.264 on R2, zero egress.
- Steady state ≈ $29-32/month at the measured 41 meetings/month
  (~$0.60/meeting all-in; a meeting above ~$5 in `cost_usd` is an anomaly).
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
  selection); ci-health reports it and the run keeps a GitHub issue open until
  it is handled. Redoing any meeting — parked, or published-but-wrong — has
  exactly one sanctioned path: `pipeline reset-meeting <key>` (clears statuses,
  deletes the durable R2 result record, drops the committed page), then commit
  the registry and dispatch. Editing statuses by hand is always undone by the
  next merge — that is by design.
- Accepted risk (reviewed 2026-07-08): if a runner dies between a paid API
  call and its checkpoint reaching R2 (a window of seconds to minutes), that
  meeting re-pays the uncheckpointed portion once. Bounded and rare; inline
  per-call R2 uploads were judged not worth coupling paid stages to R2.
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

## ASR pricing note (2026-07-06)

Sync Voxtral ($0.18/audio-hr) is the enforced default. Mistral's 50%-off Batch
Jobs API is fully integrated behind `COUNCIL_VOXTRAL_MODE=batch` but its queue
currently drops `diarize` (validated live: same clip diarizes sync, returns
speaker_id=null batch, across every documented encoding — likely their bug,
worth a support ticket). A cost-regression test pins sync + the reason. At the
true price Voxtral still beats AssemblyAI (~$0.23/hr, fails a quality gate)
and ElevenLabs (~$0.22-0.28/hr, fails) — decision unchanged. If Mistral fixes
batch diarization: flip the env var, verify labels on one meeting, update the
test; ASR line halves.
