# Council Discourse

NYC Council meetings, transcribed with named speakers and divided into titled chapters. Live at **[discourse.qqlab.io](https://discourse.qqlab.io)**. Coverage starts late May 2026.

Independent project, not affiliated with the New York City Council. Transcripts, speaker names, and chapters are machine-generated and may contain errors.

## Credit

Idea and core methodology, same-day transcription with speaker naming and LLM chaptering, from **[Vikram Oberoi](https://vikramoberoi.com)**'s citymeetings.nyc, documented in his public talks and writing. This is an independent reimplementation that modifies his approach.

## Pipeline

| Stage | |
|---|---|
| Discovery | Legistar Web API, Council video RSS |
| Ingest | video remuxed and hosted on Cloudflare R2 |
| Transcription | Voxtral Mini 2602 Batch API, with speaker diarization ($0.09/audio-hour) |
| Speaker naming | Gemini 3.8 Flash (DeepSeek V4 Pro recovery for failed responses), anchored to the dated Council roster and meeting agenda |
| Chaptering | DeepSeek V4.1 Flash via OpenRouter, full transcript anchored to the agenda |
| Site | Astro on Cloudflare Pages, static meeting pages and edge-rendered chapter pages |
| Schedule | GitHub Actions |

## Model review, September 2026

| Check | Result |
|---|---|
| Speaker naming, 87 labeled cases | Gemini 3.8 Flash 81, GLM 5.3 Flash 72, Gemini Pro 70 |
| Naming cost, 2 meetings | Gemini 3.8 Flash $0.098, Gemini Pro $0.166 |
| Chaptering cost, 2 meetings | DeepSeek V4.1 Flash $0.056, Gemini $0.432 |
| Batch vs. synchronous ASR, 2 eight-minute clips | identical text and timestamps |

## Corrections

Use the report link on any page.

## License

Code: [MIT](LICENSE).
