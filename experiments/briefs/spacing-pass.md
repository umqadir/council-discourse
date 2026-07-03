# Spacing/positioning pass (queued behind edge-restructure run)
User-confirmed issues (see conversation 2026-07-03):
1. Meeting page: summary rail too narrow (~260px) -> ~400px; drop quote-bar.
2. Header block doesn't share the content grid; dead air right of title.
3. Chapter cards: duplicate type label (fix committed, verify live), timestamp
   column visually detached (restore tie), title/summary/divider rhythm inverted.
4. Homepage rows: date shown twice (gutter + under title) -> once.
5. CHAPTER TYPE FILTERS: currently buried below summary in left rail, invisible
   until scrolled + not visibly associated with the chapter list. Move to a
   horizontal chip row directly ABOVE the chapter list (sticky within column ok);
   rail keeps summary only.
6. Summary text normalization: ASR artifacts like "$125. 8 billion" -> "$125.8
   billion" (regex pass in export or chapterize output cleaning).
7. TRUNCATION (user-refined): do it the idiomatic CSS way, not server-side
   chopping. Use CSS line-clamp with UP TO 6 LINES (line-clamp-6) wherever
   summaries preview (homepage rows, chapter cards) — browser handles the
   ellipsis and adapts to the actual rendered width/window dynamically.
   Server side: ship the full summary text (or a very generous sentence-safe
   cap only to keep HTML lean); DELETE the naive firstSentences() period-split
   ("$194.5M" -> "$194." bug) or fix its splitter if any semantic use remains.
8. STATIC-RECENT-WINDOW (user-decided): the edge-restructure made ALL chapter pages
   SSR. Refine: prerender chapter pages + chapter OG images STATICALLY for the N
   most recent meetings (N sized so dist stays well under ~15k files; likely last
   ~60-90 days of meetings), older chapters fall through to the existing SSR route
   at identical URLs. Recent = where traffic is; static = fast + no function calls.
