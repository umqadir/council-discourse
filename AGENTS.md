# AGENTS.md — operating contract

## Source of truth
`PLAN.md` at the repo root is authoritative for architecture, scope, and current
plan. `research/` holds the findings behind it. Treat both as read-mostly context;
update `PLAN.md` only when a decision actually changes.

## Mode
Autonomous: keep working while useful progress is available; don't stop early just
to summarize. Preserve continuity by committing coherent, well-described changes
(git author is preconfigured — never change git identity or add Co-Authored-By /
generated-with trailers; commit messages must not mention any AI tool).

## Model/effort tiers (for any sub-agent launches)
- Orchestrator: gpt-5.5, xhigh reasoning, fast/priority tier.
- Focused sub-agents (code, analysis): gpt-5.5, high reasoning, standard tier.
- Mechanical high-throughput workers: strongest current small model, schema-checkable outputs.

## Conventions
- Python via `uv`; the global env (`~/.venvs/global`) is on PATH — use it unless a
  project env exists. No bare pip/venv.
- Experiments live in `experiments/` as numbered scripts; pipeline code (when it
  exists) in `pipeline/`; site in `site/`.
- `data/` is gitignored (large artifacts). Don't commit media or multi-MB JSON.
- Secrets come from the environment; never hardcode or print keys.
- No emojis in code. Plain, top-down imperative scripts with constants at top.
