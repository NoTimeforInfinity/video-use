"""Unit tests for the transcription provider adapter (transcribe.py).

Covers the Venice normalization contract that every downstream helper depends
on: words + synthesized spacing in strict time order, correct fidelity
metadata, and that pack_transcripts breaks phrases on the synthesized spacing.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
SPEC = importlib.util.spec_from_file_location("vu_transcribe", HELPERS / "transcribe.py")
assert SPEC and SPEC.loader
tr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tr)

SPEC_P = importlib.util.spec_from_file_location("vu_pack", HELPERS / "pack_transcripts.py")
assert SPEC_P and SPEC_P.loader
pack = importlib.util.module_from_spec(SPEC_P)
SPEC_P.loader.exec_module(pack)


class VeniceNormalizeTests(unittest.TestCase):
    def _payload(self):
        # Two segments with a real inter-segment gap (as Venice returns)
        return {
            "text": "Hello there.  Big pause.",
            "duration": 3.0,
            "timestamps": {"segment": [
                {"text": "Hello there.", "start": 0.1, "end": 1.1},
                {"text": "Big pause.", "start": 1.9, "end": 2.9},
            ]},
        }

    def test_words_and_spacing_are_time_ordered(self):
        n = tr.normalize_venice_payload(self._payload(), 3.0)
        starts = [w["start"] for w in n["words"]]
        self.assertEqual(starts, sorted(starts))

    def test_spacing_sits_between_the_right_words(self):
        n = tr.normalize_venice_payload(self._payload(), 3.0)
        kinds = [(w["type"], w["start"], w["end"]) for w in n["words"]]
        # the only big gap is between "there." (ends 1.1) and "Big" (starts 1.9)
        spacing = [k for k in kinds if k[0] == "spacing"]
        self.assertEqual(spacing, [("spacing", 1.1, 1.9)])

    def test_contract_keys(self):
        n = tr.normalize_venice_payload(self._payload(), 3.0)
        for w in n["words"]:
            self.assertEqual(sorted(w.keys()), ["end", "speaker_id", "start", "text", "type"])
            self.assertIn(w["type"], ("word", "spacing"))

    def test_speaker_id_is_none(self):
        n = tr.normalize_venice_payload(self._payload(), 3.0)
        self.assertTrue(all(w["speaker_id"] is None for w in n["words"]))
        self.assertEqual(n["_provider"], "venice")
        self.assertIn("hallucination", n["_fidelity"])

    def test_empty_payload_yields_no_words(self):
        n = tr.normalize_venice_payload({}, 2.0)
        self.assertEqual(n["words"], [])

    def test_pack_breaks_phrase_on_synthesized_spacing(self):
        # Regression: spacing misplaced by index caused a false phrase break at
        # the start of a segment. With time-ordered spacing, only the real
        # >=0.5s gap splits phrases.
        payload = {"text": "A B.  C D.",
                   "timestamps": {"segment": [
                       {"text": "A B.", "start": 0.0, "end": 1.0},
                       {"text": "C D.", "start": 1.5, "end": 2.5},
                   ]}}
        n = tr.normalize_venice_payload(payload, 2.5)
        phrases = pack.group_into_phrases(n["words"], silence_threshold=0.5)
        # gap 1.0->1.5 = 0.5 >= threshold -> two phrases, each covering one segment
        self.assertEqual([p["text"] for p in phrases], ["A B.", "C D."])
        self.assertEqual(phrases[0]["start"], 0.0)
        self.assertEqual(phrases[1]["start"], 1.5)


if __name__ == "__main__":
    unittest.main()
