"""Prepare benchmark text artifacts:
- official-transcript.pdf -> official-transcript.txt (WER ground truth)
- captions.vtt -> captions-clean.jsonl (deduplicated roll-up CC cues w/ timestamps)
                  and captions-clean.txt (timestamped lines for LLM input)

Viebit VTT is broadcast roll-up CC: ALL CAPS, cues repeat lines as they scroll.
Dedup strategy: cues arrive as (start, end, text-fragment) where consecutive cues
re-show earlier fragments on different display lines; we keep first occurrence of
each fragment and emit (start_sec, text).
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "benchmark"


def ts_to_sec(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_vtt(path: Path) -> list[dict]:
    raw = path.read_text(errors="replace")
    cues = []
    block: list[str] = []
    for line in raw.splitlines():
        if "-->" in line:
            m = re.match(r"([\d:.]+)\s+-->\s+([\d:.]+)", line.strip())
            block = []
            cues.append({"start": ts_to_sec(m.group(1)), "end": ts_to_sec(m.group(2)), "lines": block})
        elif line.strip() and cues and not line.startswith(("WEBVTT", "NOTE")):
            block.append(line.strip())
    return cues


def dedupe_rollup(cues: list[dict]) -> list[dict]:
    """Emit each caption fragment once, at the time it first appears."""
    out = []
    seen_tail: list[str] = []  # recent fragments to suppress repeats
    for cue in cues:
        for frag in cue["lines"]:
            norm = re.sub(r"\s+", " ", frag).strip()
            if not norm:
                continue
            if norm in seen_tail:
                continue
            out.append({"t": round(cue["start"], 2), "text": norm})
            seen_tail.append(norm)
            if len(seen_tail) > 8:
                seen_tail.pop(0)
    return out


for d in sorted(DATA.iterdir()):
    if not d.is_dir():
        continue
    print(f"== {d.name}", flush=True)
    pdf = d / "official-transcript.pdf"
    if pdf.exists():
        subprocess.run(["pdftotext", "-layout", str(pdf), str(d / "official-transcript.txt")], check=True)
        n = len((d / "official-transcript.txt").read_text(errors="replace").split())
        print(f"  official transcript: {n:,} words", flush=True)
    vtt = d / "captions.vtt"
    if vtt.exists():
        frags = dedupe_rollup(parse_vtt(vtt))
        (d / "captions-clean.jsonl").write_text("\n".join(json.dumps(f) for f in frags))
        lines = [f"[{int(f['t']//3600)}:{int(f['t']%3600//60):02d}:{int(f['t']%60):02d}] {f['text']}" for f in frags]
        (d / "captions-clean.txt").write_text("\n".join(lines))
        words = sum(len(f["text"].split()) for f in frags)
        print(f"  captions: {len(frags):,} fragments, {words:,} words, span {frags[0]['t']:.0f}-{frags[-1]['t']:.0f}s", flush=True)

print("ALL DONE", flush=True)
