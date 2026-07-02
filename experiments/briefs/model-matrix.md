# Model matrix: naming + chaptering across the price-performance frontier

## Context
PLAN.md = source of truth. Gemini 3.5 Flash is the incumbent for BOTH speaker naming and chaptering. We now have eval harnesses and baselines:
- Naming: experiments/07_eval_speaker_naming.py --benchmark {transportation,stated} --asr voxtral (Gemini baseline: 87.6% / 95.9% same-person; reports in data/benchmark/*/speaker-naming-eval-voxtral-*.md)
- Chaptering: experiments/04_chapter_gemini.py + 05_compare_chapters.py (Gemini baseline: stated F1@30s 84.6%, type agreement 88.9%; transportation F1@30s 73.2%)

OPENROUTER_API_KEY is now in .env (never print). OpenRouter is OpenAI-compatible at https://openrouter.ai/api/v1/chat/completions. HARD BUDGET: $8 total for this run — check https://openrouter.ai/api/v1/credits usage as you go and STOP at $8 spent, reporting what completed.

## Candidate models (exact OpenRouter ids — verify against /api/v1/models, pick nearest if renamed)
1. deepseek/deepseek-v4-flash (price floor)
2. openai/gpt-5.4-mini
3. z-ai/glm-5.2 (or moonshotai/kimi-k2.6 if GLM unavailable)
4. google/gemini-3.1-flash-lite (cheap Gemini tier; may also use native GOOGLE_API_KEY)
Baseline for comparison (do NOT rerun unless needed): gemini-3.5-flash existing reports.

## Tasks
1. Generalize the naming + chaptering model clients to accept an OpenAI-compatible endpoint (base_url + api_key + model id) alongside native Gemini. Keep structured-output robustness (JSON schema or tool-call; retry on parse failure ONCE, then mark failed).
2. Run the matrix: {4 models} x {naming, chaptering} x {stated, transportation} using voxtral utterances (utterances-voxtral-labeled.jsonl). Write per-config reports with the SAME metrics as baselines (same-person + strict spelling for naming; count, F1@15/30/60s, type agreement for chaptering) to data/benchmark/<bench>/matrix/<task>-<model-slug>.md plus a summary table experiments/out/model-matrix-summary.md (create dir).
3. Include per-run cost (OpenRouter returns usage + you can GET /api/v1/generation?id= for exact cost; or estimate from token counts x published prices) in every report and the summary.
4. Note failures honestly (refused JSON, truncation, timeout) — a model that can't do structured output reliably FAILS regardless of benchmark scores.

## Hard constraints
Only this repo; no browser/MCP; no Metal jobs; never print keys; .git read-only (no commits; list changed files). Do not modify the production default model — this is measurement only.
