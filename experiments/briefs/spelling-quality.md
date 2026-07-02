# Spelling quality round: roster biasing + verification anchoring; Voxtral as default

## Context
PLAN.md section 8: Voxtral (voxtral-mini-2602) is now the production ASR backend. Same-person accuracy is 87.6-95.9% but STRICT spelling is 70-73%: dominant error class = correct person, misspelled name (e.g. "Margaret Forgioni" vs "Forgione"; "Julie Menon" vs "Menin"). Eval reports: data/benchmark/*/speaker-naming-eval-voxtral-*.md. MISTRAL_API_KEY + GOOGLE_API_KEY in .env (never print).

## Tasks
1. **Voxtral context biasing**: the transcription API supports bias terms (up to 100). Feed council-member names (pipeline/roster.py has the roster) + committee names + common NYC agency terms (NYCHA, DCWP, DOT, SBS, DCP...) for the meeting's committee. Wire into the voxtral backend call; document the param name from https://docs.mistral.ai/studio-api/audio/speech_to_text (fetch not allowed — if unsure of the exact param, implement behind a config flag and mark TODO with your best-guess param from the SDK's typed interface in mistralai pip package, which you may install and inspect).
2. **Verification-pass spelling anchoring**: in pipeline/speakers.py verification step, when a named speaker is fuzzy-close (edit distance <=2 per name token, or phonetic match) to a roster member or Legistar-known official, snap to the canonical spelling; extend to org names. For Member of the Public names keep Gemini's web-grounded corrections as-is.
3. Rerun BOTH voxtral evals (stated + transportation) and report strict-spelling before/after. Budget ~$2 API spend.
4. Flip pipeline default transcribe backend to voxtral (config option remains for local). Update PLAN.md pipeline section to match.

## Hard constraints
Only this repo; no browser/MCP; no Metal jobs; never print keys; .git read-only (no commits; list changed files).
