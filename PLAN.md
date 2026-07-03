# council-discourse — Architecture Plan

A replacement for citymeetings.nyc: NYC Council meeting videos, transcribed with named
speakers, divided into chapters with titles and summaries, published same/next day.
Target: feature and performance parity with the original, then cost-optimized.

## Decisions at a glance

| Area | Decision | Why |
|---|---|---|
| Discovery | Legistar Web API (token) + viebit RSS; InSite video-link decode as join | Verified working; RSS gives ~1-2h post-meeting latency |
| Video serving | Re-host on Cloudflare R2: faststart remux (`-c copy`) at ingest, progressive MP4 via R2 public bucket | ~$15/mo at 500-meeting scale (zero egress); direct viebit playback ruled out (see 6) |
| ASR + diarization | Voxtral (`voxtral-mini-2602`) for production ASR+diarization, with local parakeet-mlx + pyannote retained as fallback | Best benchmark speaker accuracy so far, fast enough for CI, GPU-free; local path remains useful when avoiding API spend |
| Speaker naming | glm-5.2 (OpenRouter) label→name mapping over diarized-label evidence, with roster + agenda context, deterministic roster/Legistar spelling anchors, and a web-grounded verification/correction pass | Maps dozens of labels instead of thousands of utterances; glm-5.2 beats Gemini on both benchmarks and avoids whole-speaker block collapse (see 12) |
| Chaptering + summaries | glm-5.2 (OpenRouter) over full transcript with agenda/context; chunk-merge only if benchmark shows long-context degradation | Original's chunking machinery was a 2024-model workaround |
| LLM tier | Production naming + chaptering = z-ai/glm-5.2 via OpenRouter; Gemini 3.5 Flash one flag away (`COUNCIL_LLM_PROVIDER=gemini`) | Model matrix + spelling round: glm-5.2 wins on all quality metrics at ~equal cost (see 12) |
| Pipeline runtime | Dual mode: local CLI runs (Mac, manual/cron) AND remote cron (GitHub Actions, private repo) | Laptop isn't always on; same CLI both places |
| Data store | Artifacts in Cloudflare R2 + build-ready JSON/SQLite committed or cached | R2 = zero egress fees; site build pulls from it |
| Web app | Astro hybrid on Cloudflare Pages: static meeting/home/about pages, edge-rendered chapter detail pages from R2 JSON | Keeps deployments proportional to meetings, not chapters, while staying on the free Pages platform |
| OG images | Meeting/home OG pre-rendered; chapter OG rendered at the edge with satori + resvg-wasm | Avoids one PNG per chapter in deployments while preserving share cards |
| Accounts/identity | User's personal accounts (umqadir GitHub, personal Cloudflare/API accounts) | User decision 2026-07-02 (reversed earlier gothamizer plan) |

## 1. What parity means (from research/01)

Must-have (the product):
- Meeting list per body, reverse-chron, meeting-type tag filters, per-meeting AI summary
- Meeting page: date/time/duration, summary bullets, chapter list with type filters;
  chapter cards = type badge + headline title + summary + start ts + duration
- Chapter page: video player seeked to chapter start; Summary | Transcript tabs;
  transcript with named speakers + click-to-seek per-utterance timestamps;
  prev/next chapter; report-an-issue mailto; OG share cards
- Homepage: recent meetings tabs, press/social proof (ours will differ), newsletter signup
- About/FAQ; SEO-first pages (50%+ of original's traffic was organic search)
- Newsletter (Buttondown embed — zero backend)

Explicitly NOT needed for parity (absent in original): site search, accounts, API,
person pages, pagination. Candidates to drop/defer from original: beg-wall survey,
request-coverage Tally form (keep as mailto initially), City of Yes-style editorial hubs.

Improvements allowed (careful, additive only): cleaner visual design, meeting-page
date grouping / text filter, chapter deep-links with timestamps, maybe inline player
on meeting page. The existing UX is functional — don't regress information density,
click-to-seek, or SEO structure.

## 2. Pipeline (runs on GitHub Actions cron)

```
discover (hourly on weekdays)
  Legistar API events (EventLastModifiedUtc cursor) ─┐
  viebit RSS poll ───────────────────────────────────┼─> meeting registry (R2 JSON/SQLite)
  join: InSite Video.aspx base64 → viebit filename ──┘
process (per new video, ~30-60 min job)
  fetch MP4 + VTT from viebit CDN → extract 16k mono audio (ffmpeg)
  ASR + diarization (Voxtral, context-biased with roster/committee/agency terms)
                                                       → utterances + diarized labels
  local fallback: parakeet-mlx ASR + pyannote community-1 diarization
  speaker naming LLM label→name mapping (glm-5.2 via OpenRouter; roster + agenda + intro
    evidence) + verification pass, with deterministic roster/Legistar spelling anchors before
    grounded public-name correction (verification stays on gemini-3.1-flash-lite for search grounding)
  chaptering LLM pass (glm-5.2 via OpenRouter; full transcript + agenda/matter context)
  summaries (chapter, meeting) + meeting/chapter type labels
  QA gates (coverage %, speaker-unknown %, chapter len distribution, ts monotonicity)
  write artifacts to R2; flag low-confidence items for review
publish (on artifact change)
  export static meeting JSON + hashed per-meeting chapter JSON
  sync chapter JSON to R2 data/ prefix
  build Astro hybrid site → deploy Cloudflare Pages
backfill/upgrade (weekly)
  poll Legistar for official transcript attachments (arrive weeks later)
  → optional transcript-quality upgrade pass + eval telemetry
roster (weekly)
  Legistar bodies/officerecords/persons refresh
```

Review tooling: start with a lightweight flag queue (artifacts marked low-confidence
+ a local CLI/simple page to approve or fix). The original's human-in-the-loop was
its trust moat, but 2026 models + QA gates should push review from "every chapter"
to exceptions only. Publish-then-correct, like the original's error policy.

## 3. Experiment plan (~$10 budget, task #3)

Data: two benchmark meetings downloaded (2025-04-23 Transportation joint hearing
~4h; 2025-04-24 Stated Meeting ~2h) with viebit VTT, official Legistar transcript
PDFs (ground truth), and citymeetings' own chapters/speaker transcripts (reference).

1. **ASR bake-off**: Voxtral Transcribe V2, ElevenLabs Scribe v2, AssemblyAI U-3 Pro
   (+ Deepgram Nova-3 if free credits). Score: WER vs official transcript (normalized),
   cpWER w/ speaker attribution, DER-proxy, timestamp offset vs citymeetings seeks,
   positional degradation (start/middle/end bins). Most vendors have free credits —
   expected spend ≈ $0–3.
2. **Chaptering bake-off**: Gemini 3.5 Flash, Gemini 3.1 Flash-Lite, Haiku 4.5,
   DeepSeek V4 Flash (OpenRouter), GPT-5.5 anchor (OpenRouter). Compare against
   citymeetings' 110/63 chapters: boundary alignment, title/summary quality
   (LLM-judge + my read), type labels. Single-pass vs chunk-merge on the 4h meeting.
3. **Speaker naming**: roster CSV + transcript → names; score vs citymeetings speaker
   names on sampled chapters (they human-verified those).
Keys needed: Mistral, ElevenLabs, AssemblyAI, OpenRouter (~$10 credit). Gemini key
already works.

## 4. Cost model (to refine after benchmark)

Steady state (~40 meetings/mo, ~100 video-hours):
- ASR: $18–22/mo (Voxtral/Scribe rates) — or ~$2/mo self-hosted on RunPod if quality holds
- LLM passes: $1–15/mo (cheap tier) to ~$50/mo (frontier)
- Hosting: $0 (GH Actions public repo + CF Pages free tier + R2 ~$1/mo)
- Domain: ~$12/yr
→ **~$25–75/month** vs original's ~$200–400/mo equivalent (Deepgram + GPT-4-Turbo at $5–10/meeting + Mux).

Backfill (optional, decide later): citymeetings-era archive ≈ 480 meetings ≈ 1,200h:
ASR $220–260, LLM $15–150, one-time.

## 5. Open questions being resolved by experiments
- Which ASR wins on far-field council audio; whether diarization holds for 3h+ / 30+ speakers
- Whether cheap-tier LLMs chapter a 4h meeting in one pass without degradation
- Speaker-naming accuracy without web-search verification loop (add if needed)

## 6. Experiment findings so far (2026-07-01)
- Gemini audio-native transcription (3.1-flash-lite probe on real hearing audio):
  timestamps drift (median +44s mid-meeting, p10–p90 spread >100s) and long stretches
  collapse into few utterances. CONFIRMS: audio-LLMs are not the transcriber;
  timestamps come from a dedicated ASR layer.
- Single-pass full-transcript chaptering works: Gemini 3.5 Flash on a 4h/131k-token
  hearing → 79 chapters, 66s, ~$0.25; 3.1 Flash-Lite → 67 chapters, 14s, ~$0.05.
  Granularity responds well to prompt anchoring (citymeetings reference: 110).
- Viebit CDN: direct MP4 playback viable (accept-ranges: bytes; plain <video> needs
  no CORS). VTT captions same-day; official transcript PDFs confirmed for both
  benchmark meetings.
- Chapter quality vs citymeetings human-reviewed reference (boundary F1 @30s,
  CC-caption input): 3.5-flash 73% (hearing) / 76% (stated); 3.1-flash-lite 59% / 52%.
  DECIDED: Gemini 3.5 Flash for chaptering (~$0.25/meeting). Remaining gaps:
  Q&A round splitting granularity + names (ASR input will fix names).
- Clock parity: citymeetings seek offsets match viebit video time (median 3.8s,
  29/29 sampled) — direct viebit serving preserves click-to-seek exactly.

## 7. Video serving decision (2026-07-02, supersedes earlier "direct viebit" line in 6)
Direct viebit playback fails in practice despite open MP4 access:
- Viebit MP4s are NOT faststart (ftyp/free/1.2GB mdat; moov at tail) — browser
  stalls at readyState 0 for a long time before first frame; chapter-seek UX unusable.
- Viebit's own player uses on-the-fly HLS at /otfpvv/ with SIGNED URLs (~12h expiry)
  AND bot-gating (curl → 418). Hotlinking that would mean adversarially spoofing
  city infrastructure — rejected.
DECISION: re-host video on Cloudflare R2 (personal account), remuxing to faststart
at ingest (`ffmpeg -c copy -movflags +faststart`, no re-encode; ~seconds per meeting).
Progressive MP4 + range requests gives instant seek. Storage ~2GB/meeting → $15/mo
at full scale; optional later: 720p re-encode to halve it. Pipeline artifact:
video-web.mp4. Site takes a per-meeting video URL (R2 in prod; local file in dev).
Needs (morning): wrangler OAuth grant or R2 API token on the personal Cloudflare account.

## 8. ASR backend decision (2026-07-02)
Head-to-head on benchmark meetings vs citymeetings' human-reviewed speaker names
(same-person accuracy): Voxtral (voxtral-mini-2602, diarize=true) 87.6% transportation /
95.9% stated; local parakeet-mlx + pyannote community-1 84.0% transportation.
Voxtral also ~8x faster wall-clock, ~$0.30-0.70/meeting, GPU-free (works in CI).
DECISION: Voxtral = production ASR+diarization; local path retained as free fallback.
Next quality levers: roster context-biasing at transcription time (Voxtral supports
100 bias terms), verification-pass spelling anchoring (60/307 misses were
correct-person-wrong-spelling). Mistral SDK 2.5.1 exposes the transcription bias
field as `context_bias: Optional[List[str]]` (multipart) on `AudioTranscriptionRequest`
(confirmed by inspecting the installed SDK's typed model). The live API validates each
item as comma-separated with no whitespace, so the pipeline sends hyphen-joined
roster/committee/agency items and records the param, count, and sources in ASR metadata.

## 9. Open-ASR ceiling result (2026-07-02, closes the self-hosting question)
whisper-large-v3 (best open long-form ASR) + pyannote community-1 on the transportation
benchmark: 81.5% same-person (vs Voxtral 87.6%), 56 min wall (vs 5 min), though better
name spelling (75.8% vs 70.4% strict). Self-hosted/GPU ASR is rejected on quality;
Voxtral confirmed as prod ASR. Local dev profile stays parakeet (speed). Whisper's
spelling edge supports the roster-anchored spelling-correction round for Voxtral.

## 10. ASR batch pricing (2026-07-02)
Mistral Batch API explicitly supports /v1/audio/transcriptions at 50% off ->
Voxtral ASR line ~$9/mo instead of $18 at 40 meetings/mo. Async turnaround
(unquantified in docs) — verify latency empirically before making batch the
prod default; keep sync path for time-sensitive runs.

## 11. Video transcode decision (2026-07-02)
Hosted copies transcode to 480p H.264 CRF 32, 64k mono AAC, faststart
(measured: -89% vs source copy; chyron text legible, faces fine — verified frame).
Storage line: ~$2-3/mo year-end instead of ~$17. Caveat: transcoding a 3h meeting
costs ~30-60 CPU-min; at 40 meetings/mo this may exceed GH Actions' 2,000 free
private-repo minutes -> either make repo public (free unlimited Actions) or
transcode locally. Flag repo-visibility decision to user.

## 12. Spelling round + GLM-5.2 production LLM (2026-07-02)
Spelling-quality round on the Voxtral benchmarks (roster context-biasing was already wired;
verification-pass now snaps council/Legistar-known names to canonical spelling before the
web-grounded public-name correction). Naming reruns on the existing Voxtral ASR, scored vs
citymeetings human-reviewed names (same-person / strict spelling):

| benchmark | Gemini 3.5 Flash | glm-5.2 (OpenRouter) | brief baseline |
|---|---|---|---|
| transportation | 87.9% / 70.7% | 87.9% / 70.7% | 87.6% / 70.4% |
| stated | 77.0% / 54.1% (see note) | 97.3% / 74.3% | 95.9% / 73.0% |

Both models run the same anchoring + verification. Deterministic roster/Legistar spelling
anchors are correct (unit-verified: "Julie Menon"->"Julie Menin", "Amanda Farrias"->"Amanda
Farias") but fire zero times on these two meetings: the label-mapping prompt already carries
the roster CSV, so council members come out spelled correctly, and the residual strict misses
are public-witness nickname/formal variants ("Rob" vs "Robert Bookman", "Sara" vs "Sarah Lind")
that are not roster names and are genuinely same-person. So anchoring is a safety net for the
rare mis-spelled roster name, not a lever on the current benchmark residual.

Note on Gemini stated 77.0%: a single label→name error collapsed a 15-utterance block
(Hanif labelled as Krishnan). This is Gemini 3.5 Flash run-to-run variance on the mapping;
the earlier 95.9% baseline was a cleaner draw. glm-5.2 on the identical ASR produced no such
collapse (only two isolated UNKNOWN misses), which is the steadiness the model matrix predicted.

DECISION: production naming AND chaptering default = z-ai/glm-5.2 via OpenRouter. glm-5.2 with
the full verification pass meets or beats Gemini on same-person on both benchmarks (87.9 vs 87.6;
97.3 vs 95.9) and on strict spelling (70.7 vs 70.4; 74.3 vs 73.0), and is markedly steadier on
the label→name mapping. Config-driven in pipeline/config.py (`naming_llm_config()`); Gemini is
one flag away (`COUNCIL_LLM_PROVIDER=gemini`, or a bare `--model gemini-3.5-flash`). Verification
stays on gemini-3.1-flash-lite because it needs Google Search grounding for public-name spelling.

Operational notes: OpenRouter is now a production dependency for the two LLM passes (Voxtral ASR
already depends on Mistral; hosting still has no other external runtime). glm-5.2 structured output
via OpenRouter's `response_format: json_schema` was reliable — all four naming chunks across both
benchmarks parsed on the first attempt with no fallback to the tool-call path and no JSON repair.
OpenRouter returned no rate-limit throttling on these serial runs; per-call latency ran higher than
Gemini (~a few extra seconds/call from glm-5.2 reasoning), acceptable for the batch pipeline. Cost:
naming ~$0.24-0.25/meeting (OpenRouter model-pricing estimate; the per-generation exact-cost lookup
intermittently returned nothing and the code falls back to the pricing estimate).

## 13. Remote production mode (2026-07-02)
GitHub Actions is the production runner. Schedule: every 2 hours at 13:00-03:00 UTC
(NYC meeting day through evening publish), plus manual `workflow_dispatch`. The workflow is
single-run serialized with a concurrency group; per-meeting processing is a matrix with
`max-parallel: 2`, one meeting per job, 120 minute timeout.

Job shape:
- `discover`: poll Viebit RSS + Legistar, update `data/registry.db`, emit a pending-meeting
  JSON matrix, and upload the registry as an artifact.
- `process`: for each meeting key, run the prod profile only: fetch source MP4/captions/agenda,
  transcode `video-web.mp4` to 480p H.264 CRF 32 + 64k mono AAC + faststart, upload to R2
  bucket `council-discourse-videos` through rclone/S3, delete source MP4, run Voxtral ASR with
  diarization, name speakers, and chapterize. Failed meetings write `last_error` but keep their
  pending stage state so the next cron retries them.
- `export-site`: single writer. Download per-meeting result artifacts, merge registry rows,
  checkpoint SQLite WAL state, export Astro JSON plus upload-ready chapter JSON under
  `site/r2-data/data/`, sync that `data/` prefix to R2, build the site, and commit
  `data/registry.db` + `site/src/data/meetings` with `[skip ci]`.
- `deploy`: separate Cloudflare Pages deploy job using `wrangler pages deploy site/dist
  --project-name council-discourse --config site/wrangler.toml`. TODO: repository secret
  `CLOUDFLARE_API_TOKEN` is required; the job warns and skips deploy until the supervisor sets it.

Prod ASR writes canonical downstream files (`utterances.jsonl`, `utterances-labeled.jsonl`,
`transcribe-meta.json`). `utterances-voxtral*.jsonl` remains the benchmark/eval convention under
`data/benchmark`. Long production Voxtral runs use fixed-duration chunks with persisted raw part
JSON, exponential backoff on 429/5xx, `Retry-After` honoring, inter-chunk delay, and partial-result
resume. The acceptance retry case is NYCC-PV-CH-CHA_260528-100627 (5.9h).

## 12. Backfill decision (2026-07-03, user)
NO historical backfill. Coverage is forward-only from July 2026 (plus the two 2025
benchmark meetings and the June 2026 pilot/tranche set already processed). Kills the
one-time backfill spend; storage grows only with ongoing coverage.

## 13. LLM cost round results (2026-07-03, experiments/out/llm-cost-round.md)
- GPT Pro's caching thesis disproven for our architecture: naming sends per-label
  evidence windows (no transcript), chaptering already emits summaries in one call —
  no shared prefix to cache (0 cached tokens observed across 8 calls).
- Combined single pass: fails quality gates AND costs more. Dead.
- PRODUCTION SPLIT ADOPTED: naming = DeepSeek V4 Pro (ties GLM gates at ~1/3 cost),
  chaptering/summaries = GLM-5.2 (V4 Pro fails chaptering gates). $0.221/meeting
  measured (~$8.8/mo at 40 meetings; 42% below GLM-only). Config: per-stage
  resolvers in pipeline/config.py w/ COUNCIL_NAMING_LLM_* / COUNCIL_CHAPTER_LLM_* envs.
- Open lever if a hard ceiling ever matters: Z.AI-direct GLM pricing/caching.
Steady-state monthly at 40 meetings: ASR $9 + LLM $8.8 + verification ~$1 + R2 $2-3
= ~$21/mo (~$0.53/meeting marginal).
