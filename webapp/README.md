# Job Hunt

A local web page that connects `jobscout` (search and rank postings) with the `/apply`
skill (tailor the CV and cover letter). It runs only on this computer.

```bash
cd webapp
./start.sh            # then open http://localhost:8000 (Ctrl+C stops it)
```

1. **Search:** choose countries, how recent, and keywords, then *Search jobs*.
   Each search is saved under `jobscout/runs/<date_time>/` and can be reopened from the dropdown.
2. **Shortlist:** filter, read the posting text, tick the jobs you want.
3. **Tailor:** runs `claude -p "/apply <job file>"` for each ticked job, 3 at a time
   (about 3 minutes each). Results appear under *Tailored applications*; files go to `output/`.

Tailoring uses your Claude Code login and counts against its usage limits. The
"≈ $ at API prices" figure only shows how heavy a run was; you are not billed it.

---

## How it works

```
Browser (static/index.html)
   │  fetch() calls to /api/...
   ▼
webapp/app.py  (FastAPI server on localhost:8000)
   ├─ search  → runs  python jobscout/jobscout.py --countries … --terms … --max-age-days … --run-dir …
   │             → reads jobscout/runs/<run>/shortlist.csv and jobs/*.txt
   └─ tailor  → runs  claude -p "/apply jobscout/runs/<run>/jobs/<file>.txt" --output-format json
                 → finds the new output/<folder>/ and serves its PDFs
```

The server never searches or tailors by itself. It starts the same two tools you can run in
a terminal and shows their results, so each part can be tested on its own.

### Files

| File | What it does |
|---|---|
| `webapp/app.py` | The server: API endpoints, starts jobscout and `claude`, keeps the run history |
| `webapp/companies.py` | Lists, checks and adds companies in `jobscout/config.yaml` |
| `webapp/tracker.py` | Tracker rows (one per `output/` folder), statuses, the funnel and the summary numbers |
| `webapp/static/tracker.html` | The Tracker page: stat tiles, Sankey diagram (d3-sankey), applications table |
| `webapp/tracker.json` | Your statuses, applied dates and notes, keyed by output folder. **Back this up**: it's the only place they're stored |
| `webapp/static/index.html` | The whole page: HTML, CSS and JavaScript in one file |
| `webapp/tasks.json` | History of tailoring runs (created on first use; safe to delete) |
| `webapp/start.sh` | Creates `.venv` if needed and starts the server |
| `jobscout/jobscout.py` | Searches career sites, filters, scores, writes `runs/<run>/` |
| `jobscout/config.yaml` | Countries, keywords, sources, scoring rules |
| `jobscout/seen.sqlite` | Every posting seen before; drives the NEW marker |
| `~/.claude/skills/apply/SKILL.md` | The tailoring instructions Claude follows |

### API endpoints (in `app.py`)

| Endpoint | Used for |
|---|---|
| `GET /api/settings` | Countries and default keywords from `config.yaml`, CV info |
| `GET /api/runs`, `GET /api/runs/<run>` | List saved searches; load one shortlist |
| `GET /api/companies` | Companies jobscout searches |
| `POST /api/companies/check` | Is a company (name or link) already included? If not, find and test its job board |
| `POST /api/companies` | Add a checked company to `config.yaml` |
| `GET /api/tracker` | Tracker rows, summary numbers, Sankey nodes/links, status list |
| `POST /api/tracker` | Set status, applied date or notes for one or more output folders |
| `GET /api/runs/<run>/jobs/<file>` | Full text of one posting |
| `POST /api/search`, `GET /api/search/<id>` | Start a jobscout search; poll its progress |
| `POST /api/apply` | Queue tailoring runs for the ticked jobs |
| `GET /api/tasks`, `DELETE /api/tasks/<id>` | Tailoring status and results; remove one from the list |
| `GET /output/<folder>/<file>` | Serve a PDF or text file from `output/` |

### What happens on *Search jobs*

1. The page sends countries, keywords, days and options to `POST /api/search`.
2. `app.py` runs jobscout with `--run-dir runs/<date_time>`, so searches never overwrite each other.
3. The page polls `GET /api/search/<id>` every 2 seconds and shows jobscout's printed output.
4. When it's done, the page loads `shortlist.csv` from the new run.

### What happens on *Tailor*

1. `POST /api/apply` creates one entry per job in `tasks.json` (status `queued`).
2. Each entry waits for a free slot (`MAX_PARALLEL`), then runs `claude -p "/apply …"` from the
   `CoverLetters` folder, allowed to use Read, Write, Edit and Bash without asking.
3. When `claude` finishes, `app.py` reads its JSON output (report, steps, cost) and finds the
   output folder: first from the path Claude mentions, otherwise the newest folder whose
   `job_description.txt` matches the posting.
4. The run is `done` if `cv.pdf` exists there, otherwise `failed` with the reason.
5. The page polls `GET /api/tasks` every 5 seconds while anything is running.

---

### The Tracker page (`/tracker.html`)

Every folder in `output/` is one row: company, role and location come from the first line of its
`analysis.md`. A folder with a tailored CV or letter starts as *Not applied yet*; one without
(`/apply` judged it a poor fit) starts as *Skipped*. You change the status in the table.

The Sankey diagram is built from the statuses: each status stands for a path, e.g. *Rejected after
interview* = Analysed → Tailored → Applied → Interview → Rejected after interview. The paths are
the `STATUSES` table at the top of `tracker.py`; add or rename a status there.

Choosing a status beyond *Not applied yet* without an applied date sets the date to today.

## Common changes

| I want to… | Change |
|---|---|
| Add a country | `jobscout/config.yaml` → `locations:` add `Germany: [Germany, Berlin, Munich, …]`. It appears in the dropdown automatically. For SmartRecruiters, also add its code in `smartrecruiters()` in `jobscout.py` |
| Add a company | On the page: *Search → Companies searched*, type the name or paste a link to one of its jobs, *Check*, then *Add*. Or edit `config.yaml` → `sources:` by hand (see `jobscout/README.md`, "Adding companies") |
| Remove a company | Delete its line in `config.yaml` (or put `#` in front of it), then restart |
| Change default keywords | `config.yaml` → `search_terms:` (the page remembers your last edits in the browser) |
| Change scoring or verdicts | `config.yaml` → `keywords`, `gaps`, `title_points`, `thresholds` |
| Fix "rate limited" failures | `config.yaml` → raise `workday_pace_seconds` (e.g. 1.5). Lower it for faster searches. A Workday group that still refuses is skipped for the rest of that search |
| Change the default country | `index.html`, search for `defaultCountry` |
| Change the "Posted within" choices | `index.html`, the `<select id="max-age">` options (value = days) |
| Run more or fewer tailoring jobs at once | `app.py` → `MAX_PARALLEL` |
| Change the stuck-run time limit | `app.py` → `RUN_TIMEOUT` (seconds) |
| Change how tailoring works | Edit the skill: `~/.claude/skills/apply/SKILL.md`. The app needs no change |
| Use another port | `start.sh` → `--port 8000` |

After editing `app.py` or `config.yaml`, stop the server (Ctrl+C) and start it again. After
editing `index.html`, just reload the page.

## When something goes wrong

- **Search fails:** read the log on the page. Run jobscout by hand to see the full error:
  `cd ../jobscout && python3 jobscout.py --countries Spain --terms "data scientist" --max-age-days 7`.
  One failing company is listed and skipped; the rest of the search still runs.
- **Tailoring fails:** the card shows the error. Test the same job in a terminal:
  `cd .. && claude -p "/apply jobscout/runs/<run>/jobs/<file>.txt" --allowedTools "Read,Write,Edit,Bash"`.
  "Blocked tools" on a card means the skill needed a tool that isn't in the allowed list in `run_apply()`.
- **Server errors:** they print in the terminal where `start.sh` runs.
- **Start fresh:** delete `webapp/tasks.json` (history only; output folders are kept).

## Things to know

- "Posted within" uses the date each site reports, in whole days: *24 hours* means today or
  yesterday. Greenhouse reports the last-updated date, so an old posting edited recently can
  appear. Postings with no date are kept.
- Running the same job twice on one day replaces that day's output folder for it.
- Every search updates `seen.sqlite`, so a posting is NEW only in the first search that finds it.
- `CLAUDE_BIN=/path/to/claude ./start.sh` uses a different `claude` executable (useful for testing).
