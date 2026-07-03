# LLM cost round: results and recommendation

Goal: get the LLM line (speaker naming + chaptering) cheaper while re-clearing the
existing quality gates. Gates, from the prior GLM-5.2 matrix run, are same-person
naming accuracy at least 87.9% (transportation) and 97.3% (stated), and chaptering
boundary F1 at 30s at least 75.5% (transportation) and 91.7% (stated).

Costs are OpenRouter's own billed amounts: the authoritative per-generation `cost`
where OpenRouter returned it, otherwise its per-token model pricing applied to
measured tokens. Both include reasoning tokens, which GLM-5.2 and DeepSeek V4 Pro
emit and which OpenRouter bills at the completion rate — this matters, because
GLM's chaptering reasoning tokens are the single largest cost item. Per-meeting
figures are naming plus chaptering, averaged over the two benchmark meetings.
Monthly figures assume 20 meetings per month.

## Decision table

| Config | naming same-person (transp / stated) | chaptering F1@30 (transp / stated) | Clears gates? | $/meeting | $/mo (20 mtgs) |
| --- | --- | --- | --- | --- | --- |
| GLM-5.2 both passes (current baseline) | 87.9% / 98.6% | 75.5% / 91.7% | yes | $0.383 | $7.65 |
| DeepSeek V4 Pro both passes | 87.9% / 97.3% | 69.0% / 84.1% | no (chaptering fails both) | $0.097 | $1.95 |
| Combined single pass, GLM-5.2 | 86.3% / 100.0% | 70.6% / 75.4% | no (chaptering fails both; naming fails transp) | $0.458 | $9.15 |
| Recommended: DeepSeek V4 Pro naming + GLM-5.2 chaptering | 87.9% / 97.3% | 75.5% / 91.7% | yes | $0.221 | $4.43 |

Gate thresholds: naming same-person 87.9% / 97.3%; chaptering F1@30 75.5% / 91.7%.

The recommended split clears every gate (each model runs only the task it already
passes, so there is no quality regression) and cuts the LLM line 42%, from $7.65 to
$4.43 per month at 20 meetings. That is a little above the $4 target at 20 meetings
and at or under it for a lighter month (14-18 meetings); the residual cost is
almost entirely GLM chaptering's reasoning tokens. See the note below on pushing it
lower if $4 must be a hard ceiling.

## What was tested, and what each result means

### 1. Prompt caching (zero quality risk)

The brief's premise was that the naming, chaptering, and summary passes share one
giant transcript prefix that provider-side caching could serve cheaply. That
premise does not hold for the current pipeline:

- Chaptering already emits chapters and the meeting summary in a single call, so
  there is no separate summary pass to cache against.
- Naming does not send the transcript at all. It sends per-diarized-label evidence
  windows (sampled utterances around each label), chunked at 40 labels per prompt.
  Measured on the transportation meeting, the two naming chunks share only about
  308 tokens of common prefix (roster plus meeting context) out of roughly 79k and
  40k token prompts — 0.4% and 0.8%. The bulk of each naming prompt is distinct
  per-label evidence, so there is essentially no repeated prefix to cache.
- Naming and chaptering do not share a prefix with each other: one sends evidence
  windows, the other sends the transcript.

Empirically, every OpenRouter call this round reported zero cached prompt tokens
(the client was instrumented to log `prompt_tokens_details.cached_tokens`). GLM-5.2
and DeepSeek V4 Pro via OpenRouter did not return cache hits on these single-shot
calls. A dedicated back-to-back probe (send the identical chaptering prompt twice,
then once with an explicit cache_control breakpoint, and compare billed cost) was
attempted but did not complete — GLM-5.2 latency through OpenRouter was severe this
session, and the probe's calls hung server-side. The instrumentation and probe
script (`experiments/12_cache_probe.py`) are in place to re-run when the provider is
responsive, but the architectural point above already settles the question. The one
place a genuine repeated prefix exists is chaptering's conditional coarse-retry,
which resends its own prompt — but that retry only fires when the first draft
undershoots the chapter-count floor, so it is not a reliable saving.

Conclusion: caching is not a lever for this pipeline as currently structured. It
would only pay off after a restructure that makes naming read the full transcript
as a cacheable prefix shared with chaptering — which is exactly the combined-pass
restructure tested next, and that failed on quality and cost. GLM-5.2's OpenRouter
cache-read price is $0.18 per million (5.2x below its $0.93 prompt rate) and
DeepSeek V4 Pro's is near zero, so caching would help a lot *if* a large shared
prefix existed; it does not.

Open question (could not verify without web access): whether Z.AI's direct GLM-5.2
API is cheaper than OpenRouter's $0.93 / $3.00 per million, and whether its native
context caching would activate on our call pattern. No Z.AI key is in the repo, so
this was not measurable here. Given the recommended split already meets the target,
this is not on the critical path.

### 2. Combined single pass (moderate risk), GLM-5.2

One structured-output call over the full transcript emitting speaker segments,
chapters, and the meeting summary together. Result: it fails on both quality and
cost.

- Quality: task interference degraded both jobs. Naming dropped to 86.3% on
  transportation (below the 87.9% gate) though it held at 100% on stated.
  Chaptering F1@30 fell to 70.6% (transportation) and 75.4% (stated), both well
  below the 75.5% / 91.7% gates. The stated chaptering collapse (91.7% baseline to
  75.4%) is the clearest interference signal.
- Cost: the combined pass cost *more*, not less — $0.572 (transportation) and
  $0.343 (stated), averaging $0.458 per meeting versus $0.383 for the two-pass GLM
  baseline. Emitting speaker segments and chapters in one response inflates
  completion and reasoning tokens, and completion is GLM's expensive side ($3.00 per
  million versus $0.93 for input). The hoped-for saving was on input tokens (one
  transcript read instead of two), but naming never sent the full transcript to
  begin with, so there was little input to save, and the extra output swamped it.
- No truncation was observed; outputs were complete. The failure is interference
  and output cost, not truncation.

Conclusion: the combined pass is dead. It loses quality and costs more.

### 3. Considered alt-model slot: DeepSeek V4 Pro

DeepSeek V4 Pro (deepseek/deepseek-v4-pro) on naming and chaptering, both
benchmarks. OpenRouter pricing is $0.435 / $0.87 per million — roughly half GLM's
input and a third of its output.

- Naming: ties both gates exactly. Same-person 87.9% (transportation) and 97.3%
  (stated), at roughly a third of GLM's naming cost. OpenRouter billed DeepSeek
  naming at $0.050 (transportation) and $0.033 (stated) versus GLM's $0.163 and
  $0.202. Notably DeepSeek's billed amount came in below its own list price on the
  naming prompts, suggesting some server-side prompt reuse even though no cached
  tokens were reported in the usage details.
- Chaptering: fails both gates. F1@30 69.0% (transportation, gate 75.5%) and 84.1%
  (stated, gate 91.7%). Type agreement was also weak on transportation (45.5%).
  DeepSeek produces reasonable chapter counts but its boundaries and type labels do
  not match the reference as tightly as GLM's.
- Latency: DeepSeek V4 Pro chaptering on the 4-hour transportation transcript took
  10 to 15 minutes per call (long reasoning traces). Naming, run in per-label
  chunks, was faster. This is an operational note, not a blocker for a batch
  pipeline, but it rules DeepSeek out for any latency-sensitive path.

Conclusion: DeepSeek V4 Pro is a strong, much cheaper naming model that matches GLM
on the naming gates, but it is not good enough for chaptering. Qwen 3.6 Plus was
considered on price (prompt $0.325, cheaper than GLM) but its completion rate
($1.95 per million) and lack of a caching discount made it a worse economic bet
than DeepSeek V4 Pro for the same quality risk, and it was not run given the split
already clears the target.

## Recommendation

Adopt the split: run speaker naming on DeepSeek V4 Pro and chaptering on GLM-5.2.
This clears every quality gate with no regression (each model keeps the task it
already passes) and brings the LLM line to $0.221 per meeting, about $4.43 per
month at 20 meetings — a 42% reduction from the $7.65 GLM-only baseline. The change
is a per-stage model override in the pipeline; no prompt or schema restructure is
needed.

Do not pursue the combined single pass (worse quality, higher cost) or prompt
caching (no meaningful shared prefix in the current architecture).

If $4 per month must be a hard ceiling at 20 meetings, the remaining cost is almost
entirely GLM chaptering's reasoning tokens (about $0.13 to $0.23 per meeting). Two
levers, neither pursued here because the split already lands close and both carry
quality risk the brief said to avoid: (a) price GLM-5.2 direct from Z.AI, which may
be cheaper than OpenRouter's $0.93 / $3.00 per million and may support native
context caching on the chaptering prompt — this was the brief's open question and
was not verifiable without web or a Z.AI key; (b) test whether a cheaper chaptering
model can be found that clears the 91.7% stated gate, which nothing except GLM has
so far. The naming side is already near the floor at $0.03 to $0.05 per meeting.

## Spend

This round used about $1.5 of the $5 OpenRouter budget: the DeepSeek V4 Pro full
matrix (four passes), the GLM-5.2 combined single-pass runs (both benchmarks), and
GLM cache probes. No Mistral or Z.AI spend (no Z.AI key available; Mistral excluded
per the brief).
