"""Transcript mapping engine (Track 4) — shared by Zoom & Google Meet.

Meeting platforms differ in their APIs but converge on WebVTT/transcript text.
This module turns that text into normalized speaker turns, which connectors wrap
into a `RawDoc`. Keeping it provider-agnostic means Zoom/Meet wiring only has to
fetch the file and call `parse_vtt`.
"""

import re

_TIMECODE = re.compile(r"-->")


def parse_vtt(text: str) -> list[tuple[str, str]]:
    """Parse WEBVTT / Zoom transcript text into [(speaker, utterance)].

    Handles cue payloads of the form `Speaker Name: spoken text` and ignores
    WEBVTT headers, cue numbers, and timestamp lines.
    """
    turns: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [
            ln.strip()
            for ln in block.splitlines()
            if ln.strip()
            and not _TIMECODE.search(ln)
            and ln.strip().upper() != "WEBVTT"
            and not ln.strip().isdigit()
        ]
        if not lines:
            continue
        payload = " ".join(lines)
        speaker, sep, said = payload.partition(":")
        if sep and said.strip():
            turns.append((speaker.strip(), said.strip()))
        else:
            turns.append(("unknown", payload.strip()))
    return turns


def transcript_to_text(turns: list[tuple[str, str]]) -> str:
    return "\n".join(f"{speaker}: {said}" for speaker, said in turns)
