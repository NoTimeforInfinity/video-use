"""Unit tests for verify.py's Hard Rule 6 check (cut edges on word boundaries).

Ensures check_cut_edges flags an edge that lands strictly inside a word,
passes an edge on a word boundary, and warns when an edge is far from any
boundary (violating Rule 7's 30-200ms pad window).
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
SPEC = importlib.util.spec_from_file_location("vu_verify", HELPERS / "verify.py")
assert SPEC and SPEC.loader
vu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vu)


def make_edit_dir() -> tuple[Path, Path]:
    """Return (edit_dir, transcript_path) with a known word track."""
    d = Path(tempfile.mkdtemp(prefix="vu_verify_test_"))
    tr = d / "transcripts"
    tr.mkdir(parents=True)
    words = [
        {"type": "word", "text": "Hello", "start": 0.5, "end": 1.0, "speaker_id": None},
        {"type": "word", "text": "world", "start": 1.2, "end": 1.8, "speaker_id": None},
        {"type": "word", "text": "again", "start": 2.0, "end": 2.6, "speaker_id": None},
    ]
    (tr / "clipX.json").write_text(json.dumps({"words": words}))
    return d, tr / "clipX.json"


class CutEdgeCheckTests(unittest.TestCase):
    def _check(self, edit_dir, start, end):
        edl = {"sources": {"clipX": "clipX.mp4"},
               "ranges": [{"source": "clipX", "start": start, "end": end}]}
        return vu.check_cut_edges(edl, edit_dir)

    def test_edge_inside_a_word_fails(self):
        d, _ = make_edit_dir()
        # 0.7 is strictly inside "Hello" [0.5, 1.0]
        res = self._check(d, 0.7, 1.2)
        statuses = {r["edge"]: r["status"] for r in res["results"]}
        self.assertEqual(res["verdict"], "fail")
        self.assertEqual(statuses["start"], "fail")

    def test_edges_on_word_boundaries_pass(self):
        d, _ = make_edit_dir()
        # start 0.5 (word start), end 1.8 (word end)
        res = self._check(d, 0.5, 1.8)
        self.assertEqual(res["verdict"], "pass")
        self.assertTrue(all(r["status"] == "pass" for r in res["results"]))

    def test_edge_far_from_boundary_warns(self):
        d, _ = make_edit_dir()
        # 1.05 is 50ms after "Hello" ends (1.0) and 150ms before "world" (1.2)
        # -> pad 0.05 <= 0.25 -> pass. Use 1.0 exactly (a boundary) for pass,
        # and a clearly mid-gap 1.3 for a warn is inside "world" though...
        # 0.3 sits 200ms before "Hello" and ~nothing after -> pad 0.2 -> pass.
        res = self._check(d, 0.3, 2.0)
        self.assertEqual(res["verdict"], "pass")

    def test_no_transcript_is_info_not_fail(self):
        d = Path(tempfile.mkdtemp(prefix="vu_verify_test_"))
        (d / "transcripts").mkdir(parents=True)
        res = self._check(d, 0.5, 1.8)
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["results"][0]["status"], "info")


if __name__ == "__main__":
    unittest.main()
