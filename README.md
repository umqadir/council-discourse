# Council Discourse

Every NYC Council meeting, transcribed with named speakers and divided into titled chapters. Live at **[council-discourse.pages.dev](https://council-discourse.pages.dev)**. Coverage from April 2025 forward.

## Credit

Idea and core methodology, same-day transcription with speaker naming and LLM chaptering, from **[Vikram Oberoi](https://vikramoberoi.com)**'s citymeetings.nyc, documented in his public talks and writing. This is an independent reimplementation that modifies his approach.

## Pipeline

| Stage | |
|---|---|
| Discovery | Legistar Web API, Council video RSS |
| Ingest | video remuxed and re-hosted on Cloudflare R2 |
| Transcription | Voxtral Mini 2602 Batch API, with speaker diarization ($0.09/audio-hour) |
| Speaker naming | Gemini 3.8 Flash (V4 Pro recovery for failed responses), then Gemini 3.8 Flash with Google Search for public-record verification |
| Chaptering | DeepSeek V4.1 Flash via OpenRouter, full transcript anchored to the agenda |
| Site | Astro on Cloudflare Pages, static meeting pages and edge-rendered chapter pages |

Runs as a CLI, locally or on scheduled CI. The site rebuilds as meetings land.

Council membership refreshes from dated Legistar office records before each scheduled run, with NYC Open Data providing older historical coverage. Validation rejects overlapping current terms and incomplete source responses; special-election changes do not require code edits. The validated roster travels with the run so all stages use the same membership snapshot.

Verification first retrieves cited research in small batches, then formats corrections from those notes. It applies model corrections only when Google returns actual search sources and the correction cites supporting URLs; abbreviated web display names do not erase fuller spoken names. Council-member aliases are normalized before verification to avoid unnecessary searches. ASR rejects speech with missing diarization rather than publishing it.

September 2026 model review: Gemini 3.8 Flash scored 81/87 on the existing labeled naming screen (Pro 70, GLM 5.3 Flash 72). Two production-prompt replays cost $0.098 versus Pro $0.166. DeepSeek V4.1 Flash retained detailed vote chapters at $0.056 versus Gemini $0.432 across the same two meetings. Batch ASR was revalidated against synchronous transcription on two eight-minute clips: identical text/timestamps, identical hearing labels and minor roll-call clustering differences. These bounded checks support the choices; they do not establish perfect accuracy. Gemini introductory pricing expires December 31, 2026.

Stage logs record processing date, production versus ad hoc purpose, workflow run ID, and provider-reported charges where available. Verification usage is retained separately. Cached stage reuse is marked and must not be counted as new consumption.

Corrections: open an issue, or use the report link on any chapter page.
