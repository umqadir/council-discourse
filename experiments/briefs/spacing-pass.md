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
