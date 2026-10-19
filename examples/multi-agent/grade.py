"""Check saved Kneepoint session answers against the fictional ground truth."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def grade(sessions_path: Path, answers_path: Path = ROOT / "answers.json") -> dict:
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    by_question = {entry["question"]: entry for entry in answers.values()}
    levels = defaultdict(lambda: {"sessions": 0, "answered": 0, "correct": 0,
                                  "wrong_marker": 0, "unknown_question": 0})
    with sessions_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            session = json.loads(line)
            counts = levels[str(session["concurrency"])]
            counts["sessions"] += 1
            transcript = session.get("transcript") or []
            users = [str(m.get("content", "")) for m in transcript if m.get("role") == "user"]
            assistants = [str(m.get("content", "")) for m in transcript
                          if m.get("role") == "assistant"]
            if not users or users[-1].strip() not in by_question:
                counts["unknown_question"] += 1
                continue
            if not assistants:
                continue
            counts["answered"] += 1
            expected = by_question[users[-1].strip()]
            answer = assistants[-1]
            marker = f"[RESOLVED {expected['article_ids'][0]}]"
            if marker in answer and expected["key_fact"] in answer:
                counts["correct"] += 1
            elif "[RESOLVED " in answer:
                counts["wrong_marker"] += 1
    by_level = {}
    totals = {key: 0 for key in ("sessions", "answered", "correct",
                                 "wrong_marker", "unknown_question")}
    for level, counts in sorted(levels.items(), key=lambda item: int(item[0])):
        by_level[level] = {
            **counts,
            "accuracy": counts["correct"] / counts["answered"] if counts["answered"] else None,
            "all_session_accuracy": counts["correct"] / counts["sessions"],
        }
        for key in totals:
            totals[key] += counts[key]
    return {
        "totals": {
            **totals,
            "accuracy": totals["correct"] / totals["answered"] if totals["answered"] else None,
            "all_session_accuracy": (
                totals["correct"] / totals["sessions"] if totals["sessions"] else None
            ),
        },
        "by_level": by_level,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions_jsonl", type=Path)
    args = parser.parse_args()
    print(json.dumps(grade(args.sessions_jsonl), indent=2))
