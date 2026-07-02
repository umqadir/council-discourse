# How citymeetings.nyc Was Built — Research Synthesis

Creator: **Vikram Oberoi** (vikramoberoi.com, GitHub/gists: `voberoi`), a fractional-CTO consultant who built and ran the site solo. Best sources: his two NYC School of Data talks (2024 annotated-slides blog post; 2025 YouTube recording), his eval blog post, two GitHub gists containing his actual production prompts, and a long Maximum New York interview.

## 1. Timeline

| When | What |
|---|---|
| 2020 | First had the idea (GPT-3 era); not viable |
| Mar 2023 | GPT-4 launch; initial tests but API pricing too high out of pocket |
| Nov 2023 | GPT-4 Turbo made economics work; started "Keys to the City Council" newsletter mid-Dec 2023 (retired Feb 2024 — writing took too long) |
| Jan 19, 2024 | Prototype blog post ("UX-centric approach") |
| Feb 26, 2024 | citymeetings.nyc launched |
| Mar 2024 | NYC School of Data talk; post went viral (N.K. Jemisin repost; HN traction negligible) |
| Apr–Aug 2024 | **Pipeline collapse.** "My tools started to fail... April through August I was banging my head on a keyboard." Complete methodology overhaul, solo |
| Mid-Aug 2024 | First same/next-day publishing; consistent since Sept 2024 (43 meetings that month) |
| Oct–Dec 2024 | NY1 coverage; Hell Gate profile |
| ~Feb–Mar 2025 | Speaker-ID v2 built on Claude 3.7 Sonnet (first model to pass his long-context tests) |
| 2025 | NYC Charter Revision Commission **contracted** citymeetings (first revenue; $0 before that) |
| Jan 2026 → now | **Dormant**: newest meeting is Jan 7, 2026 stated meeting. No public shutdown announcement. "PRO on hold indefinitely"; Mar 2025: "my current prioritization is just staying alive" financially |

## 2. Pipeline architecture

### Overall flow (constant across versions)
1. Video/records from **Legistar**. 2. **Deepgram** for transcription **and diarization**. 3. AI speaker identification → human review/fix. 4. AI chapter extraction → human review/fix. 5. Summary generation → edit → publish. Human-in-the-loop at every stage: "I don't rely on the AI to create the output entirely."

**Why Deepgram, not Whisper:** "Deepgram is pretty cheap and they're good, like frontier performance. I don't use Whisper because it just takes too much work to stitch it together and then host it." Tested Whisper and Pyannote and rejected them. Transcripts run 10K–100K tokens, averaging 40–50K.

### Chapter extraction v1 (Dec 2023 – Apr 2024) — the version that failed
Three chained LLM steps over ~8–10K-token transcript chunks (prompts: gist.github.com/voberoi/cfeb935b163c150eee5d7c86e7fb4337):
1. **Find transcript markers** — starts of four hardcoded chapter types: QUESTION, TESTIMONY, OPENING_STATEMENT, PROCEDURE.
2. **Create chapters** — per-type prompts determine endpoint using **custom time markers** (`[T354]`-style sequential tokens injected into transcript) because GPT-4 **hallucinated timestamps** but reliably copies constrained marker tokens.
3. **Write/refine titles & descriptions** — per-type formatting rules.

Structured output via **`instructor`** (Pydantic schemas, field validators).

**Why it failed** (his postmortem): (a) assumptions too rigid — e.g. Gale Brewer asks 10 rapid-fire questions → 10 useless chapters; (b) **overfit to ~5 meetings**; (c) **errors compound across unsupervised multi-step chains** — "if there are errors in step one, by step three you just get garbage."

### Chapter extraction v2 (Aug 2024 – present) — human-guided chunking
Insights: a general "break this into useful chapters" prompt works if given a **small coherent chunk** (one member holding the floor; one testimony), and meetings are procedural enough that such chunks are abundant. Workflow:
1. Human marks **major sections** in a custom UI (opening remarks / agency testimony / council questioning / public testimony; land use and stated meetings differ) — 30 sec to 5 min per meeting.
2. AI finds every floor-change / new-testifier within each section; human quickly fixes.
3. AI generates chapters + titles + summaries per chunk; human reviews on a chapter-timeline UI.
4. Every chapter prompt also gets **broad meeting context** (pre-extracted "terms of art and entities") — fixed context-lacking errors (e.g. resolving mis-transcribed "Nissan Hirs" to East Elmhurst).

### Speaker identification v1 (2024)
One speaker label at a time. 8 "instances" (utterances with neighboring dialogue) in an ~8K-token prompt (gist.github.com/voberoi/3d82f6b2a55e79b7cd014847853be8bf). Includes: **CSV of all council members** (name, district, role, neighborhoods, party) to anchor names against mistranscriptions; delimiter-forced chain-of-thought; inference-rule hierarchy (self-intro > third-party intro > speech content > fallback labels); 5 worked examples incl. roll-call diarization-error case. Accuracy **35–50% → 80–90%** via days of systematic eval iteration.

### Speaker identification v2 (Mar 2025) — agentic, Claude 3.7 Sonnet
Kept re-running "full 50–100K-token transcript, identify one speaker label per pass" against each new model; Claude 3.7 Sonnet first to pass. Loop per label: (1) model analyzes full transcript, proposes name/role/org **plus web search queries to verify**; (2) code runs Google searches (leaning on Google's spelling correction to fix mistranscribed names); (3) model final answer; (4) separate **LLM-judge step** re-checks using old-style reduced-context chunks (deliberately different context = independent perspective) and flags for human review. Review UI embeds search-result screenshots. He still reviewed everything.

## 3. Models and costs
- **GPT-4** prototype (Jan 2024); **GPT-4 Turbo** production 2024 (128K context; GPT-3.5 inadequate).
- **Claude 3.5 Sonnet** for July 2024 talking-points side project (~**$40** to analyze 203 testimonies from one 15-hour hearing).
- **Claude 3.7 Sonnet** for speaker-ID v2 (2025). "Claude tends to outperform for a lot of practical tasks."
- **Reported cost: $5–$10 per meeting** for speaker ID + chapter extraction (Mar 2024, unoptimized). Deepgram for cheap ASR.
- Manual labor: **10–30 min chapter review per meeting** (2024); speaker verification the bottleneck — up to **200 names/day** across ~5 meetings; worst case **336 speakers** in one meeting (City of Yes day 2).

## 4. Tech stack
Nothing of citymeetings is open source (only the two prompt gists + talking-points dataset).
- **Pipeline:** Python; `instructor`; OpenAI + Anthropic + Deepgram APIs; runs driven from a terminal.
- **Custom internal tools** (the real moat): section-marking UI over video+transcript, chapter-timeline editor, speaker-review panel with per-speaker playback + eval flags, an afternoon-built eval UI.
- **Website:** BunnyCDN; htmx + Alpine.js; Django (inferred from headers). **SEO first-class** — chapter pages "stand on their own"; 50%+ of traffic organic search. Site search is essentially navigable indexes, no search engine.

## 5. What he said was hard (lessons)
1. **Long context / lost-in-the-middle** — the defining GPT-4-Turbo-era constraint; forced all chunking machinery. Only in 2025 did models reason acceptably over full transcripts.
2. **Hallucinated timestamps** — solved with sequential time-marker tokens.
3. **Rigid taxonomies fail** — hardcoded chapter types broke on real meeting diversity. Better: general prompts over human-marked coherent chunks.
4. **Overfitting methodology to a handful of meetings** + error-compounding across unsupervised chains = the 2024 collapse.
5. **Speaker ID is harder than chapter extraction**: diarization errors (roll calls pathological), mistranscribed names ("NYCHA" → "Nitro"; "Ossé" → "Jose"), introductions 10–15 min before the person speaks.
6. **Evaluation discipline is highest-leverage**: classify every error into failure classes, fix each class with targeted prompt changes (few-shot examples "highest leverage"); manually created ~300 chapters to learn what "good" looks like. By 2025: battery of assumption tests run against every new model release.
7. **Summary error taxonomy**: outright hallucination (rare), contextual hallucination (subtle), "a human would fail too" (fixed by broad meeting context). Bar: prevent meaning-inverting errors pre-publish; accept name misspellings fixed on report.
8. **Trust is the product**: "an element of exactness is required for people to build trust." Reviewed every chapter for the first year.

## 6. Numbers
- Mar 2024: 80+ meetings, 150+ hours video, 3,000+ chapters. Chapters 30 sec–5 min; a 7-hour meeting navigable in ~15 min.
- Sept 2024: 43 meetings in first full same-day month.
- Mar 2025: 10,000+ monthly visitors, 50%+ organic search; newsletter 900+ subscribers (~half professionals; 28 agency domains); users ≈30% government, ≈30% advocacy/lobbying. Onboarding a new body: "about two weeks."

## 7. Sources
- Blog: vikramoberoi.com/posts/a-ux-centric-approach-to-navigating-city-council-hearings-with-llms/ · .../why-you-should-regularly-and-systematically-evaluate-your-llm-results/ · .../how-citymeetings-nyc-uses-ai-to-make-it-easy-to-navigate-city-council-meetings/ · .../whats-in-the-nyc-city-councils-public-records/ · .../can-you-figure-out-where-peoples-talking-points-com-from-with-llms/
- Prompts: gist.github.com/voberoi/cfeb935b163c150eee5d7c86e7fb4337 (chapters) · gist.github.com/voberoi/3d82f6b2a55e79b7cd014847853be8bf (speaker ID)
- Talks: youtube.com/watch?v=M9ZpuLJgyeU (2025, richest source) · citymeetings.nyc/nyc-school-of-data-2024/
- Interviews: maximumnewyork.com/p/citymeetings-interview · NY1 (Oct 2024) · Hell Gate (Dec 2024, paywalled)
