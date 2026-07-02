"""First chaptering experiment: single-pass full-transcript chaptering with Gemini.

Input: cleaned timestamped captions (stand-in for ASR transcript).
Output: chapters JSON per model, for side-by-side comparison with
citymeetings' human-reviewed chapters.

Usage: python 04_chapter_gemini.py [model] [meeting-slug]
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.5-flash"
SLUG = sys.argv[2] if len(sys.argv) > 2 else "2025-04-23-transportation"
D = ROOT / "data" / "benchmark" / SLUG
API = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={k}"

MEETING_CONTEXT = {
    "2025-04-23-transportation": (
        "NYC Council Committee on Transportation and Infrastructure (Chair: Selvena "
        "N. Brooks-Powers) jointly with the Committee on Consumer and Worker Protection "
        "(Chair: Julie Menin), April 23, 2025, 10:00 AM, Council Chambers.\n"
        "Agenda: Oversight - Dining Out NYC program (the city's permanent outdoor "
        "dining program run by DOT); Int 0857-2024 - extending roadway cafe season.\n"
        "Typical structure: chair opening remarks, agency (DOT) testimony, council "
        "member Q&A rounds, then public testimony panels."
    ),
    "2025-04-24-stated": (
        "NYC Council Stated Meeting, April 24, 2025, 1:30 PM, Council Chambers. "
        "Full council session: invocation, adoption of minutes, messages, land use "
        "call-ups, communications, member introductions of legislation, discussion, "
        "general order votes with roll call, member explanations of vote, resolutions."
    ),
}

PROMPT = """You are dividing a NYC Council meeting transcript into chapters for a public \
website that helps residents navigate long meetings. Users skim chapter titles to find \
the 2-5 minute segments they care about.

MEETING CONTEXT:
{context}

TRANSCRIPT (timestamped closed-caption text; ALL-CAPS artifacts and small transcription \
errors are expected — infer proper names from context):
<transcript>
{transcript}
</transcript>

Divide the ENTIRE meeting into consecutive, non-overlapping chapters. Rules:
- FINE granularity is essential: chapters are typically 1-4 MINUTES long; a 4-hour \
meeting should produce roughly 90-130 chapters. Split aggressively:
  * Opening remarks: one chapter per distinct topic the speaker covers (a 5-minute \
opening becomes 3-5 chapters).
  * Agency/public testimony: one chapter per testifying person; long testimony splits \
by topic.
  * Q&A: one chapter per question-and-answer exchange (a member asking about a new \
topic starts a new chapter, even mid-round). Never merge multiple members into one chapter.
  * Votes/roll calls/procedure: each is its own short chapter.
- Cover the whole meeting; no gaps. First chapter starts at the meeting's first speech.
- type: one of REMARKS, AGENCY_TESTIMONY, TESTIMONY, QA, VOTE, PROCEDURE
- title: a specific headline naming who and what, e.g. "Council Member Menin questions \
DOT on application processing delays" — never generic like "Opening remarks continued".
- summary: 2-4 sentences, concrete, naming speakers and specifics.
- start: the timestamp (H:MM:SS, copied from a transcript line) where the chapter begins.

Return JSON: {{"chapters": [{{"start": "H:MM:SS", "type": "...", "title": "...", "summary": "..."}}]}}"""


transcript = (D / "captions-clean.txt").read_text()
prompt = PROMPT.format(context=MEETING_CONTEXT[SLUG], transcript=transcript)
print(f"model={MODEL} slug={SLUG} prompt_chars={len(prompt):,}", flush=True)

t0 = time.time()
resp = httpx.post(
    API.format(m=MODEL, k=os.environ["GOOGLE_API_KEY"]),
    json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 65536,
            "temperature": 0.3,
        },
    },
    timeout=1200,
)
elapsed = time.time() - t0
body = resp.json()
if resp.status_code != 200:
    print(json.dumps(body)[:2000])
    sys.exit(1)

usage = body.get("usageMetadata", {})
text = body["candidates"][0]["content"]["parts"][0]["text"]
chapters = json.loads(text)["chapters"]
out = D / f"chapters-{MODEL}.json"
out.write_text(json.dumps({"model": MODEL, "elapsed_sec": round(elapsed, 1), "usage": usage, "chapters": chapters}, indent=2))
print(f"{len(chapters)} chapters in {elapsed:.0f}s; tokens {usage.get('promptTokenCount')}+{usage.get('candidatesTokenCount')}", flush=True)
for c in chapters[:8]:
    print(f"  {c['start']} [{c['type']}] {c['title']}", flush=True)
