# council-discourse — Architecture Plan

A replacement for citymeetings.nyc: NYC Council meeting videos, transcribed with named
speakers, divided into chapters with titles and summaries, published same/next day.
Target: feature and performance parity with the original, then cost-optimized.

## Decisions at a glance

| Area | Decision | Why |
|---|---|---|
| Discovery | Legistar Web API (token) + viebit RSS; InSite video-link decode as join | Verified working; RSS gives ~1-2h post-meeting latency |
| Video serving | Direct viebit CDN URLs in our own player (video.js), YouTube embed fallback | $0; original paid Mux to re-host the same files |
| ASR + diarization | Benchmark: Voxtral Transcribe V2 vs ElevenLabs Scribe v2 vs AssemblyAI U-3 (+ Deepgram Nova-3 as citymeetings-baseline) | $0.18–0.69 per meeting; timestamps always from ASR, never LLM |
| Speaker naming | LLM pass over full transcript w/ member roster + agenda; verification judge pass | 2025-era approach (Sonnet 3.7 was first to pass); today's mid-tier should clear it |
| Chaptering + summaries | LLM over full transcript with agenda/context; chunk-merge only if benchmark shows long-context degradation | Original's chunking machinery was a 2024-model workaround |
| LLM tier | Benchmark Gemini 3.5 Flash / 3.1 Flash-Lite / Haiku 4.5 / DeepSeek V4 vs GPT-5.5 anchor | Cost table says even frontier is ~$1.20/meeting; pick minimum tier matching citymeetings quality |
| Pipeline runtime | GitHub Actions scheduled workflows (public repo, gothamizer) | Free, cron-native, no server to manage, gh CLI-managed |
| Data store | Artifacts in Cloudflare R2 + build-ready JSON/SQLite committed or cached | R2 = zero egress fees; site build pulls from it |
| Web app | Static site generation (Astro), client-side filtering (Alpine), Cloudflare Pages | ~27k pages is trivial SSG scale; zero backend to operate; free hosting |
| OG images | Pre-rendered at build (satori → PNG) | Original ran a microservice; static is simpler |
| Accounts/identity | Everything prod under gothamizer (GitHub, Cloudflare, ASR + LLM API accounts) | User requirement; no umqadir/Claude fingerprints |

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
  ASR w/ diarization (winner of benchmark)           → utterances w/ timestamps
  speaker naming LLM pass (roster + agenda + intro heuristics) + judge pass
  chaptering LLM pass (full transcript + agenda/matter context)
  summaries (chapter, meeting) + meeting/chapter type labels
  QA gates (coverage %, speaker-unknown %, chapter len distribution, ts monotonicity)
  write artifacts to R2; flag low-confidence items for review
publish (on artifact change)
  build static site (Astro) from R2 data → deploy Cloudflare Pages
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
