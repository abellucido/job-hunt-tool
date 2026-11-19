"""Application tracking: one row per output/ folder, with a status you set on the Tracker page.

Statuses are stored in webapp/tracker.json, keyed by output folder name:
    {"2026-11-19_Roche_Sr_Scientist_...": {"status": "applied", "applied_on": "2026-11-20", "notes": "..."}}
Folders without an entry use a default: "tailored" if a CV or letter exists, otherwise "skipped".
"""
import json
import re
from datetime import date
from pathlib import Path

# status → (label, path through the funnel after the first step)
STATUSES = {
    "tailored":           ("Not applied yet",          ["Not applied yet"]),
    "skipped":            ("Skipped",                  ["Skipped"]),
    "applied":            ("Applied, waiting",         ["Applied", "Waiting"]),
    "no_response":        ("No response",              ["Applied", "No response"]),
    "rejected":           ("Rejected",                 ["Applied", "Rejected"]),
    "withdrawn":          ("Withdrew",                 ["Applied", "Withdrew"]),
    "interview":          ("Interviewing",             ["Applied", "Interview", "In progress"]),
    "rejected_interview": ("Rejected after interview", ["Applied", "Interview", "Rejected after interview"]),
    "offer":              ("Offer",                    ["Applied", "Interview", "Offer", "Deciding"]),
    "accepted":           ("Accepted",                 ["Applied", "Interview", "Offer", "Accepted"]),
    "declined":           ("Declined offer",           ["Applied", "Interview", "Offer", "Declined"]),
}
NOT_APPLIED = {"tailored", "skipped"}


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def parse_title(folder: Path) -> tuple[str, str, str]:
    """Company, role and location from analysis.md's first line, e.g.
    "# IQVIA: PK/PD Statistical Programmer" or "# HP — Business Analyst (Barcelona)".
    Falls back to the folder name (2026-11-19_Company_Role_Words)."""
    parts = folder.name.split("_")
    company, role, location = (parts[1] if len(parts) > 1 else folder.name), " ".join(parts[2:]), ""
    try:
        first = (folder / "analysis.md").read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (FileNotFoundError, IndexError):
        return company, role, location
    m = re.match(r"#\s*(.+?)\s*(?::|—|–| - )\s*(.+)$", first)
    if not m:
        return company, role, location
    company, role = m.group(1).strip(), m.group(2).strip()
    company = re.sub(r"\s*\(.*\)$", "", company)              # "Syngenta (Bracknell, UK)" → "Syngenta"
    loc = re.search(r"\(([^()]*)\)\s*$", role)                 # trailing "(Basel, Switzerland)"
    if loc:
        location, role = loc.group(1), role[:loc.start()].strip()
    return company, role.rstrip(","), location


def rows(output_dir: Path, store_path: Path) -> list[dict]:
    store = load(store_path)
    out = []
    for d in sorted((p for p in output_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        m = re.match(r"(\d{4}-\d{2}-\d{2})_", d.name)
        if not m:
            continue
        has_docs = (d / "cv.tex").exists() or (d / "cover_letter.tex").exists()
        company, role, location = parse_title(d)
        entry = store.get(d.name, {})
        status = entry.get("status") or ("tailored" if has_docs else "skipped")
        out.append({
            "folder": d.name, "date": m.group(1), "company": company, "role": role, "location": location,
            "tailored": has_docs, "status": status, "status_label": STATUSES.get(status, (status,))[0],
            "applied_on": entry.get("applied_on", ""), "notes": entry.get("notes", ""),
            "updated": entry.get("updated", ""),
            "files": [f for f in ("cv.pdf", "cover_letter.pdf", "analysis.md") if (d / f).exists()],
        })
    return out


def update(output_dir: Path, store_path: Path, folders: list[str], changes: dict) -> int:
    """Set status / applied_on / notes for one or more folders. Returns how many changed."""
    allowed = {"status", "applied_on", "notes"}
    changes = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if "status" in changes and changes["status"] not in STATUSES:
        raise ValueError(f"Unknown status {changes['status']!r}")
    if changes.get("applied_on") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", changes["applied_on"]):
        raise ValueError("applied_on must be YYYY-MM-DD")
    store = load(store_path)
    count = 0
    for folder in folders:
        if not (output_dir / folder).is_dir() or "/" in folder or folder.startswith("."):
            continue
        entry = store.setdefault(folder, {})
        entry.update(changes)
        # Moving past "applied" without a date: assume today, so time-to-response can be tracked.
        if entry.get("status") not in NOT_APPLIED and not entry.get("applied_on"):
            entry["applied_on"] = date.today().isoformat()
        entry["updated"] = date.today().isoformat()
        count += 1
    save(store_path, store)
    return count


def funnel(all_rows: list[dict]) -> dict:
    """Nodes and links for the Sankey: Analysed → Tailored/Not tailored → … per status path."""
    links: dict[tuple[str, str], int] = {}

    def add(a, b):
        links[(a, b)] = links.get((a, b), 0) + 1

    for r in all_rows:
        first = "Tailored" if r["tailored"] else "Not tailored"
        add("Analysed", first)
        path = STATUSES.get(r["status"], ("", ["Not applied yet"]))[1]
        if r["status"] == "skipped" and not r["tailored"]:
            path = []                       # analysed, judged a poor fit: ends at "Not tailored"
        steps = [first] + path
        for a, b in zip(steps, steps[1:]):
            add(a, b)
    names = list(dict.fromkeys(n for pair in links for n in pair))
    return {"nodes": [{"name": n} for n in names],
            "links": [{"source": a, "target": b, "value": v} for (a, b), v in links.items()]}


def summary(all_rows: list[dict]) -> dict:
    applied = [r for r in all_rows if r["status"] not in NOT_APPLIED]
    responded = [r for r in applied if r["status"] not in ("applied", "no_response", "withdrawn")]
    interviews = [r for r in applied if r["status"] in ("interview", "rejected_interview", "offer", "accepted", "declined")]
    offers = [r for r in applied if r["status"] in ("offer", "accepted", "declined")]
    return {
        "analysed": len(all_rows),
        "tailored": sum(r["tailored"] for r in all_rows),
        "applied": len(applied),
        "responded": len(responded),
        "response_rate": round(100 * len(responded) / len(applied)) if applied else None,
        "interviews": len(interviews),
        "offers": len(offers),
    }
