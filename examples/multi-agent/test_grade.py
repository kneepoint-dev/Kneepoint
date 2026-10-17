"""A plausible marker cannot pass when it cites the wrong article or fact."""

import json
import tempfile
import unittest
from pathlib import Path

from grade import grade


class GradeTests(unittest.TestCase):
    def test_wrong_marker_and_fact_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            question = "How do I restore the LanternDesk mod042 status panel?"
            rows = [
                {"concurrency": 1, "transcript": [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": (
                        "set recovery code LD-042 to enabled [RESOLVED kb-42]"
                    )},
                ]},
                {"concurrency": 1, "transcript": [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": (
                        "set recovery code LD-042 to enabled [RESOLVED kb-41]"
                    )},
                ]},
                {"concurrency": 1, "transcript": [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": (
                        "enable something else [RESOLVED kb-42]"
                    )},
                ]},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            result = grade(path)
        self.assertEqual(result["totals"]["correct"], 1)
        self.assertEqual(result["totals"]["wrong_marker"], 2)
        self.assertEqual(result["by_level"]["1"]["accuracy"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
