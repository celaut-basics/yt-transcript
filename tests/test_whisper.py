"""Parsing whisper-cli's JSON, against output it really wrote.

The three fixtures are committed captures (see `fixtures/README.md`), so what is
asserted here is the schema the service will actually meet.
"""

import json
import os
import unittest

import whisper

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return handle.read()


class TestRealOutput(unittest.TestCase):
    def test_single_segment(self):
        result = whisper.parse(fixture("whisper-base-en.json"))
        self.assertEqual(result["language"], "en")
        self.assertEqual(len(result["segments"]), 1)
        self.assertTrue(result["text"])
        # whisper writes each segment with a leading space; it must not survive.
        self.assertFalse(result["text"].startswith(" "))
        self.assertFalse(result["segments"][0]["text"].startswith(" "))

    def test_multiple_segments_keep_their_own_bounds(self):
        """The case that catches reading the first segment's offsets for all."""
        result = whisper.parse(fixture("whisper-base-multiseg.json"))
        self.assertEqual(len(result["segments"]), 2)
        first, second = result["segments"]
        self.assertEqual(first["start_ms"], 0)
        self.assertEqual(first["end_ms"], 30000)
        self.assertEqual(second["start_ms"], 30000)
        self.assertEqual(second["end_ms"], 60000)
        self.assertNotEqual(first["text"], second["text"])

    def test_segments_are_joined_in_order_with_single_spaces(self):
        result = whisper.parse(fixture("whisper-base-multiseg.json"))
        first, second = result["segments"]
        self.assertEqual(result["text"], f"{first['text']} {second['text']}")
        self.assertNotIn("  ", result["text"])

    def test_silence_still_produces_a_segment(self):
        """Pinned because it is counter-intuitive and shapes the caller's contract.

        Three seconds of digital silence transcribes to `" you"` -- a known whisper
        artefact -- not to an empty transcription list. So a caller cannot read
        "empty text" as "silent audio", and this service does not pretend otherwise
        by special-casing it.
        """
        result = whisper.parse(fixture("whisper-base-silence.json"))
        self.assertEqual(len(result["segments"]), 1)
        self.assertTrue(result["text"])

    def test_the_fixtures_are_verbatim_captures(self):
        """Tabs and all: a re-serialised fixture would be a tidied schema."""
        raw = fixture("whisper-base-en.json")
        self.assertIn("\t", raw)
        self.assertNotEqual(raw, json.dumps(json.loads(raw)))


class TestShapes(unittest.TestCase):
    def test_empty_transcription_is_an_answer_not_an_error(self):
        result = whisper.parse(json.dumps({"transcription": []}))
        self.assertEqual(result["text"], "")
        self.assertEqual(result["segments"], [])

    def test_language_is_optional(self):
        result = whisper.parse(json.dumps({"transcription": []}))
        self.assertIsNone(result["language"])

    def test_blank_language_is_none_not_empty_string(self):
        raw = json.dumps({"transcription": [], "result": {"language": "   "}})
        self.assertIsNone(whisper.parse(raw)["language"])

    def test_missing_offsets_leave_none_bounds(self):
        raw = json.dumps({"transcription": [{"text": " hi"}]})
        segment = whisper.parse(raw)["segments"][0]
        self.assertIsNone(segment["start_ms"])
        self.assertIsNone(segment["end_ms"])
        self.assertEqual(segment["text"], "hi")

    def test_booleans_are_not_accepted_as_offsets(self):
        """`True` is an int in Python, and would become a 1 ms timestamp."""
        raw = json.dumps({"transcription": [
            {"text": " hi", "offsets": {"from": True, "to": False}}
        ]})
        segment = whisper.parse(raw)["segments"][0]
        self.assertIsNone(segment["start_ms"])
        self.assertIsNone(segment["end_ms"])

    def test_whitespace_only_segments_do_not_pad_the_text(self):
        raw = json.dumps({"transcription": [
            {"text": " one"}, {"text": "   "}, {"text": " two"}
        ]})
        result = whisper.parse(raw)
        self.assertEqual(result["text"], "one two")
        self.assertEqual(len(result["segments"]), 3)


class TestRejections(unittest.TestCase):
    """A transcription this service cannot read is a failed request."""

    def _rejected(self, raw, because):
        with self.subTest(because=because):
            with self.assertRaises(whisper.WhisperOutputError):
                whisper.parse(raw)

    def test_not_json(self):
        self._rejected("", "empty output, e.g. whisper wrote nothing")
        self._rejected("not json at all", "garbage")
        self._rejected("{unclosed", "truncated, e.g. a killed process")

    def test_not_an_object(self):
        self._rejected("[]", "a list at the top level")
        self._rejected('"a string"', "a bare string")
        self._rejected("null", "null")

    def test_missing_or_wrong_transcription(self):
        self._rejected(json.dumps({}), "no transcription key")
        self._rejected(json.dumps({"transcription": "text"}), "not a list")
        self._rejected(json.dumps({"transcription": {}}), "an object, not a list")

    def test_bad_entries(self):
        self._rejected(json.dumps({"transcription": ["hi"]}), "entry not an object")
        self._rejected(json.dumps({"transcription": [{}]}), "entry has no text")
        self._rejected(
            json.dumps({"transcription": [{"text": 42}]}), "text is not a string"
        )
        self._rejected(
            json.dumps({"transcription": [{"text": None}]}), "text is null"
        )


if __name__ == "__main__":
    unittest.main()
