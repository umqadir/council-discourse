# Fix stated-meeting chaptering granularity + eval loop

## Context
- PLAN.md = source of truth. Chapterize stage (pipeline/chapterize.py, Gemini 3.5 Flash) produces good committee-hearing chapters but stated meetings come out too coarse: benchmark 2025-04-24-stated got 40 chapters vs citymeetings' 63 (reference: data/benchmark/2025-04-24-stated/citymeetings-chapters.json).
- Reference comparison tooling exists: experiments/05_compare_chapters.py writes comparison-report.md per benchmark meeting.
- GOOGLE_API_KEY is in the environment (network allowed in your sandbox). Gemini spend authorized ~$2 for this task. Use gemini-3.5-flash (production model) for candidate runs; you may use one gemini-3.1-pro-preview run as a judge if useful.

## Task
1. Study the reference: how does citymeetings chapter a stated meeting? (roll calls, individual votes per item, land-use call-ups, communications, member statements — each its own chapter with type labels like VOICE_VOTE/VOTE_OUTCOME).
2. Iterate the chaptering prompt/logic so stated meetings split correctly WITHOUT over-fragmenting committee hearings:
   - Meeting-type-aware guidance is fine (stated vs hearing detected from body name/Legistar data), but keep it one prompt with conditional sections, not a rigid taxonomy fork (see research/03 lessons: rigid taxonomies failed).
   - Do NOT overfit to the single benchmark meeting: the rules must be general (roll call = one chapter; each legislative item's vote = one chapter; etc.).
3. Eval loop: rerun chapterize on data/benchmark/2025-04-24-stated (utterances-named.jsonl exists) + 05_compare against reference; also rerun the transportation benchmark to confirm no regression (its last comparison was good — do not make it worse).
4. Report: chapter counts + boundary-match stats before/after for both benchmarks, prompt diff summary, total Gemini spend.

## Hard constraints
- Operate ONLY in this repo; no browser/MCP; never print API keys.
- You cannot run Metal/MPS jobs. Text/Gemini only.
- .git is read-only in your sandbox — do NOT attempt commits; leave the tree ready and list changed files in your report.
