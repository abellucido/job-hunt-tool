"""Compatibility check: Claude compares one posting with the master CV and gives a 0–100 score.

It uses the same idea as /apply's analysis step (key requirements → evidence in the CV, or a gap)
but tailors nothing, so it's quick: one `claude -p` call with no tools. The instructions are in
fit_prompt.md. Results are stored per search run in jobscout/runs/<run>/fit.json, keyed by job file:
    {"001_ING_Junior_AI_Engineer.txt": {"status": "done", "score": 72, "rating": "Good", "summary": "...",
                                        "requirements": [...], "checked": "2026-11-20", "cost_usd": 0.05}}
"""
import json
import re
from datetime import date
from pathlib import Path

PROMPT_FILE = Path(__file__).resolve().parent / "fit_prompt.md"
STATUSES = ("match", "partial", "gap")


def rating(score: int) -> str:
    return "Good" if score >= 70 else "Partial" if score >= 45 else "Poor"


def build_prompt(posting: str, cv: str, facts: str) -> str:
    return (PROMPT_FILE.read_text(encoding="utf-8")
            .replace("{posting}", posting.strip())
            .replace("{cv}", cv.strip())
            .replace("{facts}", facts.strip() or "(none)"))


def parse(text: str) -> dict:
    """Claude's reply → a clean result. Raises ValueError if it isn't the JSON asked for."""
    m = re.search(r"\{.*\}", text or "", re.S)          # tolerate a ```json fence or a stray sentence
    if not m:
        raise ValueError("Claude's reply had no JSON in it.")
    d = json.loads(m.group(0))
    score = max(0, min(100, int(round(float(d["score"])))))
    reqs = []
    for r in d.get("requirements") or []:
        if not isinstance(r, dict) or not r.get("requirement"):
            continue
        status = str(r.get("status", "")).lower()
        reqs.append({"requirement": str(r["requirement"]), "status": status if status in STATUSES else "partial",
                     "importance": "nice" if str(r.get("importance", "")).lower() == "nice" else "must",
                     "evidence": str(r.get("evidence", ""))})
    return {"status": "done", "score": score, "rating": rating(score), "summary": str(d.get("summary", "")),
            "requirements": reqs, "checked": date.today().isoformat()}


def load(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "fit.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(run_dir: Path, file: str, result: dict | None):
    """Store (or with None, forget) one job's result."""
    data = load(run_dir)
    if result is None:
        data.pop(file, None)
    else:
        data[file] = result
    tmp = run_dir / "fit.tmp"
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(run_dir / "fit.json")
