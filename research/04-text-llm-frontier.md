# Price-Performance Frontier for Text LLMs — Structured Document Processing (July 2026)

Research for the pipeline: chaptering, speaker ID, Q&A extraction, summarization over 30k–100k-word meeting transcripts. All prices verified July 1, 2026. USD per 1M tokens, input/output, standard tier unless noted.

## 1. Frontier and mid-tier (Western vendors)

### OpenAI
| Model | Input / Output | Context | Notes |
|---|---|---|---|
| GPT-5.5 | $5.00 / $30.00 | ~1.05M | Flagship; >272k-token prompts billed 2x in / 1.5x out |
| GPT-5.5-Pro | $30 / $180 | — | Heavy reasoning, irrelevant here |
| GPT-5.4 | $2.50 / $15.00 | 400k | Prior flagship, mid-tier workhorse |
| GPT-5.4-mini | $0.75 / $4.50 | 400k | |
| GPT-5.4-nano | $0.20 / $1.25 | 400k | Cheapest OpenAI |

GPT-5.6 family in limited preview — not GA, skip. No GPT-5.5-mini exists. Batch = 50% off.

### Anthropic
| Model | Input / Output | Context | Notes |
|---|---|---|---|
| Claude Fable 5 | $10 / $50 | 1M | New flagship |
| Claude Opus 4.8 | $5 / $25 | 1M | |
| Claude Sonnet 5 | **$2 / $10 intro through Aug 31, 2026**, then $3 / $15 | 1M | Best Anthropic value |
| Claude Sonnet 4.6 | $3 / $15 | 1M | |
| Claude Haiku 4.5 | $1 / $5 | 200k | Cheap tier |

Gotchas: (a) Fable 5 / Sonnet 5 / Opus 4.7+ tokenizer produces ~30% more tokens — effective cost ~1.3x sticker; (b) 1M context flat (no surcharge), batch 50% off, cache reads 10% of input.

### Google
| Model | Input / Output | Context | Notes |
|---|---|---|---|
| Gemini 3.5 Flash | $1.50 / $9.00 | 1M | GA May 19, 2026; workhorse |
| Gemini 3.1 Pro (preview) | $2 / $12 (≤200k); $4 / $18 (>200k) | 1M+ | |
| Gemini 3.1 Flash-Lite | $0.25 / $1.50 | 1M | Very cheap long-context |

Gemini 3.5 Pro announced, NOT GA (slipped to July 2026) — don't plan around it. Batch 50% off. (Earlier check of pricing page: Gemini 2.5 Flash $0.30/$2.50 w/ audio input $1.00/M; 2.5 Flash-Lite $0.10/$0.40, audio $0.30/M; audio = 25 tokens/sec.)

### xAI
Grok 4.3 (Apr 2026): $1.25/$2.50, 1M context. Grok 4.1 Fast: $0.20/$0.50, 2M context. Up to $175/mo free API credits via data-sharing program (transcripts are public data — probably acceptable).

## 2. Cheap/small tier — is it enough?

2026 community consensus: small models are the *default* for structured output / JSON workloads; Haiku 4.5 / Gemini Flash class interchangeable on general workloads; differentiator is context window. Original citymeetings ran on GPT-4-Turbo — every 2026 cheap-tier model exceeds that class. Open question is long-context degradation, not raw ability.

## 3. Chinese / open-weight models

| Model | Version | Input / Output | Context | Notes |
|---|---|---|---|---|
| DeepSeek V4 Flash | (old names retire Jul 24, 2026) | $0.14 / $0.28 | 1M | Cache-hit input $0.0028; JSON + tools |
| DeepSeek V4 Pro | flagship | $0.435 / $0.87 promo | 1M | 384k max output |
| GLM 5.2 (Z.AI) | Jun 2026, 744B MoE | ~$1.40 / $4.40 | 200k | Leads AA Intelligence Index among open weights |
| Kimi K2.6 (Moonshot) | K2.6 general | ~$0.95 / $4.00 | 256k | ~1T MoE |
| MiniMax M3 | Jun 1, 2026 | ~$0.60 / $2.40 | 1M | Cheap 1M-context |
| Qwen 3.6 | 3.6 Plus (API) | varies by host | up to 1M | Qwen3.5-397B best open-weight long-context scorer |

DeepSeek V4 Flash is the extreme price outlier — ~35x cheaper than GPT-5.5 on input.

## 4. Access route: OpenRouter

400+ models, pass-through per-token rates, 5.5% fee on credit purchases. One key + ~$20 credits covers the whole benchmark.

## 5. Long-context reliability (100k+ tokens)

- BenchLM long-context leaderboard (Jul 1, 2026; LongBench v2, MRCRv2, AI-Needle, Graphwalks): **GPT-5.5 leads at 85.3**, then Claude Opus 4.5 (68.2), Qwen3.5-397B (65.4, best open-weight), Qwen3.6 Plus (64.5), GLM-5 (61.8). Large gap. "Most models claim 128K+ context, but actual performance varies wildly."
- NoLiMa caution stands: models drop below 50% at 32k when lexical overlap removed.
- Practical: 50k-token transcript is within mid-tier reliable range; 130k tokens is where degradation bites. Mitigation: chunked chaptering (2 x 60k halves w/ overlap + merge pass) keeps cheap tier viable for longest meetings.

## 6. Embeddings for search

text-embedding-3-small ($0.02/M), voyage-4-lite ($0.02/M), jina-v3 ($0.02/M) cheap workhorses; voyage-4 ($0.06/M), Gemini Embedding 2 ($0.15–0.20/M) higher quality; Google text-embedding-005 floor at $0.006/M. Embedding a year of meetings (~50M tokens) ≈ $1. Not a decision needing benchmarking.

## Recommendations

### (a) Benchmark set for chaptering
1. **GPT-5.5** — quality anchor (not production candidate)
2. **Claude Sonnet 5** — frontier-adjacent at intro pricing, 1M ctx (note +30% tokenizer)
3. **Gemini 3.5 Flash** — mid-cheap sweet spot, 1M ctx
4. **Claude Haiku 4.5** — Western cheap tier, 200k ctx
5. **DeepSeek V4 Flash** — price floor; if it passes, pipeline is nearly free
6. *(optional)* Grok 4.1 Fast (2M ctx + free credits) or GPT-5.4-mini

Skipped: Fable 5 / GPT-5.5-Pro (overkill), Gemini 3.5 Pro (not GA), GPT-5.6 (preview), GLM 5.2 / Kimi (no long-context edge over Gemini Flash at their price).

### (b) Cost per meeting (50k in, 5k out, ~3 passes)

| Model | Per pass | Per meeting (3 passes) | Batch |
|---|---|---|---|
| GPT-5.5 | $0.40 | $1.20 | $0.60 |
| Claude Sonnet 5 (intro, incl. tokenizer) | $0.20 | $0.59 | $0.29 |
| Gemini 3.5 Flash | $0.12 | $0.36 | $0.18 |
| Claude Haiku 4.5 | $0.075 | $0.23 | $0.11 |
| GPT-5.4-mini | $0.06 | $0.18 | $0.09 |
| Grok 4.1 Fast | $0.0125 | $0.04 | — |
| DeepSeek V4 Flash | $0.0084 | $0.025 | cache makes repeats near-free |

At ~40 meetings/month: $48/mo worst case, $1–8/mo cheap tier. Cost is not the binding constraint — quality is; benchmark generously, pick on accuracy.

### (c) Does a cheap model saturate the task?
Probably at 50k tokens; unproven at 130k. Either frontier for longest meetings or chunk-and-merge with cheap model (likely the better engineering answer).

Suggested design: 3–5 meetings spanning 30k–100k words, gold-standard chapters hand-checked once, score chapter-boundary F1 + title/summary quality (LLM-judge with GPT-5.5), single-pass vs chunked. Expected benchmark spend: under $15.
