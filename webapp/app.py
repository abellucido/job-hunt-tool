"""Job Hunt: a local web page for jobscout (search) and /apply (tailoring).

    cd webapp
    .venv/bin/uvicorn app:app --port 8000
    # then open http://localhost:8000

Searching runs jobscout. Tailoring runs `claude -p "/apply <job file>"` (headless Claude Code,
on your own subscription) for each selected job, a few at a time.
"""
import asyncio
import csv
import json
import os
import re
import shutil
import sys
import time
import uuid
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

import companies
import fit
import posting
import tracker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "jobscout"))
import jobscout  # noqa: E402  (its Scorer, Job and seen.sqlite, so added jobs are scored like searched ones)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]          # CoverLetters/
JOBSCOUT = ROOT / "jobscout"
RUNS = JOBSCOUT / "runs"
OUTPUT = ROOT / "output"
CV_MASTER = ROOT / "base" / "cv_master.tex"
HERE = Path(__file__).resolve().parent
TASKS_FILE = HERE / "tasks.json"

CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
MAX_PARALLEL = 3            # tailoring runs at the same time
RUN_TIMEOUT = 25 * 60       # seconds before a stuck run is stopped
SAFE_NAME = re.compile(r"^[\w.\-]+$")

app = FastAPI(title="Job Hunt")
searches: dict[str, dict] = {}
semaphore = asyncio.Semaphore(MAX_PARALLEL)


# ------------------------------------------------------------------ helpers

def safe(name: str) -> str:
    """Only plain file/folder names, so requests can't escape their folder."""
    if not SAFE_NAME.match(name) or name in (".", ".."):
        raise HTTPException(400, f"Invalid name: {name}")
    return name


def load_tasks() -> dict:
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


tasks: dict[str, dict] = load_tasks()
for t in tasks.values():                 # runs that were going when the app stopped
    if t["status"] in ("queued", "running"):
        t.update(status="failed", error="The app was restarted before this run finished.")


def save_tasks():
    TASKS_FILE.write_text(json.dumps(tasks, indent=1, ensure_ascii=False), encoding="utf-8")


def read_shortlist(run: str) -> list[dict]:
    path = RUNS / safe(run) / "shortlist.csv"
    if not path.exists():
        raise HTTPException(404, f"No shortlist in run {run}")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    tailored = {(t["run"], t["file"]): t["id"] for t in tasks.values()}
    fits = fit.load(RUNS / run)
    for row in rows:
        row["task"] = tailored.get((run, row["file"]))
        row["fit"] = {"status": "running"} if (run, row["file"]) in fits_running else fits.get(row["file"])
    return rows


def find_output_folder(job_text: str, report: str, started: float) -> str | None:
    """Which output/ folder a run created: the path in Claude's report, or else the newest
    folder whose job_description.txt best matches the posting."""
    for m in re.finditer(r"output/([\w.\-]+)", report or ""):
        if (OUTPUT / m.group(1) / "cv.tex").exists() or (OUTPUT / m.group(1) / "cover_letter.tex").exists():
            return m.group(1)
    best, best_ratio = None, 0.5
    body = job_text.split("\n\n", 1)[-1][:3000]
    for d in OUTPUT.iterdir():
        jd = d / "job_description.txt"
        if d.is_dir() and jd.exists() and d.stat().st_mtime >= started - 5:
            ratio = SequenceMatcher(None, body, jd.read_text(encoding="utf-8", errors="ignore")[:3000]).quick_ratio()
            if ratio > best_ratio:
                best, best_ratio = d.name, ratio
    return best


def folder_files(folder: str | None) -> list[str]:
    if not folder or not (OUTPUT / folder).is_dir():
        return []
    wanted = ("cv.pdf", "cover_letter.pdf", "analysis.md", "cv.tex", "cover_letter.tex", "job_description.txt")
    return [f for f in wanted if (OUTPUT / folder / f).exists()]


# ------------------------------------------------------------------ settings & runs

@app.get("/api/settings")
def settings():
    cfg = yaml.safe_load((JOBSCOUT / "config.yaml").read_text(encoding="utf-8"))
    return {
        "countries": list(cfg["locations"]),
        "search_terms": cfg["search_terms"],
        "cv": {"path": str(CV_MASTER.relative_to(ROOT)),
               "modified": datetime.fromtimestamp(CV_MASTER.stat().st_mtime).strftime("%Y-%m-%d %H:%M")},
        "max_parallel": MAX_PARALLEL,
    }


# Jobs added by hand from a link (Search page): a run folder of their own, jobscout/runs/added/,
# with the same shortlist.csv and jobs/*.txt a search writes. It is listed last among the runs.
ADDED_RUN = "added"
SHORTLIST_FIELDS = ["verdict", "score", "new", "company", "title", "location", "posted", "flags", "gaps", "url", "file"]


class AddJobRequest(BaseModel):
    url: str = ""
    text: str = ""           # the posting, pasted when the link can't be read; then title and company are needed
    title: str = ""
    company: str = ""
    location: str = ""


def read_added() -> list[dict]:
    path = RUNS / ADDED_RUN / "shortlist.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_added(rows: list[dict]):
    path = RUNS / ADDED_RUN / "shortlist.csv"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHORTLIST_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


AGE_LABELS = {1: "24 hours", 3: "3 days", 7: "1 week", 30: "1 month"}


def run_label(d: Path) -> str:
    """e.g. "19 Nov 14:42 · Spain · 24 hours · 7 postings", from the run's search.json."""
    if d.name == ADDED_RUN:
        n = len(read_added())
        return f"Added from links · {n} posting{'' if n == 1 else 's'}"
    with open(d / "shortlist.csv", encoding="utf-8") as f:
        count = max(sum(1 for _ in f) - 1, 0)
    try:
        meta = json.loads((d / "search.json").read_text(encoding="utf-8"))
        when = datetime.strptime(d.name, "%Y-%m-%d_%H%M%S").strftime("%d %b %H:%M")
        days = meta["max_age_days"]
        parts = [when, ", ".join(meta["countries"]), AGE_LABELS.get(days, f"{days} days")]
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        parts = [d.name, "from the command line"]   # older runs have no search.json
    return " · ".join(parts + [f"{count} postings"])


@app.get("/api/runs")
def list_runs():
    """Saved searches, newest first, then the "Added from links" list if there is one."""
    runs = [d for d in RUNS.iterdir() if (d / "shortlist.csv").exists()] if RUNS.exists() else []
    searches_ = sorted((d for d in runs if d.name != ADDED_RUN), key=lambda d: d.name, reverse=True)
    added = [d for d in runs if d.name == ADDED_RUN]
    return [{"name": d.name, "label": run_label(d)} for d in searches_ + added]


@app.get("/api/runs/{run}")
def get_run(run: str):
    return {"run": run, "label": run_label(RUNS / safe(run)), "jobs": read_shortlist(run)}


@app.get("/api/runs/{run}/jobs/{file}")
def get_posting(run: str, file: str):
    path = RUNS / safe(run) / "jobs" / safe(file)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/plain; charset=utf-8")


# ------------------------------------------------------------------ companies

CONFIG = JOBSCOUT / "config.yaml"


class CompanyQuery(BaseModel):
    query: str


class CompanySource(BaseModel):
    system: str
    id: str
    host: str = ""
    site: str = ""
    name: str = ""


@app.get("/api/companies")
def get_companies():
    return companies.list_companies(CONFIG)


@app.post("/api/companies/check")
async def check_company(q: CompanyQuery):
    if not q.query.strip():
        raise HTTPException(400, "Type a company name or paste a career-site link.")
    return await run_in_threadpool(companies.check, CONFIG, q.query)


@app.post("/api/companies")
async def add_company(src: CompanySource):
    if any(s["status"] == "running" for s in searches.values()):
        raise HTTPException(409, "A search is running. Add the company when it has finished.")
    return await run_in_threadpool(companies.add, CONFIG, src.model_dump())


# ------------------------------------------------------------------ tracker

TRACKER_FILE = Path(os.environ.get("TRACKER_FILE") or HERE / "tracker.json")   # override for testing


class TrackerUpdate(BaseModel):
    folders: list[str]
    status: str | None = None
    applied_on: str | None = None
    notes: str | None = None


@app.get("/api/tracker")
def get_tracker():
    rows = tracker.rows(OUTPUT, TRACKER_FILE)
    return {"rows": rows, "summary": tracker.summary(rows), "funnel": tracker.funnel(rows),
            "statuses": [{"value": k, "label": v[0]} for k, v in tracker.STATUSES.items()]}


@app.post("/api/tracker")
def update_tracker(u: TrackerUpdate):
    try:
        n = tracker.update(OUTPUT, TRACKER_FILE, u.folders,
                           {"status": u.status, "applied_on": u.applied_on, "notes": u.notes})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"updated": n}


# ------------------------------------------------------------------ dashboard (home page)

FOLLOW_UP_DAYS = 14         # applied this long ago with no reply: show under "Follow up"


@app.get("/api/dashboard")
def dashboard():
    """Everything the home page shows, in one call."""
    rows = tracker.rows(OUTPUT, TRACKER_FILE) if OUTPUT.exists() else []
    today = date.today()

    def days_since(day: str) -> int | None:
        try:
            return (today - date.fromisoformat(day)).days
        except ValueError:
            return None

    to_send = [r for r in rows if r["status"] == "tailored"]                       # newest first
    follow_up = [{**r, "days": days_since(r["applied_on"])} for r in rows if r["status"] == "applied"]
    follow_up = sorted((r for r in follow_up if (r["days"] or 0) >= FOLLOW_UP_DAYS), key=lambda r: -r["days"])
    active = [r for r in rows if r["status"] in ("interview", "offer")]

    latest = None
    runs = [r for r in list_runs() if r["name"] != ADDED_RUN]
    if runs:
        jobs = read_shortlist(runs[0]["name"])
        verdicts = {}
        for j in jobs:
            verdicts[j["verdict"]] = verdicts.get(j["verdict"], 0) + 1
        top = sorted((j for j in jobs if j["verdict"] == "Apply" and not j["task"]),
                     key=lambda j: -int(j["score"] or 0))[:5]
        latest = {**runs[0], "total": len(jobs), "new": sum(1 for j in jobs if j["new"]), "verdicts": verdicts,
                  "top": [{k: j[k] for k in ("company", "title", "score", "new", "url", "file")} for j in top]}

    week_ago = time.time() - 7 * 86400
    return {
        "today": today.isoformat(),
        "summary": tracker.summary(rows),
        "weekly": tracker.weekly(rows),
        "to_send": to_send[:8], "to_send_total": len(to_send),
        "follow_up": follow_up[:8], "follow_up_total": len(follow_up), "follow_up_days": FOLLOW_UP_DAYS,
        "active": active,
        "latest_run": latest, "runs_total": len(runs),
        "tailoring": {
            "running": sum(t["status"] == "running" for t in tasks.values()),
            "queued": sum(t["status"] == "queued" for t in tasks.values()),
            "failed_recent": [{"company": t["company"] or t["title"], "title": t["title"], "error": (t.get("error") or "")[:200]}
                              for t in tasks.values() if t["status"] == "failed" and t["created"] >= week_ago],
        },
    }


# ------------------------------------------------------------------ search (jobscout)

class SearchRequest(BaseModel):
    countries: list[str]
    terms: list[str]
    max_age_days: int = 7          # only postings from the last N days
    new_only: bool = False
    include_skips: bool = False


@app.post("/api/search")
async def start_search(req: SearchRequest):
    terms = [t.strip() for t in req.terms if t.strip()]
    if not req.countries or not terms:
        raise HTTPException(400, "Pick at least one country and one keyword.")
    if not 1 <= req.max_age_days <= 365:
        raise HTTPException(400, "max_age_days must be between 1 and 365.")
    run = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    sid = uuid.uuid4().hex[:8]
    searches[sid] = {"id": sid, "run": run, "status": "running", "log": [], "started": time.time()}
    (RUNS / run).mkdir(parents=True, exist_ok=True)       # remember what was searched for
    (RUNS / run / "search.json").write_text(json.dumps(
        {"countries": req.countries, "terms": terms, "max_age_days": req.max_age_days,
         "include_skips": req.include_skips}, ensure_ascii=False, indent=1), encoding="utf-8")
    cmd = [sys.executable, str(JOBSCOUT / "jobscout.py"), "--run-dir", str(RUNS / run),
           "--countries", *req.countries, "--terms", *terms, "--max-age-days", str(req.max_age_days)]
    if req.new_only:
        cmd.append("--new-only")
    if req.include_skips:
        cmd.append("--include-skips")
    asyncio.create_task(run_search(sid, cmd))
    return searches[sid]


async def run_search(sid: str, cmd: list[str]):
    s = searches[sid]
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=JOBSCOUT, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    async for line in proc.stdout:
        s["log"].append(line.decode(errors="replace").rstrip())
    code = await proc.wait()
    s["status"] = "done" if code == 0 and (RUNS / s["run"] / "shortlist.csv").exists() else "failed"
    s["seconds"] = round(time.time() - s["started"])


@app.get("/api/search/{sid}")
def search_status(sid: str):
    if sid not in searches:
        raise HTTPException(404)
    return searches[sid]


# ------------------------------------------------------------------ compatibility check (Check fit)

FIT_PARALLEL = 3            # fit checks at the same time
FIT_TIMEOUT = 5 * 60        # seconds
FIT_MODEL = ""              # "" = Claude Code's default model, or e.g. "sonnet" to go lighter on usage limits
FACTS = ROOT / "base" / "facts.md"
fits_running: set[tuple[str, str]] = set()
fit_semaphore = asyncio.Semaphore(FIT_PARALLEL)


class FitRequest(BaseModel):
    run: str
    files: list[str]


@app.post("/api/fit")
async def check_fit(req: FitRequest):
    """Start a compatibility check for each job; results land in runs/<run>/fit.json."""
    files = {r["file"] for r in read_shortlist(req.run)}
    started = []
    for file in req.files:
        if file not in files:
            raise HTTPException(404, f"{file} is not in run {req.run}")
        if (req.run, file) not in fits_running:
            fits_running.add((req.run, file))
            asyncio.create_task(run_fit(req.run, file))
            started.append(file)
    return {"started": started}


def clear_fit(run: str, file: str):
    fit.save(RUNS / run, file, None)


async def run_fit(run: str, file: str):
    run_dir = RUNS / run
    try:
        async with fit_semaphore:
            prompt = fit.build_prompt((run_dir / "jobs" / file).read_text(encoding="utf-8"),
                                      CV_MASTER.read_text(encoding="utf-8"),
                                      FACTS.read_text(encoding="utf-8") if FACTS.exists() else "")
            cmd = [CLAUDE, "-p", "--output-format", "json"] + (["--model", FIT_MODEL] if FIT_MODEL else [])
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=HERE, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), FIT_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"Stopped after {FIT_TIMEOUT // 60} minutes.")
        try:
            reply = json.loads(out)
        except json.JSONDecodeError:
            raise RuntimeError((err or out).decode(errors="replace")[-500:] or "No output from claude")
        if reply.get("is_error"):
            raise RuntimeError(str(reply.get("result", "Claude reported an error"))[:500])
        try:
            result = fit.parse(reply.get("result", ""))
        except (ValueError, KeyError, TypeError) as e:
            raise RuntimeError(f"Couldn't read Claude's answer: {e}")
        result["cost_usd"] = round(reply.get("total_cost_usd") or 0, 3)
    except Exception as e:                               # any failure is shown on the job, with a retry
        result = {"status": "failed", "error": str(e) or e.__class__.__name__, "checked": date.today().isoformat()}
    try:
        fit.save(run_dir, file, result)                  # save first, so the job never looks unchecked meanwhile
    finally:
        fits_running.discard((run, file))


# ------------------------------------------------------------------ tailoring (/apply)

class ApplyRequest(BaseModel):
    run: str
    files: list[str]
    model: str = ""          # "" = Claude Code's default, or e.g. "sonnet"


@app.post("/api/apply")
async def start_apply(req: ApplyRequest):
    if req.model and req.model not in ("sonnet", "opus"):
        raise HTTPException(400, "Unknown model")
    rows = {r["file"]: r for r in read_shortlist(req.run)}
    created = []
    for file in req.files:
        if file not in rows:
            raise HTTPException(404, f"{file} is not in run {req.run}")
        busy = [t for t in tasks.values() if t["run"] == req.run and t["file"] == file
                and t["status"] in ("queued", "running")]
        if busy:
            continue
        row = rows[file]
        created.append(queue_apply(req.run, file, row["company"], row["title"], row["url"], req.model))
    save_tasks()
    return {"created": created}


def queue_apply(run: str, file: str, company: str, title: str, url: str, model: str) -> str:
    """Add a tailoring run for jobscout/runs/<run>/jobs/<file> and start it when a slot is free."""
    tid = uuid.uuid4().hex[:8]
    tasks[tid] = {"id": tid, "run": run, "file": file, "company": company, "title": title, "url": url,
                  "model": model or "default", "status": "queued", "created": time.time()}
    asyncio.create_task(run_apply(tid))
    return tid


class AddedUpdate(BaseModel):
    file: str
    company: str


@app.post("/api/added/company")
def set_added_company(u: AddedUpdate):
    """Set the company of a job in "Added from links" (some pages don't say which company it is)."""
    company = u.company.strip()
    if not company:
        raise HTTPException(400, "Type the company name.")
    rows = read_added()
    row = next((r for r in rows if r["file"] == safe(u.file)), None)
    if not row:
        raise HTTPException(404, "That job isn't in the Added from links list.")
    row["company"] = company
    write_added(rows)
    path = RUNS / ADDED_RUN / "jobs" / row["file"]           # /apply reads the company from this header
    if path.exists():
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"^Company:.*$", lambda _: f"Company: {company}", text, count=1, flags=re.M)
        path.write_text(text, encoding="utf-8")
    for t in tasks.values():                                  # its tailoring cards on the Search page
        if t["run"] == ADDED_RUN and t["file"] == row["file"]:
            t["company"] = company
    save_tasks()
    return {"file": row["file"], "company": company}


@app.post("/api/add-job")
def add_job(req: AddJobRequest):
    """Read a posting from a link (or take pasted text), score it like jobscout does and add it
    to the "Added from links" list. Tailoring it is then the same as for any search result.
    Adding a link that is already in the list reads it again and replaces its entry."""
    url, text = req.url.strip(), req.text.strip()
    if not url and not text:
        raise HTTPException(400, "Paste a link to the job posting.")
    rows = read_added()
    old = next((r for r in rows if url and r["url"] == url), None)
    if text:
        if not req.title.strip() or not req.company.strip():
            raise HTTPException(400, "Fill in the job title and company too.")
        if len(text) < 200:
            raise HTTPException(400, "That's too short to be a job posting. Paste the whole posting text.")
        job = {"title": req.title.strip(), "company": req.company.strip(), "location": req.location.strip(),
               "posted": "", "url": url, "description": text}
    else:
        try:
            job = posting.fetch(url, companies.list_companies(CONFIG))
        except posting.PostingError as e:
            raise HTTPException(422, str(e))
        if req.company.strip():                     # typed by the user: wins over what the page says
            job["company"] = req.company.strip()

    # Score with jobscout's own rules (config.yaml), and remember it in seen.sqlite like a search does.
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    j = jobscout.Job("added", job["company"], job["title"], job["url"] or f"pasted:{job['company']}:{job['title']}",
                     [job["location"]], jobscout.parse_date(job["posted"]) if job["posted"] else None, job["description"])
    jobscout.Scorer(cfg).score(j)
    db = jobscout.open_db()
    j.new = db.execute("SELECT 1 FROM jobs WHERE key=?", (j.key,)).fetchone() is None
    today = date.today().isoformat()
    db.execute("""INSERT INTO jobs VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                  last_seen=excluded.last_seen, score=excluded.score, verdict=excluded.verdict""",
               (j.key, j.company, j.title, today, today, j.score, j.verdict))
    db.commit()
    db.close()

    jobs_dir = RUNS / ADDED_RUN / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    # Same file name when refreshing, so tailoring runs and fit checks stay linked to it.
    file = old["file"] if old else f"{datetime.now():%Y%m%d_%H%M%S}_{jobscout.slug(j.company, 20)}_{jobscout.slug(j.title)}.txt"
    (jobs_dir / file).write_text(
        f"Title: {j.title}\nCompany: {j.company}\nLocation: {job['location'] or '?'}\n"
        f"Posted: {job['posted'] or '?'}\nURL: {job['url'] or '(pasted text)'}\n\n{j.description}\n", encoding="utf-8")
    row = {"verdict": j.verdict, "score": str(j.score), "new": "NEW" if j.new else "", "company": j.company,
           "title": j.title, "location": job["location"], "posted": job["posted"], "flags": "; ".join(j.flags),
           "gaps": ", ".join(j.gaps), "url": job["url"], "file": file}
    write_added([row] + [r for r in rows if r is not old])        # newest first
    if old:
        clear_fit(ADDED_RUN, file)                                 # the posting changed: check it again
    return {"run": ADDED_RUN, **row, "updated": bool(old), "warning": job.get("warning", "")}


async def run_apply(tid: str):
    t = tasks[tid]
    job_path = RUNS / t["run"] / "jobs" / t["file"]
    async with semaphore:
        t.update(status="running", started=time.time())
        save_tasks()
        cmd = [CLAUDE, "-p", f"/apply {job_path.relative_to(ROOT)}",
               "--allowedTools", "Read,Write,Edit,Bash", "--output-format", "json"]
        if t["model"] != "default":
            cmd += ["--model", t["model"]]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=ROOT, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), RUN_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            t.update(status="failed", error=f"Stopped after {RUN_TIMEOUT // 60} minutes.")
            save_tasks()
            return

    t["seconds"] = round(time.time() - t["started"])
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        t.update(status="failed", error=(err or out).decode(errors="replace")[-2000:] or "No output from claude")
        save_tasks()
        return

    report = result.get("result", "")
    folder = find_output_folder(job_path.read_text(encoding="utf-8"), report, t["started"])
    t.update(report=report, folder=folder, files=folder_files(folder), steps=result.get("num_turns"),
             cost_usd=round(result.get("total_cost_usd") or 0, 2),
             denied=[d.get("tool_name") for d in result.get("permission_denials") or []])
    ok = not result.get("is_error") and "cv.pdf" in t["files"]
    t["status"] = "done" if ok else "failed"
    if not ok:
        t["error"] = "Claude finished, but no compiled CV was found." if not result.get("is_error") else report[:2000]
    save_tasks()


@app.get("/api/tasks")
def list_tasks():
    for t in tasks.values():           # files may change if the user re-compiles by hand
        if t["status"] == "done":
            t["files"] = folder_files(t.get("folder"))
    return sorted(tasks.values(), key=lambda t: -t["created"])


@app.delete("/api/tasks/{tid}")
def forget_task(tid: str):
    """Remove a run from the list (its output folder is kept)."""
    if tasks.get(tid, {}).get("status") in ("queued", "running"):
        raise HTTPException(409, "This run is still going.")
    tasks.pop(tid, None)
    save_tasks()
    return {"ok": True}


@app.get("/output/{folder}/{file}")
def output_file(folder: str, file: str):
    path = OUTPUT / safe(folder) / safe(file)
    if not path.exists() or file not in folder_files(folder):
        raise HTTPException(404)
    media = "application/pdf" if file.endswith(".pdf") else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media)


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")
