# Council Discourse

Every NYC Council meeting, transcribed with named speakers and divided into titled chapters. Live at **[council-discourse.pages.dev](https://council-discourse.pages.dev)**. Coverage from April 2025 forward.

## Credit

Idea and core methodology, same-day transcription with speaker naming and LLM chaptering, from **[Vikram Oberoi](https://vikramoberoi.com)**'s citymeetings.nyc, documented in his public talks and writing. This is an independent reimplementation that modifies his approach.

## Pipeline

| Stage | |
|---|---|
| Discovery | Legistar Web API, Council video RSS |
| Ingest | video remuxed and re-hosted on Cloudflare R2 |
| Transcription | ASR with speaker diarization |
| Speaker naming | LLM pass over roster and agenda evidence, verified against public records |
| Chaptering | full-transcript LLM chaptering, anchored to the agenda |
| Site | Astro on Cloudflare Pages, static meeting pages and edge-rendered chapter pages |

Runs as a CLI, locally or on scheduled CI. The site rebuilds as meetings land.

Corrections: open an issue, or use the report link on any chapter page.
