# Convert Voxtral transcription to the Mistral Batch API (50% discount)

Production transcription (`pipeline/voxtral_prod.py`) currently POSTs each
30-minute audio chunk synchronously to `https://api.mistral.ai/v1/audio/transcriptions`
and pays $0.003/min. Mistral's batch API lists `/v1/audio/transcriptions` as a
supported endpoint at a 50% discount ($0.0015/min). Transcription is the
pipeline's largest cost line; convert it to batch mode. Read Mistral's current
batch docs (https://docs.mistral.ai/capabilities/batch/) for exact mechanics
before writing code — do not code from memory. You have network access; you may
curl the docs pages, but make NO paid API calls and create no real batch jobs.

## Required design (constraints from the existing hardening — do not regress)

1. **One batch job per meeting**, entries = the meeting's not-yet-transcribed
   chunks (`custom_id` = part index). Reuse the existing chunk split and the
   existing per-part resume: parts with a valid `voxtral-transcript-part-{i}.json`
   are excluded from the job. Keep `diarize`/timestamps/context-bias request
   params identical to the sync form data.
2. **Persist job state to disk immediately**: write
   `voxtral-batch-job.json` (job id, input file id(s), submitted_at, chunk
   custom_id map, request params hash) in the meeting dir as soon as the job is
   created, BEFORE polling. Add `voxtral-batch-job.json` to the R2 persist
   include list in `.github/workflows/production.yml`. On any later attempt,
   if this file exists and the job id is still valid (GET the job), RE-ATTACH
   and poll instead of creating a new job — never resubmit paid work. If the
   params hash mismatches current params, discard and resubmit.
3. **Pending is not failure.** Poll the job up to a stage budget
   (env `VOXTRAL_BATCH_POLL_BUDGET_SEC`, default 4500). If the job has not
   finished within budget, raise a dedicated exception type (e.g.
   `VoxtralBatchPending`). `process_one` must catch it and:
   - set the result status to `"pending"` (not `"failed"`), record a note,
   - NOT increment `process_attempts`,
   - NOT write `last_error`,
   - exit code 0 even with `--fail-on-error` (a slow batch is not an error).
   The workflow's zero-complete guard treats only status=="complete" as done;
   verify a batch-pending run does not trip it incorrectly: pending results
   with zero completes must NOT fail the run - adjust the guard step in
   production.yml so `results and not done` excludes runs where all
   non-complete results are status=="pending" (fail only if any "failed").
4. **On job completion**: download the output file, split per custom_id, write
   each part's `voxtral-transcript-part-{i}.json` exactly as the sync path
   does, then reuse the existing canonicalization (utterances.jsonl etc.)
   unchanged. Failed entries within an otherwise-successful job: write nothing
   for those parts; they re-enter the next job.
5. **Mode switch**: env `COUNCIL_VOXTRAL_MODE` = `batch` (default) | `sync`
   (keeps the existing code path untouched for fallback). The sync path must
   remain working.
6. **Cost-regression test**: a test asserting the production default mode is
   batch (so a silent revert to sync pricing fails CI).
7. Update the `VOXTRAL_USD_PER_AUDIO_HOUR` constant in `pipeline/config.py`
   to the batch rate (0.09) with a comment giving both rates and the mode
   dependency; make cost capture use 0.18 when mode is sync.

## Tests

Mock all HTTP (httpx). Cover: job creation payload shape (from the docs),
persist-then-poll ordering, re-attach on existing job file, params-hash
mismatch resubmit, pending exception -> process_one result "pending" with no
attempt increment, completed job -> part files identical in shape to sync
path, mode switch, cost-regression default-batch test. Keep the whole suite
green: `uv run pytest tests/ -q` (currently 66 passing).

`actionlint .github/workflows/production.yml` must pass if you touch it.
Repo style: no AI mentions anywhere. You cannot commit (sandbox); leave the
working tree clean and complete, and print a summary of files changed.
