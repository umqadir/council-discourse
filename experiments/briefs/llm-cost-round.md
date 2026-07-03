# LLM cost round: zero-risk levers first, restructures only if needed

## Philosophy (user directive)
Correctness first; skeptical of clever restructures. Test in order of quality-risk,
stop when the line is cheap enough. All configs must re-clear existing benchmarks
(same-person >= current 87.9/97.3; chaptering F1@30 >= 75.5/91.7) or they're dead.

## Tests, cheapest-risk first
1. PROMPT CACHING (zero quality risk — identical tokens/outputs): our naming +
   chaptering + summary passes share the same giant transcript prefix. Investigate
   provider-side caching: Z.AI direct API context caching for glm-5.2; OpenRouter
   pass-through caching support; structure prompts so the transcript is a common
   prefix and passes run back-to-back. Measure real cached vs uncached cost on one
   benchmark meeting (log actual billed tokens). If OpenRouter can't cache, price
   GLM-5.2 direct from Z.AI (also plain rate — GPT Pro claims OpenRouter lists
   $0.93/$3 while Z.AI's own GLM-4.7 pricing is lower; verify 5.2 direct rates from
   the API/account pages you can reach WITHOUT web browsing — if unverifiable,
   report as open question).
2. MISTRAL LARGE 3 STRAIGHT SWAP (zero architectural risk): run naming + chaptering
   on mistral-large-3 (via Mistral API direct, MISTRAL_API_KEY in .env; batch
   discount applies there too) on BOTH benchmarks with the standard eval. Report
   quality + measured cost vs GLM.
3. COMBINED SINGLE PASS (moderate risk): one structured-output pass emitting
   speakers+chapters+summaries together (one full-transcript read instead of three).
   glm-5.2 only. Both benchmarks. Watch for task interference + output truncation;
   report honestly.
4. DO NOT build candidate-first chaptering or evidence-ledger machinery this round —
   only report whether 1-3 already get the LLM line under ~$4/mo; if yes we stop.

Budget: $5 across OpenRouter+Mistral+Z.AI. Write results to
experiments/out/llm-cost-round.md with a decision table incl. $/meeting measured.

## Constraints
Only this repo; no browser/MCP/web; never print keys; .git read-only (list changed
files). Benchmarks in data/benchmark/*; reuse the matrix harness from tonight.
