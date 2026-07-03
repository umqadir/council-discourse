# Model Matrix Summary

- OpenRouter total_usage start: $0.000000
- OpenRouter total_usage end: $1.276393
- OpenRouter observed run delta: $1.276393
- Reported generation cost total: $1.396289
- Conservative budget spend: $1.396289
- Budget limit: $8.00
- Verification pass: disabled for matrix LLM calls; deterministic roster/Legistar spelling anchors still run.
- Chaptering input: utterances-voxtral-labeled.jsonl.

## Models

| requested | used | prompt $/tok | completion $/tok |
| --- | --- | --- | --- |
| deepseek/deepseek-v4-flash | deepseek/deepseek-v4-flash | 0.000000089 | 0.00000018 |
| openai/gpt-5.4-mini | openai/gpt-5.4-mini | 0.00000075 | 0.0000045 |
| z-ai/glm-5.2 | z-ai/glm-5.2 | 0.00000093 | 0.000003 |
| google/gemini-3.1-flash-lite | google/gemini-3.1-flash-lite | 0.00000025 | 0.0000015 |

## Results

| task | benchmark | model | status | cost | same-person | strict | chapters | F1@15 | F1@30 | F1@60 | type agree | report | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| naming | transportation | deepseek/deepseek-v4-flash | PASS | $0.017150 | 81.5% | 70.1% |  |  |  |  |  | data/benchmark/2025-04-23-transportation/matrix/naming-deepseek-deepseek-v4-flash.md |  |
| chaptering | transportation | deepseek/deepseek-v4-flash | PASS | $0.018634 |  |  | 82 | 63.5% | 68.8% | 74.0% | 45.5% | data/benchmark/2025-04-23-transportation/matrix/chaptering-deepseek-deepseek-v4-flash.md |  |
| naming | stated | deepseek/deepseek-v4-flash | PASS | $0.014367 | 78.4% | 55.4% |  |  |  |  |  | data/benchmark/2025-04-24-stated/matrix/naming-deepseek-deepseek-v4-flash.md |  |
| chaptering | stated | deepseek/deepseek-v4-flash | PASS | $0.008020 |  |  | 82 | 77.2% | 82.8% | 84.1% | 85.7% | data/benchmark/2025-04-24-stated/matrix/chaptering-deepseek-deepseek-v4-flash.md |  |
| naming | transportation | openai/gpt-5.4-mini | PASS | $0.117707 | 71.0% | 65.3% |  |  |  |  |  | data/benchmark/2025-04-23-transportation/matrix/naming-openai-gpt-5.4-mini.md |  |
| chaptering | transportation | openai/gpt-5.4-mini | PASS | $0.166715 |  |  | 60 | 55.3% | 57.6% | 61.2% | 45.5% | data/benchmark/2025-04-23-transportation/matrix/chaptering-openai-gpt-5.4-mini.md |  |
| naming | stated | openai/gpt-5.4-mini | PASS | $0.079701 | 74.3% | 51.4% |  |  |  |  |  | data/benchmark/2025-04-24-stated/matrix/naming-openai-gpt-5.4-mini.md |  |
| chaptering | stated | openai/gpt-5.4-mini | PASS | $0.045154 |  |  | 64 | 80.3% | 83.5% | 83.5% | 96.8% | data/benchmark/2025-04-24-stated/matrix/chaptering-openai-gpt-5.4-mini.md |  |
| naming | transportation | z-ai/glm-5.2 | PASS | $0.182912 | 87.9% | 76.4% |  |  |  |  |  | data/benchmark/2025-04-23-transportation/matrix/naming-z-ai-glm-5.2.md |  |
| chaptering | transportation | z-ai/glm-5.2 | PASS | $0.130517 |  |  | 94 | 68.6% | 75.5% | 79.4% | 45.5% | data/benchmark/2025-04-23-transportation/matrix/chaptering-z-ai-glm-5.2.md |  |
| naming | stated | z-ai/glm-5.2 | PASS | $0.222622 | 98.6% | 74.3% |  |  |  |  |  | data/benchmark/2025-04-24-stated/matrix/naming-z-ai-glm-5.2.md |  |
| chaptering | stated | z-ai/glm-5.2 | PASS | $0.229200 |  |  | 70 | 88.7% | 91.7% | 91.7% | 88.9% | data/benchmark/2025-04-24-stated/matrix/chaptering-z-ai-glm-5.2.md |  |
| naming | transportation | google/gemini-3.1-flash-lite | PASS | $0.044990 | 84.4% | 72.9% |  |  |  |  |  | data/benchmark/2025-04-23-transportation/matrix/naming-google-gemini-3.1-flash-lite.md |  |
| chaptering | transportation | google/gemini-3.1-flash-lite | PASS | $0.060489 |  |  | 60 | 56.5% | 62.4% | 64.7% | 44.5% | data/benchmark/2025-04-23-transportation/matrix/chaptering-google-gemini-3.1-flash-lite.md |  |
| naming | stated | google/gemini-3.1-flash-lite | PASS | $0.028309 | 78.4% | 55.4% |  |  |  |  |  | data/benchmark/2025-04-24-stated/matrix/naming-google-gemini-3.1-flash-lite.md |  |
| chaptering | stated | google/gemini-3.1-flash-lite | PASS | $0.029802 |  |  | 50 | 79.6% | 81.4% | 81.4% | 82.5% | data/benchmark/2025-04-24-stated/matrix/chaptering-google-gemini-3.1-flash-lite.md |  |

## Baselines

| task | benchmark | model | baseline |
| --- | --- | --- | --- |
| naming | transportation | gemini-3.5-flash | same-person 87.6%; strict in existing report |
| naming | stated | gemini-3.5-flash | same-person 95.9%; strict in existing report |
| chaptering | transportation | gemini-3.5-flash | F1@30s 73.2% |
| chaptering | stated | gemini-3.5-flash | F1@30s 84.6%; type agreement 88.9% |
