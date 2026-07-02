# Brief: single-pass speaker naming + stated-meeting chapter granularity

Work in this repo (council-discourse). Python via the project venv (`uv run` or the global env). GOOGLE_API_KEY is in the environment; LEGISTAR_TOKEN is in `.env`. Commit in logical chunks as you go (git author is already configured). Do NOT touch data/benchmark inputs (audio, captions, reference JSONs) — outputs/artifacts are fine.

## Context
The stages pipeline works end-to-end. Eval evidence from experiments/07_eval_speaker_naming.py on the 2025-04-23 transportation benchmark:
- one-shot naming (whole transcript in one prompt): 88.8% accuracy
- chunked production path (4 chunks): 67.5% — errors cluster at chunk boundaries / public-witness handoffs
Stated-meeting chapterization (data/meetings/NYCC-PV-CH-CHA_260528-100627) produced 40 chapters vs ~63 in the citymeetings reference for the 2025-04-24 stated benchmark — too coarse, mostly missing per-item vote/roll-call splits.

## Task 1 — make speaker naming single-pass by default
In pipeline/speakers.py:
1. Restructure so the ENTIRE meeting transcript goes into ONE naming prompt by default. Gemini 3.5 Flash has 1M-token context; a 9-hour meeting is ~160k tokens, so in practice chunking should never trigger. Raise the effective limit accordingly (e.g. chunk only above ~700k prompt tokens) and keep the chunked path purely as an overflow fallback.
2. When the fallback DOES chunk, add a cheap reconciliation pass: after per-chunk naming, send Gemini a compact table of (diarized speaker label, per-chunk assigned names, first/last utterance snippets around each boundary) and ask it to produce one consistent mapping. But keep this simple — it's a fallback that will almost never run.
3. Re-run the eval one-shot against the transportation benchmark and report accuracy + cost. Target: >=85% on the 80 matched reference utterances. If below, inspect the error table in the eval report, classify the failures (mistranscription vs role confusion vs genuine ambiguity), and make ONE targeted prompt improvement per failure class (e.g. roster anchoring, self-introduction precedence rules) — the citymeetings creator's playbook. Do not iterate more than 2 rounds; report what remains.

## Task 2 — remove the hardcoded alias in the eval
experiments/07_eval_speaker_naming.py has `if value == "Robert Bookman": return "Rob Bookman"`. Replace with a GENERIC nickname-equivalence step in name matching: normalize via a standard nickname map (Robert/Rob/Bob, William/Bill, Elizabeth/Liz, etc. — a small builtin dict of common English nicknames applied to first names when last names match). No person-specific hardcoding.

## Task 3 — stated-meeting chapter granularity
In pipeline/chapterize.py, strengthen the prompt with meeting-type-aware splitting rules (the meeting type is known from Legistar/registry metadata; stated meetings are identifiable):
- Each agenda item's vote/adoption = its own chapter (VOICE_VOTE / VOTE_OUTCOME types).
- Roll calls = separate chapter.
- Each council member's floor remarks/explanation of vote = separate chapter (REMARKS).
- Ceremonial items (invocation, honoree presentations) each separate.
- General guidance: prefer 1-5 minute chapters; a chapter should answer "one thing happened here."
Re-run chapterize on the June stated meeting (data/meetings/NYCC-PV-CH-CHA_260528-100627) and the 2025-04-24 stated benchmark; compare counts and boundary alignment vs the reference with experiments/05_compare_chapters.py. Target: within ~20% of reference chapter count with sensible boundaries. Report before/after.

## Task 4 — surface eval + run costs
Add to the stage report (or a small runlog) the Gemini token usage and estimated $ per stage per meeting, so each run shows cost drift. (Read usage from API responses; keep it simple.)

## Acceptance
- experiments/07 eval one-shot >=85% (or documented failure classes after 2 rounds)
- stated-meeting chapter count within ~20% of reference, no meaning-inverting titles on spot-check
- all committed; report: accuracy before/after, chapter counts before/after, per-meeting cost table, anything needing my judgment.
