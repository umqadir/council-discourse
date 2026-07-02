# Task: static site skeleton (design locked — implement faithfully)

Read AGENTS.md and PLAN.md first. Build under `site/`. This brief is the design
authority; where it's silent, match citymeetings.nyc's information architecture
(reference HTML snapshots in `data/benchmark/*/citymeetings-*.html`).

## Stack (decided)
- Astro (static output), Tailwind. Alpine.js only where interactivity is needed
  (filters, tabs). No React/SPA. Node 22, pnpm.
- Data source: `site/src/data/` reads build-time JSON produced by the pipeline
  (schema below). For now, generate the site from the two benchmark meetings using
  a small adapter script that converts `data/benchmark/*/` artifacts into the schema.
- Video: plain video.js player streaming the viebit MP4 URL directly
  (`https://vbfast-vod.viebit.com/counciln/{hash}/{file}.mp4`), seeked via
  `currentTime` on load and on transcript-timestamp clicks. No Mux.
- Deploy target: Cloudflare Pages (just make `pnpm build` produce `dist/`; deploy
  config comes later).

## Data schema (per meeting JSON)
{ slug, body, title, date, time, duration_sec, video: {url, poster},
  summary: [str], tags: [HEARING|VOTE|STATED_MEETING|LAND_USE],
  chapters: [{ id, slug, type, title, summary, start_sec, end_sec,
               utterances: [{t_sec, speaker, text}] }] }

## Pages
1. `/` homepage: recent meetings list (tabbed later; single list fine now),
   short mission statement, newsletter placeholder box.
2. `/meetings/{body-slug}/` meeting list: reverse-chron, one row per meeting
   (tag badge, linked title, date • time • duration, 2-3 sentence summary),
   client-side tag filter checkboxes with counts (Alpine, no server).
3. `/meetings/{body-slug}/{meeting-slug}/` meeting page: header (body, title,
   date/time/duration), summary bullets, chapter-type filter checkboxes w/ counts,
   chapter cards (badge, title, truncated summary, start H:MM:SS, duration).
4. `.../chapter/{chapter-slug}/` chapter page: header + badge + start/duration,
   video player seeked to chapter start (paused), Summary | Transcript tabs
   (Alpine), transcript = utterance blocks with speaker name bold + clickable
   timestamp that seeks the player, prev/next chapter links footer.
5. `/about/`, `/faq/` from markdown stubs.

## Design language (do NOT ape citymeetings' visuals; this is the improved UX)
- Typography-first, newspaper-adjacent: a strong serif for headings (e.g. Source
  Serif 4 / Newsreader via fontsource), humanist sans for body UI (e.g. Inter).
  Generous line-height, max-w-prose transcript column.
- Near-monochrome palette: warm off-white background, near-black text, ONE accent
  (deep civic blue) for links/active states. Chapter-type badges: muted tinted
  pills (not saturated rainbow).
- Meeting/chapter pages must remain information-dense: no hero sections, no cards
  with heavy shadows, no marketing air. Scanability beats decoration.
- Timestamps in tabular figures. Mobile: single column, filters collapse into a
  disclosure.
- SEO: per-page <title>/description, OpenGraph tags (og:image can be a static
  placeholder for now), semantic headings, sitemap generated at build.

## Acceptance
- `pnpm build` clean; both benchmark meetings fully browsable locally including
  working video seek from transcript timestamps (verify viebit URL plays in dev).
- Lighthouse-sane HTML (no giant JS bundles; Alpine + video.js only where used).
- Commit incrementally with clear messages.
