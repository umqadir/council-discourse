# ASR comparison harness: Voxtral vs parakeet+pyannote

## Context
PLAN.md = source of truth. We now have two ASR paths:
- LOCAL: parakeet-mlx transcribe -> pyannote diarize -> label-mapping name-speakers (pipeline stages).
- REMOTE candidate: Mistral Voxtral (model id `voxtral-mini-2602`, endpoint /v1/audio/transcriptions, multipart file upload, params diarize=true + timestamp_granularities=segment). A probe on data/benchmark/2025-04-24-stated/audio.m4a produced data/benchmark/2025-04-24-stated/voxtral-transcript.json: 1166 segments, 55 distinct speaker_id labels, 67s wall, schema {text,start,end,speaker_id,type} per segment. MISTRAL_API_KEY is in .env (never print it).

## Task
1. Add a voxtral backend to pipeline/transcribe.py (backend="voxtral"): calls the API (httpx/requests, multipart; audio.m4a preferred), converts segments to our utterances.jsonl schema (t0/t1/text) AND writes utterances-labeled.jsonl directly (speaker_id -> label), plus transcribe-meta.json (wall time, usage, model). Files >3h (10800s per meeting.json duration or ffprobe): split audio at a silence near the midpoint via ffmpeg (you may run ffmpeg — CPU only), transcribe parts, merge with offsets, and RELABEL second-part speakers by appending part suffix (labels don't persist across requests) — note this limitation in meta for the naming stage.
2. Generalize experiments/07_eval_speaker_naming.py: accept --benchmark {transportation,stated} and --asr {local,voxtral} (default current behavior). For voxtral: use its utterances-labeled.jsonl, run the SAME label-mapping name_speakers, score against the citymeetings references in that benchmark dir (stated dir has citymeetings-chapter-*.html files too).
3. Do NOT run Metal jobs. You MAY run the voxtral API (cheap, ~$0.30/meeting) and Gemini naming (~$0.40/meeting) for BOTH benchmarks (transportation is 3.9h -> exercise the split path). Budget ~$3 total.
4. Report: eval table (same-person accuracy, strict spelling) for voxtral-stated, voxtral-transportation; wall times; any schema surprises. If local-path eval results exist by then (data/benchmark/2025-04-23-transportation/speaker-naming-eval.md gets overwritten by runs — snapshot per-config results to speaker-naming-eval-{asr}-{benchmark}.md instead, and make the script do that).

## Hard constraints
- Only this repo; no browser/MCP; never print keys; .git read-only (no commits; list changed files).
- A local diarize-eval chain may be running concurrently writing to data/benchmark/2025-04-23-transportation/ (pyannote path). Don't delete/overwrite its diarization.jsonl / utterances-labeled.jsonl for the LOCAL asr config; voxtral files must use distinct names (utterances-voxtral.jsonl, utterances-voxtral-labeled.jsonl).
