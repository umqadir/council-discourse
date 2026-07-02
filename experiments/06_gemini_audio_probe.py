"""Probe Gemini audio transcription on council-meeting audio slices.

Tests: verbatim quality, diarization plausibility, and — critically — timestamp
fidelity (known Gemini failure mode on long audio). We cut 12-min slices from the
start / middle / end of the meeting, ask for timestamped speaker-attributed
transcription, then compare returned timestamps against the viebit CC clock
(captions-clean.jsonl), which is aligned to true video time.

Usage: python 06_gemini_audio_probe.py [model] [slug]
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.1-flash-lite"
SLUG = sys.argv[2] if len(sys.argv) > 2 else "2025-04-23-transportation"
D = ROOT / "data" / "benchmark" / SLUG
KEY = os.environ["GOOGLE_API_KEY"]
SLICE_MIN = 12

PROMPT = """This is audio from a NYC Council hearing. Transcribe it verbatim with speaker
diarization and timestamps. The recording starts at offset {offset} (H:MM:SS) into the
meeting — express all timestamps as absolute meeting time (i.e. first word is at ~{offset}).

Return JSON: {{"utterances": [{{"t": "H:MM:SS", "speaker": "SPEAKER_1", "text": "..."}}]}}
New utterance on every speaker change. Use consistent speaker labels; if a speaker
identifies themselves or is introduced, use their actual name instead of a label."""


def hms(sec: float) -> str:
    return f"{int(sec//3600)}:{int(sec%3600//60):02d}:{int(sec%60):02d}"


audio = D / "audio.m4a"
dur = float(subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
    capture_output=True, text=True).stdout.strip())
slices = {"start": 120, "middle": dur / 2, "end": max(0, dur - SLICE_MIN * 60 - 120)}
results = {}

for name, off in slices.items():
    cut = D / f"slice-{name}.m4a"
    if not cut.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(off), "-t",
                        str(SLICE_MIN * 60), "-i", str(audio), "-c", "copy", str(cut)], check=True)
    size = cut.stat().st_size
    print(f"== {name}: offset {hms(off)}, {size/1e6:.1f} MB", flush=True)

    # upload via Files API (resumable, single shot)
    up = httpx.post(
        f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={KEY}",
        headers={
            "X-Goog-Upload-Protocol": "raw",
            "Content-Type": "audio/mp4",
        },
        content=cut.read_bytes(),
        timeout=300,
    )
    up.raise_for_status()
    furi = up.json()["file"]["uri"]
    # wait for ACTIVE
    for _ in range(30):
        st = httpx.get(f"{furi}?key={KEY}", timeout=30).json()
        if st.get("state") == "ACTIVE":
            break
        time.sleep(2)

    t0 = time.time()
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}",
        json={
            "contents": [{"parts": [
                {"fileData": {"fileUri": furi, "mimeType": "audio/mp4"}},
                {"text": PROMPT.format(offset=hms(off))},
            ]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "maxOutputTokens": 65536, "temperature": 0.1},
        },
        timeout=1200,
    )
    body = resp.json()
    if resp.status_code != 200:
        print(json.dumps(body)[:800], flush=True)
        continue
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    try:
        utt = json.loads(text)["utterances"]
    except Exception as e:
        print(f"  parse fail: {e}; head={text[:200]}", flush=True)
        continue
    usage = body.get("usageMetadata", {})
    results[name] = {"offset_sec": off, "n_utterances": len(utt),
                     "elapsed": round(time.time() - t0, 1), "usage": usage, "utterances": utt}
    print(f"  {len(utt)} utterances, {results[name]['elapsed']}s, "
          f"tokens {usage.get('promptTokenCount')}+{usage.get('candidatesTokenCount')}", flush=True)

out = D / f"audio-probe-{MODEL}.json"
out.write_text(json.dumps(results, indent=2))

# timestamp fidelity vs CC clock: for each slice, take utterances and find caption
# fragments with best word overlap within +/-120s; report offset stats
caps = [json.loads(l) for l in (D / "captions-clean.jsonl").read_text().splitlines()]


def to_sec(ts: str) -> float:
    p = [float(x) for x in ts.split(":")]
    return p[0] * 3600 + p[1] * 60 + (p[2] if len(p) > 2 else 0)


print("\n== timestamp fidelity vs CC clock ==", flush=True)
for name, r in results.items():
    deltas = []
    for u in r["utterances"][:: max(1, len(r["utterances"]) // 25)]:
        t = to_sec(u["t"])
        words = set(u["text"].upper().split())
        if len(words) < 4:
            continue
        best, best_score = None, 0
        for c in caps:
            if abs(c["t"] - t) > 150:
                continue
            score = len(words & set(c["text"].split()))
            if score > best_score:
                best, best_score = c, score
        if best and best_score >= 3:
            deltas.append(t - best["t"])
    if deltas:
        deltas.sort()
        med = deltas[len(deltas) // 2]
        print(f"  {name}: n={len(deltas)} median_offset={med:+.1f}s "
              f"p10={deltas[len(deltas)//10]:+.1f} p90={deltas[9*len(deltas)//10]:+.1f}", flush=True)
    else:
        print(f"  {name}: no alignable utterances", flush=True)
