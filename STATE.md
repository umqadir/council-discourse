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
- Auto-fixer (`auto-fix.yml`): on any production-run failure, launches Claude
  Opus (Anthropic's `claude-code-action`, their CI-failure pattern) with the
  failed logs + this runbook. It either pushes a tested root-cause fix, or —
  when the cause is credits/outage/data — opens an issue and changes nothing;
  a silent-error guard fails the job if the agent errors without working.
  Auth: `CLAUDE_CODE_OAUTH_TOKEN` secret = a subscription token from
  `claude setup-token` (zero marginal cost; **expires ~2027-07**, regenerate
  then). When regenerating, the ONE thing that bites: the CLI prints the token
  wrapped across terminal lines, so a copy/scrape easily drops a character and
  the result 401s. Always verify the token returns HTTP 200 via a direct curl
  (`api.anthropic.com/v1/messages`, header `anthropic-beta: oauth-2025-04-20`)
  BEFORE putting it in the secret. Separately: don't map an `ANTHROPIC_API_KEY`
  into this workflow's `env` — the action falls back to `env.ANTHROPIC_API_KEY`
  and it would override the OAuth token. (A repo secret alone is harmless; only
  an env mapping matters. The workflow currently maps only `GH_TOKEN`.)

## Billing access (verified 2026-07-13)

Never source `.env` in a shell; load it with `python-dotenv`, Wrangler, or the
pipeline. A credential value can contain shell metacharacters. Credential names
below are names only; secrets remain in `.env`, the GitHub CLI keyring, or the
Google Cloud CLI credential store.

| Provider | Verified programmatic access and visible fields | Dashboard-only / recovery |
|---|---|---|
| GitHub | Credential: `gh` user `umqadir`, now with `user` scope. `gh api -H 'X-GitHub-Api-Version: 2026-03-10' '/users/umqadir/settings/billing/usage?year=YYYY&month=M'` returns per-day `actions` quantity, SKU, repository, gross, discount, and net amounts. July: 6,608.94 runner-minutes equivalent, $36.26 gross, **$0 net**. Runs and artifacts: `gh api 'repos/umqadir/council-discourse/actions/runs?per_page=1'` and `gh api 'repos/umqadir/council-discourse/actions/artifacts?per_page=100'`; verification saw 98 active artifacts / 5.67 GB in that artifact page. | Payment method is not exposed by these REST endpoints. If billing returns 404, run `gh auth switch --user umqadir` then `gh auth refresh -h github.com -s user`. If Actions halts, inspect the billing usage response and failed run, then dispatch `gh workflow run production.yml` after the account/quota issue is cleared. |
| Cloudflare Pages/R2 | Credentials: `CLOUDFLARE_API_TOKEN` (Pages), `CLOUDFLARE_BILLING_API_TOKEN` (Billing Read), and `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_ENDPOINT` (S3 read). `GET /client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/pages/projects`, `/subscriptions`, `/paygo-usage`, and deprecated `/billing/profile` all return 200. The account has active `R2 Paid`, $0 fixed price plus usage, renewing 2026-08-02; `/paygo-usage` exposes daily R2 quantity and cumulative contracted cost ($0 in the current cycle). An environment-configured `rclone size r2:council-discourse-videos --json` returned 175 objects / 9.36 GB. | V2 `/billable/usage` is officially Alpha/Restricted and returns permission 1171 even with Billing Read. Use Billing > Billable usage for projected cost/free-tier detail; current cycle is $0, 3.26 GB-months, 2.43k Class A and 1.08k Class B operations. Invoices and card are dashboard-only (latest invoice $50 paid; primary card ending 0628, expires 10/2027). If R2 is suspended, pay the outstanding invoice or update the primary method under Manage Account > Billing > Subscriptions; Cloudflare restores service after payment validation. |
| OpenRouter | Credential: `OPENROUTER_API_KEY`. `GET https://openrouter.ai/api/v1/credits` returns total purchased and used; `GET /api/v1/key` returns key limit/reset, remaining limit, and daily/weekly/monthly/all-time usage. Verified: $40 purchased, $24.1241 used, $15.8759 balance; key cap $60 resetting monthly with $35.8759 remaining. | Auto-top-up/payment method are not in the documented account APIs. Dashboard verification found auto-top-up **enabled**: buy $25 below $2, primary Visa ending 0628. If requests return 402 or preflight reports under $4, add credits at `/settings/credits` (or confirm the automatic top-up succeeded), then dispatch production once. |
| Mistral / Voxtral | Credential: `MISTRAL_API_KEY`. `GET https://api.mistral.ai/v1/models` returns 200 and proves production auth. The documented billing endpoints are `GET https://console.mistral.ai/api/admin/{usage,spend-limit,rate-limit}`, but they require a dedicated Admin API key and the Admin API is Preview on Enterprise plans; this non-Enterprise organization cannot create one, and the standard key correctly returns 401. | Admin > Billing is the exact fallback. Verified current API usage $50.34, organization cap $100, credit balance $0, two paid invoices, and default card ending 0628. If halted at the cap, raise the monthly limit there or wait for reset; if payment fails, update the card/pay the invoice. Recheck `/v1/models`, then dispatch production. |
| Google / Gemini | Credentials: `GOOGLE_API_KEY` plus the `gcloud` user credential for `uzairq93@gmail.com`. The key is a **standard**, Generative Language-restricted key in project `gen-lang-client-0826070711`, linked to open billing account `0195E4-EC8095-4AEEE3`. `gcloud billing projects describe gen-lang-client-0826070711`, `gcloud billing accounts list`, and `gcloud billing budgets list --billing-account=0195E4-EC8095-4AEEE3` work; the account has a $100 monthly alert budget (50/90/100/150%). Cloud Monitoring is enabled: query `generativelanguage.googleapis.com/generate_content_usage_output_token_count` and `serviceruntime.googleapis.com/api/request_count` via `GET https://monitoring.googleapis.com/v3/projects/gen-lang-client-0826070711/timeSeries`; July verification returned 2.306M output tokens and 3,503 requests. | There is no Billing API current-cost method and no BigQuery billing export is configured, so use AI Studio `/spend` and `/billing`: $22.63 Gemini cost through Jul 13, postpay balance due $24.53, Tier 1/$250 account cap, no project spend cap, primary card ending 0628. Do not confuse the closed `My Billing Account` with the open `My Maps Billing Account`. On payment suspension, settle/update the open account, verify the models endpoint, then dispatch. The standard key must be migrated to an authorization key before Google rejects standard keys in September 2026. |
| ElevenLabs (optional) | Credential: `ELEVENLABS_BILLING_API_KEY`, a restricted key with only User Read. `GET https://api.elevenlabs.io/v1/user/subscription` exposes tier/status, credits used/limit, overage permission/current overage, open invoices, next invoice, and reset. Verified free tier: 6,428 / 10,000 credits, overage disabled, $0 overage, no open invoice. | Payment method is dashboard-only. If the optional backend is re-enabled and quota is exhausted, upgrade/add prepaid usage in Billing or switch production back to Voxtral; do not broaden this audit key. |
| AssemblyAI (optional) | Credential: `ASSEMBLYAI_API_KEY`; `GET https://api.assemblyai.com/v2/transcript?limit=1` returns 200 and proves auth. AssemblyAI documents usage/spend and balance in the dashboard but no account billing/balance API. | Dashboard fallback: Free plan, $46.91 credits remaining on verification; billing method/auto-pay are dashboard-only. If balance reaches zero, add credits or enable auto-pay before selecting this optional backend. |

For the Google Monitoring query, use an OAuth token from `gcloud auth
print-access-token`, a start/end interval, and `view=FULL`. For Cloudflare S3
statistics without writing an rclone config, set `RCLONE_CONFIG_R2_TYPE=s3`,
`RCLONE_CONFIG_R2_PROVIDER=Cloudflare`, and the three credential-backed
`RCLONE_CONFIG_R2_*` variables in the process environment before `rclone size`.

## Returning after a break — checklist

1. Check the watchdog issue list and the Actions page for red runs.
2. If the cron halted (breaker) it is almost always credits:
   - OpenRouter: query `/api/v1/credits` first; preflight needs >= $4. The
     dashboard currently has a $25 automatic top-up below $2.
   - Mistral: `/v1/models` proves auth, but spend requires Admin > Billing;
     the current organization limit is $100.
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
