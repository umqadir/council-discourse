# Robustness round 2: cost-loop and state-model fixes

A four-lens review (cost loops, state persistence, paid-call idempotency, silent
failures) produced a converged fix list. The workflow-side fixes are already
applied in `.github/workflows/production.yml` (circuit breaker, preflight
upgrades, R2 restore hard-fail, chunk-file persistence, path-scoped rebase
fallback). This brief covers the pipeline-Python side. Work top to bottom;
each item independently shippable; add or extend tests for every item; run
`uv run pytest tests/ -q` (all currently pass, 50 tests).

Style: follow existing repo idiom. No AI mentions in code or commits. Commit
per logical item (or small groups), imperative subjects.

## 1. Artifact-exists short-circuit for naming and chaptering (highest value)

`pipeline/production.py` gates `_stage_name_speakers` (~line 230) and
`_stage_chapterize` (~line 257) on registry status only. Transcription has a
file-level guard (`voxtral_prod.py:44` -> `_canonical_voxtral_complete`,
lines 95-104) so a regressed status cannot re-pay it; naming and chaptering
lack the equivalent, so a lost registry commit (export job died before commit)
re-pays both LLM stages for a whole 12-meeting batch even though the outputs
were restored from R2.

Mirror the voxtral pattern: at the top of `name_speakers_meeting`
(`pipeline/speakers.py:~201`) and `chapterize_meeting`
(`pipeline/chapterize.py:~80`), return early (recording a "skipped/cached"
result) when the output artifact AND its stage meta exist, are non-empty, and
parse. For naming: `utterances-named.jsonl` + `name-speakers-meta.json`. For
chaptering: `chapters.json` + its meta. Keep the check conservative: any parse
failure means proceed with a fresh run.

## 2. Naming stage: never discard paid work on verification failure

`pipeline/speakers.py:~270-287`: per-chunk label mappings live only in memory,
and `write_jsonl(output)` happens only after ALL naming chunks AND the Gemini
verification call succeed. Two fixes:

a. Checkpoint each chunk's mapping to `name-speakers-chunk-{n}.json` in the
   meeting dir immediately after it returns; on retry, reuse parsed checkpoint
   files instead of re-calling the LLM (delete-and-redo on parse failure,
   same as `voxtral-transcript-part-*.json` handling). Add
   `name-speakers-chunk-*.json` to the R2 persist include list in
   `.github/workflows/production.yml` (persist step ~line 225).

b. Make verification non-fatal: write `utterances-named.jsonl` BEFORE
   `_verify_non_roster_speakers`; run verification in try/except; on failure,
   keep the unverified output, record `"verified": false` and the error in
   `name-speakers-meta.json`, and continue. On success rewrite the output and
   set `"verified": true`. Verification is an enhancement, not a gate.

## 3. Dead-letter: attempts cap so no meeting retries forever

Schema (`pipeline/db.py`): add `process_attempts INTEGER NOT NULL DEFAULT 0`
column (migration additions dict + CREATE TABLE + MEETING_COLUMNS).
`pipeline/production.py process_one`: on failure, increment
`process_attempts`; on a fully-complete run, reset it to 0.
`select_process_candidates`: exclude rows with `process_attempts >= 5` and
emit a stderr warning listing excluded keys so the discover log shows them.
The result JSON row snapshot must carry the incremented value so merge_results
persists it (verify it does; fix if the row snapshot is taken before the
increment).

## 4. merge_results must never regress a done status

`pipeline/production.py merge_results` (~451-471) writes incoming result rows
blindly, so a result produced from a downgraded local state can regress the
canonical registry (e.g., transcribed -> pending) even though R2 holds the
artifacts. For each stage status column, take the more-advanced value of
(current registry value, incoming value); define the per-column done values
(fetch_status='fetched', prepare_status='prepared',
transcribe_status='transcribed', diarize_status='diarized',
name_speakers_status='named', chapterize_status='chapterized'): a done value
never gets overwritten by a non-done value. Non-status fields keep current
behavior. `process_attempts` merges by max, except an incoming 0 (success
reset) wins.

## 5. Per-meeting cost capture

`pipeline/gemini.py:_attach_openrouter_cost` already records `exact_cost_usd`
in naming and chaptering metas; `transcribe-meta.json` records
`audio_duration_sec` but no dollars. Add a `cost_usd REAL` column; in
`process_one`, after stages complete (and also on failure), sum:
naming meta `exact_cost_total` (or per-chunk `exact_cost_usd`s) +
chaptering meta `exact_cost_usd` + `audio_duration_sec / 3600 * 0.09`
(define `VOXTRAL_USD_PER_AUDIO_HOUR = 0.09` in `pipeline/config.py`).
Store on the row, include in the result JSON, and print
`cost_usd=<x>` on the process-one stdout summary line so it lands in CI logs.
Merge in merge_results by "latest non-null wins".

## 6. Chapterize prompt-size guard

`pipeline/chapterize.py` builds one prompt from the entire transcript with no
size check. Before the LLM call, estimate tokens (chars/4 is fine) and raise a
distinct `RuntimeError("transcript too long for chaptering: ~Nk tokens")`
above a 180k-token threshold (env-overridable
`COUNCIL_CHAPTER_MAX_PROMPT_TOKENS`). This converts a pathological meeting
into a first-attempt cheap failure that the dead-letter cap then parks.

## 7. Registry dedupe must not delete completed rows

`pipeline/db.py:_find_existing_row` (~175-180): when an event-id row and a
filename row conflict, it DELETEs the event row outright. If the deleted row
has advanced statuses (paid work), that work is orphaned and re-paid under a
new key. Change to: merge the doomed row into the survivor first - copy
non-null fields the survivor lacks, and per stage-status column keep the more
advanced value (same ladder as item 4). Refuse to delete (prefer the
completed row as survivor, transferring the new filename onto it) when the
event row is fully chapterized and the filename row is pristine.

## 8. Surface last_error and freshness in CI

a. `pipeline/cli.py` status command: include a truncated `last_error` column.
b. New CLI subcommand `pipeline ci-health` that prints:
   - rows with non-null last_error (key + first 200 chars)
   - count of viebit rows with no Legistar match older than 7 days
   - newest event_date in the registry
   and exits 0 always (informational). Wire a workflow step in export-site
   after merge-results: `uv run python -m pipeline ci-health | tee -a "$GITHUB_STEP_SUMMARY"`.
c. Discovery freshness: in `discover_viebit_rss`, raise RuntimeError when the
   feed parses to zero items (the feed always has history; zero means a source
   regression). In `discover_legistar`, when the token is missing AND
   `GITHUB_ACTIONS` env var is set, print an `::error::`-style loud stderr
   line before returning (keep the local-dev silent skip).

## 9. merge-results input scope

`merge_results` uses `results_dir.rglob("*.json")`, which sweeps the meeting
artifact files rsynced under `data/processed-results/meetings/` in CI. Only
top-level result files are wanted: change to `results_dir.glob("*.json")` and
adjust the workflow comment if needed (the workflow moves meeting artifacts to
data/meetings before merge, but the source files remain; do not rely on that).
Check tests that exercise rglob behavior (test_production_pipeline.py uses
rglob-based expectations - update them to the new contract).

## Acceptance

- `uv run pytest tests/ -q` green, with new tests covering: artifact
  short-circuits (naming + chaptering skip paid path when outputs present),
  chunk checkpoint reuse, verification-failure still writes output,
  attempts increment/reset/exclusion, status no-regress merge, cost column
  capture, prompt-size guard, dedupe merge-not-delete, zero-item RSS raise.
- `actionlint .github/workflows/production.yml` clean if you touch it.
- No behavior change for the happy path.
