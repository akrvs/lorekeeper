"""Transcript mapping engine (Zoom/Meet shared parser)."""

from app.connectors._transcript import parse_vtt, transcript_to_text

_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
Alice: hello there

2
00:00:03.000 --> 00:00:05.000
Bob: hi Alice, ready to ship?
"""


def test_parse_vtt_extracts_speaker_turns():
    turns = parse_vtt(_VTT)
    assert ("Alice", "hello there") in turns
    assert ("Bob", "hi Alice, ready to ship?") in turns


def test_transcript_to_text_roundtrip():
    text = transcript_to_text(parse_vtt(_VTT))
    assert "Alice: hello there" in text
    assert "Bob: hi Alice, ready to ship?" in text
