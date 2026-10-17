"""Regenerate the fictional LanternDesk KB and prompt/answer fixtures."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def generate() -> None:
    articles = []
    answers = {}
    prompts = ROOT / "prompts"
    prompts.mkdir(exist_ok=True)
    for number in range(1, 201):
        article_id = f"kb-{number}"
        module = f"mod{number:03d}"
        fact = f"set recovery code LD-{number:03d} to enabled"
        articles.append({
            "id": article_id,
            "module": module,
            "title": f"LanternDesk {module} support note",
            "body": f"For {module}, {fact}. This restores its status panel.",
            "key_fact": fact,
        })
        wordings = [
            f"How do I restore the LanternDesk {module} status panel?",
            f"The status panel for LanternDesk {module} is unavailable. "
            "What exact setting fixes it?",
        ]
        for variant, question in enumerate(wordings[:2 if number <= 100 else 1], start=1):
            question_id = f"q{number:03d}-{variant}"
            (prompts / f"{question_id}.txt").write_text(question + "\n", encoding="utf-8")
            answers[question_id] = {
                "question": question,
                "article_ids": [article_id],
                "key_fact": fact,
            }
    (ROOT / "articles.json").write_text(
        json.dumps(articles, indent=2) + "\n", encoding="utf-8",
    )
    (ROOT / "answers.json").write_text(
        json.dumps(answers, indent=2) + "\n", encoding="utf-8",
    )


if __name__ == "__main__":
    generate()
