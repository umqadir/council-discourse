# Task: implement pipeline stages — transcribe (local backend), speaker naming, chapterize, site wiring

Read AGENTS.md, PLAN.md. Extend `pipeline/` (skeleton exists: registry, discover/fetch/prepare, stubs in stages.py). Commit incrementally.

## 1. transcribe stage — local MLX backend first
- Backend abstraction: `transcribe(meeting, backend=...)` writing `utterances.jsonl`
  ({t0, t1, text} segments with real timestamps) + `transcribe-meta.json` (backend,
  model, wall_clock, rtf).
- Implement `local-mlx` backend for Apple Silicon: prefer `parakeet-mlx` (fast) or
  `mlx-whisper` with large-v3-turbo — pick whichever installs/works cleanly via
  `uv pip install` into the project env or uvx; verify on a SHORT slice first
  (data/benchmark/2025-04-23-transportation/slice-start.m4a exists), then run the
  full 2025-04-24-stated meeting audio (audio-16k.wav, ~1.6h) and report wall-clock.
- Leave an `api` backend stub raising NotImplemented with a clear message listing
  env vars it will want (MISTRAL_API_KEY / ELEVENLABS_API_KEY / ASSEMBLYAI_API_KEY).
- No diarization yet: segments as ASR emits them.

## 2. name_speakers stage (Gemini)
- Fetch council roster: Socrata `https://data.cityofnewyork.us/resource/uvw5-9znb.csv?$limit=9999999`
  (City Council Members 1999-present; filter to current term) — cache under data/.
  App token in env NYC_OPENDATA_APP_TOKEN (optional header X-App-Token).
- Gemini 3.5 Flash (GOOGLE_API_KEY, REST generateContent, responseMimeType JSON —
  see experiments/04_chapter_gemini.py for the working call pattern) over the FULL
  transcript: assign a speaker to every utterance. Prompt essentials: roster CSV
  inline (name, district, party), meeting context (body, date, agenda topic from
  meeting.json when present), inference rules (self-introduction > introduced by
  chair > content), allowed fallbacks "Council Staff"/"Member of the Public — {name
  if stated}"/"UNKNOWN". Output: utterance index ranges -> speaker name, then apply
  to produce `utterances-named.jsonl`. Chunk if needed (>150k tokens) with overlap.
- EVAL: `experiments/07_eval_speaker_naming.py` — run name_speakers on the
  2025-04-23-transportation captions (captions-clean.jsonl as pseudo-utterances),
  then compare against citymeetings' verified speaker names in
  `data/benchmark/*/citymeetings-chapter-*.html` (utterances have speaker names +
  seekTo offsets; match by time). Report accuracy + confusion list to a small md file.

## 3. chapterize stage
- Port the prompt from experiments/04_chapter_gemini.py (incl. granularity rules)
  into the pipeline as the production chapterize stage (Gemini 3.5 Flash), input =
  named utterances (or raw captions when naming unavailable), output `chapters.json`
  w/ start_sec resolved against utterance timestamps + end_sec = next start.
  Add meeting-level summary + tags (HEARING/VOTE/STATED_MEETING/LAND_USE) generation
  in the same pass, stored in `meeting-derived.json`.

## 4. Site wiring
- Replace the benchmark adapter with an exporter: `python -m pipeline export-site`
  converts every registry meeting with completed stages into `site/src/data/meetings/*.json`
  (existing schema — see site/scripts + existing JSON for shape). Benchmark meetings
  keep working (they have captions-based data).
- End-to-end acceptance: run the full chain on 2025-04-24-stated —
  transcribe(local-mlx) -> name_speakers -> chapterize -> export-site -> pnpm build.
  The stated meeting's chapter pages should now show real ASR text with named
  speakers (mixed-case, not CC ALL-CAPS).

Report at the end: wall-clock + est. cost per stage, speaker-naming eval accuracy,
and anything that needs my judgment (prompt failures, granularity issues).