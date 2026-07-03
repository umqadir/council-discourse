# Constant-size deployments: edge-render chapter pages + OG images

## Problem
Cloudflare Pages free plan caps 20,000 files/deployment. We emit ~1 HTML + 1 OG PNG
per chapter (~100 chapters/meeting, ~40 meetings/mo) -> cap breached in months. Fix
architecturally now, not later.

## Design (decided)
- Astro hybrid rendering with @astrojs/cloudflare adapter on Pages Functions (same
  free platform).
- STATIC (prerendered): homepage/meeting list, meeting pages, about/faq, meeting OG
  images. These number in the hundreds/low thousands per year — fine.
- EDGE-RENDERED on request (ssr routes): chapter pages (/meetings/.../chapter/{slug}/)
  and chapter OG images (/og/chapter/...png). URLs unchanged. Data source: per-meeting
  chapter JSON uploaded to the EXISTING R2 bucket (council-discourse-videos or a new
  prefix data/) by export-site; the function fetches via R2 binding (free ops), renders,
  and uses the Cache API for edge caching (cache key = URL, long TTL, cache-busted by
  a content hash query or version in the JSON path). Target: cached hits don't invoke
  the function.
- OG at the edge: satori + resvg-wasm (workers-og or equivalent) reusing the existing
  card design in site/src/lib/og.ts. Font embedding must work in workers runtime.
- Meeting pages keep their static chapter LIST (cards) — only the chapter detail
  pages move to SSR.

## Task
1. Implement per the design. wrangler.toml / Pages config for the R2 binding
   (bucket binding name e.g. DATA; supervisor will set the binding on the Pages
   project — emit exact `npx wrangler pages ...` or dashboard-free commands needed
   and list them in your report).
2. export_site.py: also emit per-meeting chapters JSON artifacts and upload-ready
   layout (the workflow's existing rclone R2 step can sync them — extend the
   workflow sync path if straightforward).
3. Local verification: `astro build` with the adapter; `wrangler pages dev` locally
   to prove a chapter page + chapter OG render from local R2 simulation (wrangler
   dev local persistence is fine for the test). pytest green. Confirm dist file
   count now scales with meetings only (report the count).
4. SEO essentials preserved on SSR pages: full HTML (no client-side data fetch for
   content), canonical, og/twitter meta pointing at the edge OG route, sitemap still
   lists chapter URLs (sitemap generation must include them without emitting files).

## Constraints
Only this repo. No deploy (supervisor deploys). .git read-only — list changed files.
Don't touch pipeline/voxtral_prod.py or speakers.py. A local tranche job is running
(data/ writes + one deploy at end) — irrelevant to your edits but do not run export yourself.
