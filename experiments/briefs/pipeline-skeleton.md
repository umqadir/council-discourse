# Task: ingestion pipeline skeleton

Read AGENTS.md and PLAN.md (§2 pipeline). Build under `pipeline/`. Python, uv-style
(create `pyproject.toml` at repo root with a `pipeline` package if cleanest, or keep
plain scripts + shared module — your call, keep it simple and idiomatic).

## Scope (this task)
Stages: discover → fetch → prepare. The ASR/LLM stages are NOT in scope (benchmark
pending) — define their interfaces and stub them.

1. **Meeting registry**: single SQLite db (`data/registry.db`, gitignored) with a
   `meetings` table keyed by legistar EventId (nullable for videos with no event
   match) + viebit filename. Track status per stage (discovered/fetched/prepared/...),
   timestamps, and source metadata (body name, event date/time, agenda PDF url,
   InSite url, viebit hash/filename/pubDate, duration).
2. **discover** command:
   - Poll viebit RSS (https://councilnyc.viebit.com/rss.xml): items = {title:
     FILENAME.mp4, guid: HASH, pubDate}. Upsert.
   - Legistar API events sync using EventLastModifiedUtc cursor — token read from
     env LEGISTAR_TOKEN; if unset, SKIP gracefully (we're waiting on the token).
     Client 'nyc', base https://webapi.legistar.com/v1/nyc.
   - Join: for events, fetch InSite MeetingDetail page and decode the
     `Video.aspx?Mode=Auto&URL=<base64>` link → viebit filename (see
     experiments/01_fetch_benchmark_data.py for the working pattern, incl. the
     /vod/ → /embed/vod redirect and player.php hash extraction). Backstop join:
     room-prefix + YYMMDD-HHMMSS filename timestamp vs event date/time.
3. **fetch** command (per meeting or --all-pending): download VTT + thumbnail jpg,
   download MP4 to a workdir, extract audio.m4a (aac copy) + audio-16k.wav, probe
   duration, then delete the MP4 (keep audio only). Layout:
   `data/meetings/{meeting-key}/...`. Resumable/idempotent (skip existing, curl
   --retry, atomic renames). Also fetch agenda PDF when present.
4. **prepare** command: VTT → captions-clean.jsonl (reuse/port logic from
   experiments/03_prepare_texts.py), agenda PDF → text (pdftotext).
5. **Stubs with typed interfaces**: transcribe(meeting) -> utterances.jsonl,
   name_speakers(meeting) -> utterances-named.jsonl, chapterize(meeting) ->
   chapters.json. Each reads/writes files in the meeting dir; raise NotImplemented.
6. **CLI**: one entrypoint (`python -m pipeline` or `pipeline/run.py`) with
   subcommands discover/fetch/prepare/status. `status` prints a table of meetings
   and stage states.
7. **Tests**: a couple of unit tests for the joins/parsers using small fixtures
   (record a trimmed rss.xml + a saved MeetingDetail HTML fragment as fixtures).
   No network in tests.

## Acceptance
- `discover` (without LEGISTAR_TOKEN) populates registry from live RSS.
- `fetch --limit 1` on the OLDEST rss item completes end-to-end (a real ~1-3GB
  download — fine, do it once), `prepare` produces clean captions, `status` shows it.
- Idempotent re-runs are no-ops. Committed with clear messages. Do NOT commit data/.
