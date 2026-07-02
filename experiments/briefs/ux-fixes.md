# Site fixes: body taxonomy, summary truncation, residue copy, video base URL

## Context
PLAN.md = source of truth. Site = Astro static in site/, data emitted by pipeline/export_site.py. A design review found these issues (screenshots verified in browser):

## Tasks
1. **Body taxonomy bug.** Committee meetings currently export under /meetings/committee-on-finance/... as if the committee were a body. Match the original citymeetings model: ALL NYC City Council meetings (stated + every committee/subcommittee) live under /meetings/new-york-city-council/{yyyy-mm-dd-hhmm-am-slug}/. On meeting pages: eyebrow = body ("New York City Council"), h1 = committee/meeting title. Meeting list page = one reverse-chron list for the body. Breadcrumbs follow. (Registry has body_name per meeting from Legistar; committee name != body. Body inference: NYC Council rooms → "New York City Council".)
2. **Summary truncation.** Homepage and meeting-list rows must show only the first 2-3 sentences of the meeting summary (truncate at sentence boundary, ~300 chars max, no mid-word cuts). Full summary stays on the meeting page.
3. **Remove process-residue copy** on homepage: "Benchmark meetings currently loaded from local pipeline artifacts." Replace the Recent meetings subtitle with plain user-facing copy or nothing.
4. **Prev/next chapter navigation**: chapter pages need Previous/Next chapter links (title + arrow) at the bottom, like the original. Add if missing.
5. **Video base URL**: export currently emits local /videos/... paths. Add VIDEO_BASE_URL env (default empty = keep local path for dev). When set, chapter/meeting pages point the player at {VIDEO_BASE_URL}/{meeting_key}/video-web.mp4. Production value will be https://pub-19a0d24fc348462cb48cf2c7554116df.r2.dev . Keep the poster/placeholder behavior when video missing.
6. Re-export + rebuild to verify: run `uv run python -m pipeline export-site` then `cd site && pnpm build` — both must succeed; report the new URL for the finance meeting.

## Hard constraints
- Only this repo; no browser/MCP; no Metal jobs; .git read-only (no commits — list changed files).
- Don't redesign styling — these are surgical fixes; the current design passed review.
- Keep benchmark meetings (2025-04-23, 2025-04-24) in the export.
