# citymeetings.nyc — Complete Feature Inventory

Crawled July 1, 2026 (~15 URLs fetched, plus the full 27,073-URL sitemap, JS bundles, and htmx fragment endpoints). `/search?q=housing` returns a hard 404 — everything else fetched cleanly.

## 1. Site map (every page type)

| Page type | URL pattern |
|---|---|
| Homepage | `/` |
| Meeting list (per body) | `/meetings/new-york-city-council/`, `/meetings/nyc-charter-revision-commission/` |
| Meeting detail | `/meetings/{body-slug}/{yyyy-mm-dd-hhmm-am|pm}-{meeting-slug}/` |
| Chapter detail | `/meetings/{body}/{meeting}/chapter/{chapter-title-slug}/` |
| City of Yes special hub | `/city-planning-commission/2024-07-10-city-of-yes-public-hearing/` |
| City of Yes testimony segment | `/city-planning-commission/2024-07-10-city-of-yes-public-hearing/{speaker-slug}/` (no `/chapter/` infix) |
| About | `/about/` |
| FAQ | `/faq/` (anchors: `#report-an-issue`, `#transcription-errors`) |
| Request coverage | `/request-meeting-coverage/` (embedded Tally.so form, `tally.so/embed/mYrvbd`) |
| htmx fragments (not pages) | `{meeting}/get-chapters/`, `/meetings/{body}/filter-meetings/`, `POST /submit-beg-wall/` |
| Infra | `/robots.txt` (disallows `/admin` — Django admin hint), `/sitemap.xml` (6.6 MB, single file) |

There are **no** person/council-member pages, no topic pages, no site search page, no login/account, no paid features, no public API, no site RSS (newsletter RSS exists at Buttondown: `buttondown.com/citymeetingsnyc/rss`, archive at `/citymeetingsnyc/archive`).

Global chrome on every page: pigeon-at-podium logo, tagline "Your guide to NYC's public proceedings," nav menu (Meetings → New York City Council / NYC Charter Revision Commission / City of Yes / Request Coverage; About; FAQ), breadcrumbs, newsletter signup (Buttondown embed form, `POST buttondown.com/api/emails/embed-subscribe/citymeetingsnyc`), and a "beg-wall" engagement widget ("Is citymeetings.nyc useful to you?" → one-question survey: Government / Advocacy / Public Affairs / Media / Other + free text, Submit/Skip, then newsletter pitch; posts to `/submit-beg-wall/`).

## 2. Per-page-type features

**Homepage** — hero pitch, social proof (press logos: Hell Gate, City & State, NY1, Maximum New York; testimonials from Zach Seward/NYT, N.K. Jemisin, CM Jennifer Gutiérrez), newsletter box, tabbed recent-meetings list (All / NYC Council / Charter Revision Commission) with ~5 most recent meetings, "view all meetings" links.

**Meeting list page** — ALL meetings on one server-rendered page (~900 KB for 468 council meetings), reverse-chronological. Per row: meeting-type tag badge, linked title (committee name), date/time, duration, 2–3 sentence AI summary. Filter UI: checkboxes by meeting tag with counts — HEARING (280), VOTE (139), STATED MEETING (30), LAND USE (18) — plus "Deselect All"; filtering re-fetches the list via htmx `GET .../filter-meetings/?tag-HEARING=...`. No pagination, no date filter, no text search. CRC list page is identical minus tag filters (13 meetings).

**Meeting detail page** — NO video player on this page. Shows: breadcrumb, body name, committee/meeting title, date, time, duration (e.g. "5 hr 51 min"), bulleted "Summary" section, then "Meeting chapters" ("Read summaries and watch videos for short segments that matter to you"). Chapter-type filter checkboxes with counts (e.g. AGENCY TESTIMONY (27), Q&A (104), REMARKS (23)) + Deselect All, re-rendered via htmx `GET {meeting}/get-chapters/?chapter-type-X=...` into `#chapter-list`. Each chapter card: type badge, title (headline-style, names the speaker and what happens, e.g. "Speaker Adams advocates for increased parks funding"), truncated summary, start timestamp (h:mm:ss), duration in seconds, clock icon. Cards link to chapter pages; each has a numeric `data-chapter-id`. Observed chapter types: REMARKS, INVOCATION, VOICE_VOTE, VOTE_OUTCOME, AGENCY_TESTIMONY, Q&A, TESTIMONY, plus "unlabeled". Notably absent: links to Legistar, agendas, or source documents; no share buttons; no full-meeting transcript view or download.

**Chapter detail page (the core experience)** — breadcrumb, type badge, chapter title, start time + duration, "Report an issue" / "About transcription errors" links (→ FAQ anchors), video player, then a **Summary | Transcript** tab toggle (Alpine.js). Summary = paragraph + bullets. Transcript = the chapter's portion only, segmented by utterance with **speaker names (diarized and human-identified, e.g. "Letitia James", "Althea V. Stevens")** and a timestamp per utterance; **every timestamp is a click-to-seek control** (`@click="player.seekTo(547.895)"` — absolute seconds into the full meeting video). Footer: "Next Chapter →" link (prev/next navigation). OG/Twitter meta with **dynamically generated share card images** from a dedicated service: `ogloji.citymeetings.nyc/og-image/og-layouts/chapter/?mt=...&mdt=...&bn=...` (meeting type, datetime, body name, chapter title baked into the image).

**Video mechanics** — the player is a shared abstraction (`video-player.js`) supporting two backends chosen by `data-player-type`: (a) **video.js + videojs-offset plugin playing a Mux HLS stream** (`stream.mux.com/{playbackId}.m3u8`) — one full-meeting stream per meeting; each chapter page initializes `meetingChapter(chapterId, startSec, endSec)`, seeks to the chapter start (paused, no autoplay), and transcript clicks seek within the same stream; (b) **YouTube iframe API** (`data-youtube-video-id`, seekTo + 250 ms polling) for YouTube-sourced content. So yes — chapters and transcript timestamps jump the embedded video to exact offsets, within a self-hosted (Mux) copy of the meeting video rather than the city's player.

**City of Yes hub page** (one-off editorial format) — overview stats ("Out of 203 testimonies, 113 were in support and 90 were in opposition"), curated quick links (chair's opening/closing remarks, DCP presentation), correction instructions, and an **interactive DataTables (jQuery) table of all 203 testimonies** with columns: Name, For/Against, Borough, Neighborhood/Area, Stated Affiliations, Elements Discussed (policy tags like UAP, ADU, Parking Mandates, Transit-Oriented Development), Start Time — sortable/searchable client-side, each row linking to a testimony segment page. The dataset is **open-licensed (CC) and published on GitHub**: `github.com/citymeetingsnyc/cpc-city-of-yes-housing-opportunity-testimony-data`. Testimony segment pages are chapter pages with an extra tab set: **Summary | Key Points | Transcript | Elements Discussed**.

## 3. Data coverage (from full sitemap analysis)

- **482 meetings total**: 468 NYC City Council, 13 NYC Charter Revision Commission (2025, a paid contract per the About page), 1 City Planning Commission hearing (City of Yes, July 10 2024). No community boards, no MTA, no other cities.
- **~26,600 chapters** (26,383 council/CRC chapter URLs + 203 CPC testimony pages) — averages ~55 chapters per meeting.
- **By year**: 2023: 2 meetings (earliest = Aug 3, 2023), 2024: 263, 2025: 215, 2026: 1.
- **Most recent meeting: January 7, 2026** (Council charter/stated meeting — Speaker election). **Dormant since: nothing in ~6 months** as of July 1, 2026; sitemap and homepage confirm no newer content. Effectively full coverage ran Jan 2024 – mid 2025, tapering after.
- Sources per FAQ: Council meetings from **Legistar**; CPC from **YouTube**.

## 4. Pipeline / operations (About + FAQ)

Built and run solo by **Vikram Oberoi** (vikram@citymeetings.nyc). Launched Feb 2024. Transcription + diarization via **Deepgram**; chaptering/summaries via **LLMs with human review at each pipeline stage** (custom internal tooling — described in his NYC School of Data 2024 talk, annotated slides on his blog). Since Aug 2024, meetings published within 24 hours. No systematic manual transcript review; errors fixed on email report. Corrections policy: fixes misrepresentations, name misspellings, omissions; does not fact-check claims. Coverage expansion requests collected via Tally form with voting.

## 5. Tech stack (visible)

- Server-rendered HTML, almost certainly **Django** (`/admin` in robots.txt, Django-signature security headers, trailing-slash URLs).
- **htmx** (fragment swaps for chapter/meeting filters) + **Alpine.js** (tabs, menus, beg-wall) — no SPA framework, no build-heavy frontend. Tailwind CSS.
- Video: **Mux** (HLS hosting of full meetings) + video.js + videojs-offset; YouTube iframe API fallback.
- CDN: **BunnyCDN** (HTML edge-cached; static assets on `s.citymeetings.nyc` under a git-SHA path prefix).
- Analytics: **PostHog + Plausible** (custom `EventTracker` events, e.g. begwall submit/skip).
- Newsletter: **Buttondown**. Forms: **Tally.so**. OG images: custom microservice (`ogloji.citymeetings.nyc`). Data publishing: GitHub org `citymeetingsnyc`.

## 6. What's hard/expensive to replicate

1. **The data pipeline, not the site.** The site itself is a modest Django+htmx app (a few templates, two htmx endpoints). The moat is transcription (Deepgram), speaker identification (mapping diarized voices to named officials), LLM chaptering with accurate titles/summaries/type labels, and the human-in-the-loop review tooling that makes one person able to publish within 24h.
2. **Video hosting cost**: every meeting is re-hosted as a full-length Mux HLS stream (hundreds of multi-hour videos) — Mux encoding/streaming for ~2,000+ hours of video is a real recurring cost. An alternative is the YouTube-embed path the site already supports.
3. **Per-chapter precise offsets** (start/end to the millisecond) wired into player seeks and per-utterance transcript timestamps.
4. **Dynamic OG-image generation service** for shareable chapter cards.
5. Notably absent features NOT needed for parity: no search, no accounts, no API, no per-person pages, no pagination — the entire browse model is one big filterable list page + chapter pages.
