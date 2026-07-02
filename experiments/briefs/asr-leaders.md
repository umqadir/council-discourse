# ASR quality leaders: Scribe v2 + AssemblyAI vs Voxtral incumbent

## Context
PLAN.md sections 8-9. Voxtral is prod ASR (87.6% / 95.9% same-person on transportation/stated). Testing the two published accuracy leaders on identical benchmarks with the identical naming+eval pipeline. Keys in .env: ELEVENLABS_API_KEY (STT-scoped — some endpoints 401, /v1/speech-to-text works, model_id=scribe_v2, multipart file + diarize=true), ASSEMBLYAI_API_KEY (standard). Never print keys.

## Task
1. Add two transcribe backends following the voxtral pattern in pipeline/transcribe.py (distinct artifact names: utterances-scribe-*.jsonl, utterances-assemblyai-*.jsonl):
   - elevenlabs scribe_v2: POST /v1/speech-to-text multipart; diarize=true; capture word/segment timestamps + speaker ids. Check response schema first with a 30s clip before full runs.
   - assemblyai: async job API (upload -> transcript with speaker_labels=true); use speech_model="universal" default (Universal-3 Pro tier if a flag exists — check their docs via the SDK or error messages, do NOT web-browse); poll to completion.
2. Extend experiments/07_eval_speaker_naming.py --asr {local,voxtral,whisper,scribe,assemblyai}.
3. Run both backends x both benchmarks (stated 1.6h, transportation 3.9h — mind file-size/duration limits; chunk if forced, noting it) + naming + eval. Budget: free credits should cover; abort a vendor if it wants >$5.
4. Report table: same-person, strict spelling, ASR wall-clock, cost vs Voxtral baselines (87.6/95.9, 70.4/73.0 strict). Write reports to data/benchmark/*/speaker-naming-eval-{scribe,assemblyai}-*.md.

## Hard constraints
Only this repo; no browser/MCP/web; no Metal; never print keys; .git read-only (no commits; list changed files). data/benchmark files for OTHER asr configs must not be touched.
