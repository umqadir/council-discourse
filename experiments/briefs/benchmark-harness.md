# Task: chapter-quality comparison harness

Repo: you are in its root. Read AGENTS.md and PLAN.md first (PLAN.md §3 covers the
experiment design). Work only under `experiments/`; commit when done.

## Context
We generate meeting "chapters" with LLMs and need to compare them rigorously against
the reference output of citymeetings.nyc (human-reviewed) for the same meetings.

Data (per meeting) under `data/benchmark/{slug}/`:
- `citymeetings-chapters.json` — reference chapters: `chapter_id`, `url`, `badge`
  (type), `card_text` (title + truncated summary + "H:MM:SS • NN sec/min" tail),
  `start_ts` (H:MM:SS). NOTE: title/summary/duration are currently mushed inside
  `card_text` and need parsing. The meeting-page HTML is saved alongside if you need
  better selectors (`citymeetings-meeting-page.html`; cards are `a[data-chapter-id]`).
- `chapters-{model}.json` — our generated chapters: `{start: "H:MM:SS", type, title,
  summary}` plus run metadata. Present so far: `chapters-gemini-3.5-flash.json`,
  `chapters-gemini-3.1-flash-lite.json` for slug `2025-04-23-transportation`.
- `captions-clean.jsonl` — timestamped caption fragments `{t: sec, text}` (the
  transcript the models saw).
- `citymeetings-chapter-samples.json` + `citymeetings-chapter-{id}.html` — a few full
  chapter pages with per-utterance `player.seekTo(sec)` offsets and speaker names.

## Deliverables
1. `experiments/05_compare_chapters.py` — given a slug, produces
   `data/benchmark/{slug}/comparison-report.md` containing:
   - Reference parsing: extract clean `title`, `summary`, `duration_sec`, `start_sec`
     per reference chapter (parse meeting-page HTML properly, don't regex card_text
     if the HTML has cleaner structure).
   - Counts + duration distributions (ours vs reference), coverage of meeting span,
     any gaps/overlaps or non-monotonic starts in ours.
   - Boundary agreement: for tolerances of 15s/30s/60s, precision/recall/F1 of our
     chapter starts vs reference starts.
   - Alignment table: greedy time-based alignment; for each reference chapter, the
     best-overlapping generated chapter with both titles side by side and start
     deltas — the full table in the report (this is the part a human reads to judge
     title quality).
   - Type confusion summary (reference badge vs our type for aligned pairs).
2. Run it for `2025-04-23-transportation` on both existing model outputs (the script
   should accept `--model` or iterate over all `chapters-*.json` and emit one
   combined report with a section per model).
3. Timestamp sanity check (small section in the same report): using
   `citymeetings-chapter-samples.json`, verify reference `seekTo` offsets are on the
   same clock as our captions timeline (sample a few utterances, find nearest caption
   fragment with similar text, report median offset). This validates that viebit
   video time == citymeetings Mux time.
4. Keep dependencies to the global python env (httpx, bs4, pandas ok). No new services.

## Quality bar
The report must be readable by a human making a model-choice decision in 5 minutes:
lead with a metrics summary table, then the alignment table. Deterministic, re-runnable.

When finished: run it, sanity-check your own report for obvious parsing failures
(e.g. reference titles that are empty or contain summary text), fix, re-run, commit
with a clear message.
