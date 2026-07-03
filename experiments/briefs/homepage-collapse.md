# Make the meeting list the homepage; kill the standalone homepage

## Context
Site = Astro static in site/. We cover ONLY the NYC City Council, so the current
homepage (hero, tabbed recent meetings, view-all links) is pointless indirection —
user verdict: "junk". The meeting list currently at /meetings/new-york-city-council/
becomes the homepage.

## Task
1. `/` now renders the full reverse-chron meeting list (the current body list page,
   with its tag filters + truncated summaries) with a compact header: site name,
   one-line tagline, nothing else above the list. Keep the small newsletter
   placeholder box but move it to the footer area or a slim aside — not a hero.
2. /meetings/new-york-city-council/ must permanently redirect to / (Cloudflare Pages
   supports a _redirects file emitted into dist; also add <link rel=canonical>).
   Meeting/chapter URLs under /meetings/new-york-city-council/... stay unchanged.
3. Breadcrumbs: drop the "New York City Council Meetings" level everywhere; meeting
   pages breadcrumb = Home / {Committee — date}; chapter pages = Home / {meeting} / {chapter}.
4. Update nav (remove Meetings link or point it at /), sitemap, OG for the homepage
   (reuse existing home card), and any internal links to the old list URL.
5. Verify: pnpm build; check dist/index.html is the list, dist/_redirects present,
   spot-check a chapter page breadcrumb. Keep design language identical otherwise.

## Constraints
Only site/ + pipeline/export_site.py if link data requires it. No deploy (supervisor
deploys). .git read-only — list changed files. A tranche job is running that deploys
at its end — irrelevant to your file edits.
