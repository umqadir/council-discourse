"""Experiment C: open-ASR quality ceiling.

Transcribe the transportation benchmark with whisper-large-v3 (MLX), reuse the
existing pyannote diarization turns for speaker labels, run the same
label-mapping naming + eval as the voxtral/local configs. Answers: can the best
open ASR match Voxtral's speaker-naming quality on this domain?
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mlx_whisper

from pipeline.artifacts import read_jsonl, write_jsonl
from pipeline.diarize import assign_labels_to_utterances

BENCH = ROOT / "data" / "benchmark" / "2025-04-23-transportation"
AUDIO = BENCH / "audio-16k.wav"
MODEL = "mlx-community/whisper-large-v3-mlx"

out_utt = BENCH / "utterances-whisper.jsonl"
if not out_utt.exists():
    t0 = time.time()
    result = mlx_whisper.transcribe(
        str(AUDIO),
        path_or_hf_repo=MODEL,
        condition_on_previous_text=False,
        verbose=False,
    )
    wall = time.time() - t0
    rows = [
        {"t0": round(s["start"], 3), "t1": round(s["end"], 3), "text": s["text"].strip()}
        for s in result["segments"]
        if s["text"].strip()
    ]
    write_jsonl(out_utt, rows)
    (BENCH / "transcribe-whisper-meta.json").write_text(
        json.dumps({"model": MODEL, "wall_clock_sec": round(wall, 1), "segments": len(rows)})
    )
    print(f"whisper done: {len(rows)} segments in {wall:.0f}s")

# overlay existing pyannote turns
turns = read_jsonl(BENCH / "diarization.jsonl")
utterances = read_jsonl(out_utt)
labeled = assign_labels_to_utterances(utterances, turns)
write_jsonl(BENCH / "utterances-whisper-labeled.jsonl", labeled)
print(f"labeled {len(labeled)} utterances with {len({u['label'] for u in labeled})} labels")
