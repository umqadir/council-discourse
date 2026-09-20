# Council Discourse

Every NYC Council meeting, transcribed with named speakers and divided into titled chapters. Live at **[council-discourse.pages.dev](https://council-discourse.pages.dev)**. Coverage from April 2025 forward.

## Credit

Idea and core methodology, same-day transcription with speaker naming and LLM chaptering, from **[Vikram Oberoi](https://vikramoberoi.com)**'s citymeetings.nyc, documented in his public talks and writing. This is an independent reimplementation that modifies his approach.

## Pipeline

| Stage | |
|---|---|
| Discovery | Legistar Web API, Council video RSS |
| Ingest | video remuxed and re-hosted on Cloudflare R2 |
| Transcription | Voxtral Mini 2602 synchronous API, with speaker diarization ($0.18/audio-hour) |
| Speaker naming | DeepSeek V4 Pro via OpenRouter, then Gemini 3.1 Flash Lite public-record verification |
| Chaptering | DeepSeek V4.1 Flash via OpenRouter, full transcript anchored to the agenda |
| Site | Astro on Cloudflare Pages, static meeting pages and edge-rendered chapter pages |

Runs as a CLI, locally or on scheduled CI. The site rebuilds as meetings land.

Corrections: open an issue, or use the report link on any chapter page.
