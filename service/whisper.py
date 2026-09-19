"""Reading whisper.cpp's JSON output into this service's response shape.

Split out from the pipeline so it can be tested against committed fixtures with no
model, no audio and no subprocess -- which is the only way this part gets covered,
since running the real thing takes a model download and a minute of CPU.

The schema below is not guessed. It is what `whisper-cli -oj` (v1.9.4) wrote for a
real transcription, captured and committed under `tests/fixtures/`.
"""

import json
from typing import Any, Dict, List, Optional


class WhisperOutputError(ValueError):
    """whisper-cli's JSON was not the shape this service knows how to read."""


def _timestamp_ms(segment: Dict[str, Any], key: str) -> Optional[int]:
    """Milliseconds from `offsets`, which is the machine-readable half of the pair.

    whisper-cli writes each segment's bounds twice: `timestamps` as
    `HH:MM:SS,mmm` strings and `offsets` as integer milliseconds. The integers are
    taken and the strings ignored -- reformatting a string back into a number is
    work with a failure mode ("00:00:60,000") and no upside.
    """
    offsets = segment.get("offsets")
    if not isinstance(offsets, dict):
        return None
    value = offsets.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse(raw: str) -> Dict[str, Any]:
    """Turn whisper-cli's JSON into `{text, segments, language}`.

    Raises WhisperOutputError rather than returning something half-built: a
    transcription this service could not read is a failed request, not an empty one.
    An empty *transcription list*, on the other hand, is a perfectly good answer --
    it is what a silent clip produces -- and comes back as empty text.
    """
    try:
        document = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise WhisperOutputError(f"whisper-cli output is not JSON: {e}") from None

    if not isinstance(document, dict):
        raise WhisperOutputError(
            "whisper-cli output is not a JSON object at the top level."
        )

    transcription = document.get("transcription")
    if transcription is None:
        raise WhisperOutputError(
            "whisper-cli output has no 'transcription' key."
        )
    if not isinstance(transcription, list):
        raise WhisperOutputError(
            "whisper-cli's 'transcription' is not a list."
        )

    segments: List[Dict[str, Any]] = []
    pieces: List[str] = []
    for entry in transcription:
        if not isinstance(entry, dict):
            raise WhisperOutputError(
                "a 'transcription' entry is not an object."
            )
        text = entry.get("text")
        if not isinstance(text, str):
            raise WhisperOutputError(
                "a 'transcription' entry has no string 'text'."
            )
        # whisper emits each segment with a leading space; it is part of how the
        # pieces join, not part of the segment. Stripped per segment, and the join
        # below puts exactly one space back between them.
        stripped = text.strip()
        segments.append({
            "start_ms": _timestamp_ms(entry, "from"),
            "end_ms": _timestamp_ms(entry, "to"),
            "text": stripped,
        })
        if stripped:
            pieces.append(stripped)

    # `result.language` is what the model *detected* when asked to auto-detect, which
    # is the useful answer and not the same as what was requested: a caller who sent
    # `YT_LANGUAGE=auto` learns what it turned out to be. Absent in some builds, so
    # its absence is None rather than an error.
    language = None
    result = document.get("result")
    if isinstance(result, dict):
        detected = result.get("language")
        if isinstance(detected, str) and detected.strip():
            language = detected.strip()

    return {
        "text": " ".join(pieces),
        "segments": segments,
        "language": language,
    }
