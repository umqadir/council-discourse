# Long-Meeting Transcription + Speaker Attribution: Frontier (July 2026)

All claims checked against live web sources July 1, 2026.

## 1. Audio-native multimodal LLMs

### Gemini 3.x (Google)
- `gemini-3.5-flash` (flagship audio), `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`. Up to **9.5 hours audio per prompt**; 32 tokens/sec (~115k tokens/hour); >20MB via Files API; 16kHz mono downsample.
- Pricing: 3.5 Flash audio input $3.50/M (~$0.0053/min), output $9/M; 3.1 Flash-Lite audio $0.50/M; batch 50% off.
- **Timestamps unreliable on long audio**: documented unresolved bug (Mar–Jun 2026) — progressive drift, internal clock up to 22% fast on 3 Flash; 3.1 Pro drifts −17s; 3.1 Flash-Lite unaffected. Simon Willison's 3h33m Half Moon Bay council test: coherent speaker-attributed output for $1.42, but timestamps off by up to 2.5 hours and some sections summarized instead of transcribed.
- **Verdict: cheap semantic layer (naming, chaptering, correction); never the timestamped transcriber.**

### OpenAI
- `gpt-4o-transcribe-diarize`: speaker-labeled segments w/ timestamps, **known-speaker enrollment via 2–10s reference clips (up to 4)** — unique, useful for recurring council members. Constraints: 25MB file limit, ~23-min chunk cap → client-side chunking + cross-chunk label reconciliation. $0.006/min ($0.36/hr) incl. diarization.
- May 2026 releases (gpt-realtime-2 etc.) are streaming-focused; nothing new for long-form batch.
- gpt-4o-transcribe "smooths" transcripts (lower verbatim fidelity).

### Mistral — Voxtral Transcribe 2 (Feb 2026) — notable
- **Voxtral Mini Transcribe V2** (batch): **diarization, word-level timestamps, context biasing, up to 3 hours per request**. Claims accuracy above GPT-4o-mini-Transcribe, Gemini 2.5 Flash, AssemblyAI Universal, Deepgram Nova.
- **$0.003/min ($0.18/hr) with diarization — ~1/5 competitors' price.**
- Voxtral Realtime open-weights (no diarization).

### Others
- **Qwen3-ASR** (Jan 2026): open Apache-2.0, 1.7B/0.6B, strong WER, no diarization (separate ForcedAligner for timestamps).
- **MOSS-Transcribe-Diarize** (Jan 2026): end-to-end speaker-attributed timestamped LLM; 90-min cap; near top of Open ASR Leaderboard.
- **Microsoft VibeVoice ASR** (Jan 2026): open Whisper-style w/ diarization; 1-hour cap.

## 2. Dedicated ASR APIs

| Vendor | Current model | Batch price | Diarization | Notes |
|---|---|---|---|---|
| Mistral | Voxtral Mini Transcribe V2 | **$0.18/hr** | **incl.** | see above |
| Rev AI | Reverb | $0.18/hr | yes | Reverb also open-weights |
| AssemblyAI | Universal-3 Pro ($0.21/hr) / U-2 ($0.15/hr) | $0.15–0.21/hr | +$0.02/hr | Only vendor publishing far-field meeting cpWER: **33.3% vs Deepgram 43.2%, Speechmatics 46.1%** (DiPCo/NOTSOFAR) |
| ElevenLabs | **Scribe v2** (Jan 2026) | $0.22/hr | incl. ("98% label accuracy" claim) | **Best measured long-form WER: 7.32%**; tops AA WER index |
| Speechmatics | Ursa | $0.30–0.50/hr | incl. | long-form WER 8.80% |
| OpenAI | gpt-4o-transcribe-diarize | $0.36/hr | incl. | chunking constraints |
| Deepgram | Nova-3 | ~$0.46/hr + $0.12/hr diar. | add-on | what citymeetings used; fastest batch |
| Google | Chirp 3 | $0.96/hr ($0.24 dynamic batch) | incl. | diarization historically weak |

Independent long-form WER (academic reproduction, arXiv 2510.06961): Scribe v2 7.32% < AssemblyAI U-3 Pro 8.34% < Speechmatics 8.80% — closed APIs beat all open models long-form; best open: Cohere Transcribe 9.73%, Parakeet TDT 0.6B v3 10.7%, Whisper large-v3-turbo 11.0%.

## 3. Open-source / self-hosted
- HF Open ASR Leaderboard (short-form, 2026): ARK-ASR-3B (4.76), MOSS-Transcribe-preview-2B (4.87), Cohere transcribe-03-2026 (5.42), Canary-Qwen-2.5B (5.63). **Long-form: Whisper large-v3 still best open**; LLM-decoder models degrade on long audio.
- Best-practice self-hosted 2026: **WhisperX (faster-whisper large-v3) + pyannote community-1**.
- Cost floor: RunPod RTX 4090 $0.34–0.69/hr; 3-hr meeting ≈ 10–20 GPU-min → **$0.06–0.20/meeting**. Mac M-series: parakeet-mlx ~70x realtime, ~free.

## 4. Diarization specifically
- **pyannote.audio 4.0 + speaker-diarization-community-1** (CC-BY-4.0) is the open standard: AliMeeting DER 20.3% (vs 24.5% for 3.1); exclusive single-speaker mode designed for ASR-word reconciliation. pyannoteAI hosted "precision-2" claims better.
- Audio-LLMs' native diarization: mostly not reliable for 3–6h files (Gemini drift; OpenAI 23-min chunks; MOSS 90-min cap). Robust pattern remains dedicated ASR + diarizer, or diarization-engineered batch API (AssemblyAI, Scribe v2, Voxtral V2).

## 5. Municipal-meeting hard case
- citymeetings.nyc precedent: Deepgram + LLM passes + human review. LLMs good at *correcting* mistranscriptions from context; naive whole-transcript speaker-ID failed mid-transcript (2024-era).
- No rigorous published council/parliament ASR benchmark exists — ours would be novel.
- **Ground truth: NYC City Clerk official transcripts (cityclerk.nyc.gov) + Legistar attachments + citymeetings' human-reviewed output.** Official transcripts are lightly edited (fillers removed) — normalize aggressively; use for relative ranking + cpWER w/ speaker names.

## (a) Benchmark shortlist
1. **Voxtral Mini Transcribe V2** — price-performance outlier, 3-hr requests, unproven on far-field municipal audio.
2. **AssemblyAI Universal-3 Pro + diarization** — leads the only published far-field meeting comparison.
3. **ElevenLabs Scribe v2** — best long-form WER; test diarization at 3+ hrs / 20+ speakers.
4. **Self-hosted WhisperX + pyannote community-1** (+ Parakeet variant) — open baseline, cost floor.
5. **gpt-4o-transcribe-diarize** — one slot for known-speaker enrollment (council member reference clips).
Plus cross-cutting LLM layer (Gemini 3.5 Flash / 3.1 Flash-Lite) for speaker naming + correction — timestamps always from ASR layer.

## (b) Cost per 3-hour meeting
WhisperX+pyannote (RunPod) ~$0.06–0.20 · local Mac ~$0 · Voxtral V2 $0.54 · Rev $0.54 · AssemblyAI U-2 $0.51 / U-3 Pro $0.69 · Scribe v2 $0.66 · Chirp 3 batch $0.72 · Speechmatics $0.90 · gpt-4o-transcribe-diarize $1.08 · Gemini 3.5 Flash ~$1.50 ($0.75 batch) · Deepgram Nova-3 $1.75.
At ~500 meeting-hours/year: $90–800/year — cost is a tiebreaker; speaker-attribution accuracy is the decision.

## (c) Benchmark design
- Test set: 4–6 meetings spanning difficulty (stated meeting w/ roll calls; budget hearing w/ public testimony; small committee; hybrid/remote).
- Ground truth two tiers: hand-corrected 10–15-min gold segments (start/middle/end — catches positional degradation) for WER/cpWER/DER; official transcripts for full-meeting relative ranking + named-attribution scoring.
- Metrics: WER (gold), **cpWER (headline)**, DER/speaker-count error, timestamp offset vs video (20 sampled utterances — required for click-to-seek UX), positional degradation bins, hallucination on non-speech spans, cost + wall-clock + failure behavior.
- Tools: `meeteval` (cpWER), `pyannote.metrics` (DER). Identical 16kHz mono inputs. One holdout meeting until finalists chosen.
- Likely end-state to validate: diarization-strong cheap transcriber + LLM naming/correction pass + ASR-layer timestamps.

## Addendum: local ASR verification for the M4/32GB Mac (2026-07-02)
Focused re-check confirmed: **parakeet-mlx v0.5.2 + mlx-community/parakeet-tdt-0.6b-v3**
is the optimal local backend (60-70x realtime on M-series; native token-level
timestamps with 120s/15s chunking that doesn't drift over hours; non-autoregressive
so no silence hallucination; long-form WER 6.68 vs whisper large-v3's 6.43 at ~40x
the speed). Fallback for max accuracy: mlx-whisper large-v3 with Silero VAD +
condition_on_previous_text=False (or whisperx-mlx for forced-alignment word stamps).
Rejected for this hardware: VibeVoice (30-60GB RAM, 59-min cap), Voxtral Realtime
(no WER edge), Qwen3-ASR (5-min aligner cap), MOSS (8B, no MLX port), ARK-ASR (no MLX).

## Addendum 2: whisper implementation check for M4 (2026-07-02)
Freshness check on whisper impls (fast-moving): mlx-whisper ~1.78x whisper.cpp;
whisper.cpp ~3x faster-whisper on Apple hw; only WhisperKit (Argmax, ANE) is
potentially faster (+1.3-1.8x over Metal on M3/M4). Same weights = same output
quality; implementation only affects speed. Local primary remains parakeet-mlx
(~60-70x RT, no whisper comes close). mlx-whisper stays the accuracy-fallback;
IF local whisper ever becomes a hot path, benchmark WhisperKit first.
Sources: github.com/anvanvan/mac-whisper-speedtest; notes.billmill.org (Jan 2026
mlx vs cpp); voicci.com apple-silicon-whisper-performance; promptquorum.com 2026 STT comparison.
