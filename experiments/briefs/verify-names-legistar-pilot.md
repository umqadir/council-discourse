# Brief: name verification, Legistar enrichment, 3-meeting pilot, remote skeleton

Repo: council-discourse. GOOGLE_API_KEY in env; LEGISTAR_TOKEN in .env. Commit logically as you go. Keep total NEW Gemini spend under $4 (use the runlog cost tracking); if you'd exceed it, stop and report instead.

## Task A — witness name verification + honest eval tiers
1. pipeline/speakers.py: add a verification pass for named speakers NOT matched to the council-member roster (public witnesses, agency staff). Use Gemini WITH the built-in Google Search grounding tool (`tools: [{google_search: {}}]`) — one call for the batch of unverified names: give each name + role/org + a quote snippet, ask it to verify/correct spelling using search (e.g. ASR "Jeanne Ryan, Disabled In Action" → real person "Jean Ryan, Disabled In Action"). Apply corrections; record before/after in the artifact. This mirrors the original citymeetings v2 design (Google's spelling correction fixes mistranscribed names).
2. experiments/07: report TWO accuracy tiers: strict (exact after normalization) and same-person (nickname map + first-name edit-distance<=2 when last names match, or high-similarity full-name). Headline = same-person; spelling errors are the verification pass's job.
3. De-skew the reference: experiments/02 currently samples only a few chapters. Scrape ~10 more chapter transcripts for the transportation benchmark spread across the meeting (citymeetings-chapters.json has all chapter URLs) and rebuild the reference so no single speaker dominates >25% of matched utterances.
4. Re-run the eval once (single-pass + verification). Report both tiers.

## Task B — Legistar metadata enrichment
The registry currently keys meetings by viebit filename with no human metadata. Using LEGISTAR_TOKEN (see research/02 addendum):
1. In pipeline/legistar.py + discover.py: fetch NYC events for the relevant date window; join to viebit items via EventVideoPath filename (primary; 75% populated) with room+timestamp proximity as fallback. Store: legistar event id, body name, meeting datetime, agenda URL, minutes URL, EventInSiteURL, meeting type classification (STATED_MEETING / HEARING / VOTE / LAND_USE — infer from body name + agenda status like the original's tags).
2. Slugs: citymeetings-style `{yyyy-mm-dd-hhmm-am|pm}-{body-slug}` for meeting URLs/dirs; keep viebit filename as the stable internal key.
3. Backfill enrichment for the meetings already in the registry (June 2026 window).

## Task C — pilot batch (3 meetings, end-to-end local)
Pick from the June 2026 registry: (1) the already-fetched stated meeting NYCC-PV-CH-CHA_260528-100627 (finish any missing stages), (2) one typical committee hearing ~2-3h, (3) one short meeting (~1h, different body). Run full local pipeline: fetch → prepare → transcribe (parakeet-mlx) → speakers (single-pass + verification) → chapterize → export_site. Rebuild the Astro site. Report per-meeting wall-clock + Gemini cost from runlogs.

## Task D — remote-mode skeleton (do NOT over-build)
.github/workflows/pipeline.yml: cron (every 3h) + workflow_dispatch, ubuntu runner: discover + fetch + prepare stages always; transcribe/speakers/chapterize only if the needed API keys exist as secrets (remote ASR backend will be chosen tomorrow — make the transcribe backend pluggable with a clean interface: local=parakeet-mlx, remote=TBD, raise NotImplemented for now). Derived artifacts (JSON transcripts/chapters, meeting metadata — NOT audio/video) get committed back to the repo by the workflow. Site deploy step stubbed. Don't add secrets; don't enable anything that costs money. Keep it minimal and obviously readable.

## Acceptance
- Eval report with strict + same-person tiers, verification before/after table
- Registry rows show real body names/dates; slugs correct
- 3 meetings fully processed + site builds with them; costs reported
- Workflow YAML committed (inert without secrets)
- Final report: what's done, numbers, judgment calls needed, total new spend.
