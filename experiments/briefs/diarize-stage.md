# Add diarization stage; rework speaker naming to label-mapping

## Context (read first)
- PLAN.md = source of truth. This repo processes NYC Council meeting videos: fetch -> prepare -> transcribe (parakeet-mlx, no speaker labels) -> name-speakers (Gemini) -> chapterize -> export-site.
- Speaker naming currently fails on long meetings: single-pass naming over unlabeled utterances collapses mid-transcript. Evidence: data/meetings/NYCC-PV-CH-CHA_260528-100627/utterances-named.jsonl has ONE speaker assigned to 1,807 consecutive utterances (min 122 to end of a 352-min meeting). Eval on 2025-04-23 transportation benchmark: 42.7% same-person accuracy vs citymeetings reference. The bar is ~85%+.
- Research conclusion (research/05-asr-frontier.md): robust 2026 pattern = ASR + dedicated diarizer + LLM label->name mapping. pyannote.audio 4.x `pyannote/speaker-diarization-community-1` is the open standard; HF_ACCESS_TOKEN is in .env (never print it).

## Task
1. New pipeline stage `diarize` between transcribe and name-speakers:
   - pyannote speaker-diarization-community-1 on audio-16k.wav, device MPS (torch mps; fall back cpu). Add deps via `uv add`.
   - Output: diarization.jsonl (turns: start, end, label like SPK_00) + diarize-meta.json (model, wall time, n_labels).
   - Assign each utterance in utterances.jsonl a `label` by max time-overlap with turns (fallback: nearest turn midpoint). Write utterances-labeled.jsonl.
   - Registry: add diarize status column/handling like other stages; CLI subcommand `diarize --meeting-key`.
2. Rework name-speakers to label-mapping mode:
   - Input: utterances-labeled.jsonl. For each diarized label build evidence: total time share, first/last activity, ~8 sampled utterance windows w/ surrounding dialogue (self-intros, being addressed), like the citymeetings v1 approach (research/03 sec 2).
   - One Gemini 3.5 Flash call (or a few if >40 labels): map every label -> {name, role, org, confidence}, with roster CSV context (existing) + agenda/Legistar context (existing). Keep the existing verification/correction pass.
   - Write utterances-named.jsonl by joining labels->names (keep per-utterance confidence from label confidence). Preserve current output schema (t0/t1/text/speaker/confidence) so chapterize/export are untouched.
   - Handle label impurity: if a label's sampled instances clearly contain multiple people (roll calls!), allow name-speakers to split by marking specific utterance ranges (keep simple: optional per-range overrides list in the model output schema).
3. Update experiments/07_eval_speaker_naming.py: --use-existing-utterances mode must eval the new path (diarize benchmark audio too — but do NOT run pyannote/Metal jobs yourself; see constraints).
4. Tests for the overlap-assignment and schema (pure-python, no Metal/network).

## Hard constraints
- Operate ONLY in this repo. No browser/MCP tools.
- You CANNOT run Metal/MPS or network Gemini/HF-download jobs in your sandbox. Write the code + tests; the supervisor runs the actual stages. Provide exact commands to run in your final report.
- Never print API keys/tokens. Do not commit .env.
- Keep diffs focused; follow existing pipeline code style; update PLAN.md pipeline section.

## Acceptance
- `uv run python -m pipeline diarize --meeting-key NYCC-PV-CH-CHA_260528-100627` runs (supervisor executes).
- name-speakers on labeled utterances produces plausible speaker-share distribution (no speaker >25% unless chair).
- Eval script ready to rerun on transportation benchmark.
- Report: exact supervisor commands, expected wall times, any risks.
