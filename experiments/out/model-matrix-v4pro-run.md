# Model Matrix Summary

- OpenRouter total_usage start: $4.758883
- OpenRouter total_usage end: $6.096118
- OpenRouter observed run delta: $1.337235
- Reported generation cost total: $0.194592
- Conservative budget spend: $1.337235
- Budget limit: $8.00
- Verification pass: disabled for matrix LLM calls; deterministic roster/Legistar spelling anchors still run.
- Chaptering input: utterances-voxtral-labeled.jsonl.

## Models

| requested | used | prompt $/tok | completion $/tok |
| --- | --- | --- | --- |
| deepseek/deepseek-v4-pro | deepseek/deepseek-v4-pro | 0.000000435 | 0.00000087 |

## Results

| task | benchmark | model | status | cost | same-person | strict | chapters | F1@15 | F1@30 | F1@60 | type agree | report | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| naming | transportation | deepseek/deepseek-v4-pro | PASS | $0.050393 | 87.9% | 76.4% |  |  |  |  |  | data/benchmark/2025-04-23-transportation/matrix/naming-deepseek-deepseek-v4-pro.md |  |
| chaptering | transportation | deepseek/deepseek-v4-pro | PASS | $0.065247 |  |  | 93 | 62.1% | 69.0% | 70.9% | 45.5% | data/benchmark/2025-04-23-transportation/matrix/chaptering-deepseek-deepseek-v4-pro.md |  |
| naming | stated | deepseek/deepseek-v4-pro | PASS | $0.032616 | 97.3% | 74.3% |  |  |  |  |  | data/benchmark/2025-04-24-stated/matrix/naming-deepseek-deepseek-v4-pro.md |  |
| chaptering | stated | deepseek/deepseek-v4-pro | PASS | $0.046336 |  |  | 82 | 81.4% | 84.1% | 84.1% | 81.0% | data/benchmark/2025-04-24-stated/matrix/chaptering-deepseek-deepseek-v4-pro.md |  |

## Baselines

| task | benchmark | model | baseline |
| --- | --- | --- | --- |
| naming | transportation | gemini-3.5-flash | same-person 87.6%; strict in existing report |
| naming | stated | gemini-3.5-flash | same-person 95.9%; strict in existing report |
| chaptering | transportation | gemini-3.5-flash | F1@30s 73.2% |
| chaptering | stated | gemini-3.5-flash | F1@30s 84.6%; type agreement 88.9% |
