"""Experiment C part 2: name + score the whisper-large-v3 config."""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
mod = __import__("07_eval_speaker_naming") if str(ROOT / "experiments") in sys.path else None
sys.path.insert(0, str(ROOT / "experiments"))
mod = __import__("07_eval_speaker_naming")
from pipeline.artifacts import read_json, read_jsonl
from pipeline.stages import name_speakers

bench = ROOT / "data" / "benchmark" / "2025-04-23-transportation"
meeting = mod._meeting(bench)
labeled = bench / "utterances-whisper-labeled.jsonl"
named = bench / "utterances-whisper-named.jsonl"
meta = bench / "name-speakers-whisper-meta.json"
if not named.exists():
    name_speakers(meeting, input_path=labeled, output_path=named, meta_path=meta,
                  runlog_stage="name_speakers_whisper_transportation")
rows = read_jsonl(named)
refs = mod._read_citymeetings_references(bench)
report = mod._score(rows, refs, read_json(meta), asr_meta=read_json(bench / "transcribe-whisper-meta.json"),
                    benchmark="transportation", asr="whisper")
out = bench / "speaker-naming-eval-whisper-transportation.md"
out.write_text(report)
print("\n".join(report.split("\n")[:12]))
