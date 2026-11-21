# Job Hunt

A local web page that connects `jobscout` (search and rank postings) with the `/apply`
skill (tailor the CV and cover letter). It runs only on this computer.

```bash
cd webapp
./start.sh            # then open http://localhost:8000 (Ctrl+C stops it)
```

The home page is the **Dashboard**: what's ready to send, what needs a follow-up, weekly activity
and the best matches from the latest search. The steps below happen on the **Search** page.

1. **Search:** choose countries (type in the dropdown to find one), how recent, and keywords, then
   *Search jobs*.
   Each search is saved under `jobscout/runs/<date_time>/` and can be reopened from the dropdown.
2. **Shortlist:** filter, read the posting text, tick the jobs you want. *Check fit* asks Claude
   how well your CV matches a job (0–100%, with each requirement matched or missing). Found a job
   somewhere else? Paste its link above the list: it's scored and added to the *Added from links* list.
3. **Tailor:** runs `claude -p "/apply <job file>"` for each ticked job, 3 at a time
   (about 3 minutes each). Results appear under *Tailored applications*; files go to `output/`.

Tailoring uses your Claude Code login and counts against its usage limits. The
"≈ $ at API prices" figure only shows how heavy a run was; you are not billed it.

---

## How it works

```
Browser (static/index.html, search.html, tracker.html)
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
| `webapp/posting.py` | Reads one job posting from a link, for adding a job to the *Added from links* list |
| `webapp/fit.py`, `webapp/fit_prompt.md` | *Check fit*: builds the prompt, reads Claude's answer, stores results. Edit `fit_prompt.md` to change how fit is judged |
| `webapp/tracker.py` | Tracker rows (one per `output/` folder), statuses, the funnel and the summary numbers |
| `webapp/static/tracker.html` | The Tracker page: stat tiles, Sankey diagram (d3-sankey), applications table |
| `webapp/tracker.json` | Your statuses, applied dates and notes, keyed by output folder. **Back this up**: it's the only place they're stored |
| `webapp/static/index.html` | The Dashboard (home page): KPI tiles, to-do lists, weekly chart, latest search |
| `webapp/static/search.html` | The Search page: search, shortlist and tailoring (HTML, CSS and JavaScript in one file) |
| `webapp/tasks.json` | History of tailoring runs (created on first use; safe to delete) |
| `webapp/start.sh` | Creates `.venv` if needed and starts the server |
| `jobscout/jobscout.py` | Searches career sites, filters, scores, writes `runs/<run>/` |
| `jobscout/config.yaml` | Your countries (with your own place lists), keywords, sources, scoring rules |
| `jobscout/countries.yaml` | Every other country the search offers: other names, main cities, ISO code, region |
| `jobscout/seen.sqlite` | Every posting seen before; drives the NEW marker |
| `~/.claude/skills/apply/SKILL.md` | The tailoring instructions Claude follows |

### API endpoints (in `app.py`)

| Endpoint | Used for |
|---|---|
| `GET /api/settings` | Countries (`config.yaml` and `countries.yaml`), default keywords, CV info |
| `GET /api/runs`, `GET /api/runs/<run>` | List saved searches; load one shortlist |
| `GET /api/companies` | Companies jobscout searches |
| `POST /api/companies/check` | Is a company (name or link) already included? If not, find and test its job board |
| `POST /api/companies` | Add a checked company to `config.yaml` |
| `GET /api/dashboard` | Everything the Dashboard shows, in one call |
| `POST /api/fit` | Start *Check fit* for one or more jobs of a run (results come back with the run's jobs) |
| `POST /api/add-job` | Read a posting from a link (or take pasted text), score it and add it to *Added from links* |
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

### The Dashboard (`/`)

Built from `GET /api/dashboard`, which reuses the tracker rows, the newest search run and the
tailoring history. *Ready to send* lists tailored jobs still at *Not applied yet*; *Follow up* lists
applications at *Applied, waiting* for `FOLLOW_UP_DAYS` (14) days or more. Their buttons set the
status the same way the Tracker does (*Mark applied* dates it today). *Latest search* links to
`/search.html?run=<run>`, which opens that run. The weekly chart counts tailored jobs by the output
folder's date and applications by their applied-on date.

### Adding a job from a link (Search page)

Paste a posting's link above the shortlist and *Add to list*. `POST /api/add-job` reads the posting
with `posting.py`, scores it with jobscout's own `Scorer` (same keywords, gaps, flags and verdicts
from `config.yaml`) and records it in `seen.sqlite`. It then adds it to `jobscout/runs/added/`: the
same `shortlist.csv` and `jobs/*.txt` a search writes. That run shows as *Added from links*, last in
the searches dropdown, and keeps every job you add, newest first. Tick a job there to tailor it,
like any search result. The Dashboard's *Latest search* ignores this list.

How the posting is read:
- **Workday, Greenhouse, Lever, SmartRecruiters** links go through the job system's public API
  (the ones jobscout uses), which gives the full text even where the page itself needs JavaScript.
- **Phenom career sites** (e.g. jobs.thermofisher.com): the job data embedded in the page. Its job
  summary (required skills, education, pay) is added below the description, because these sites
  often leave the skills out of the description itself.
- **Any other site:** the page's schema.org `JobPosting` data, which most career sites and job
  boards include; failing that, the page's main text.
- A posting under 300 words gets a warning: open its posting text and check nothing is missing.
- If neither works (a login wall such as LinkedIn when signed out, or a JavaScript-only page), the
  page asks you to paste the posting with its title and company instead.

Some pages don't say which company the job is at: type it in the *Company (optional)* box next to
the link, or use *Add company* (✎ to change it) on the job in the list. That also updates the
posting file `/apply` reads.

Adding a link that's already in the list reads it again and replaces its entry (its fit check is
cleared). To remove a job from the list, delete its line in `jobscout/runs/added/shortlist.csv`.

### Score vs Fit

The list shows two different numbers:

- **Score** (jobscout, free and instant): points for keywords from `config.yaml` found in the
  posting (+3/+2/+1 each), plus title points, minus 1 per gap. 30+ is *Apply*, 20–29 *Long shot*. It
  says how much the posting *mentions* your skills; it's for sorting many postings quickly.
- **Fit** (Claude, on demand): Claude picks the posting's 5–8 key requirements, checks each against
  `base/cv_master.tex` (and `base/facts.md`) as match / partial / gap, and scores 0–100: 70+ *Good*,
  45–69 *Partial*, under 45 *Poor*. The same kind of analysis `/apply` does before tailoring, without
  tailoring anything. One `claude -p` call per job, about 30 s, counting against your usage limits.
  Click the result to see every requirement. Results are saved in `jobscout/runs/<run>/fit.json`.

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
| Add a country | Most are already in the dropdown (from `jobscout/countries.yaml`). A missing one: add a line to `countries.yaml` with its code, region and cities |
| Search a country more precisely | Add it to `jobscout/config.yaml` → `locations:` with your own place list, e.g. smaller towns or company sites (`Switzerland: [..., Kaiseraugst, Rotkreuz]`). That list replaces the one in `countries.yaml` and the country moves to *Your countries* |
| Add a company | On the page: *Search → Companies searched*, type the name or paste a link to one of its jobs, *Check*, then *Add*. Or edit `config.yaml` → `sources:` by hand (see `jobscout/README.md`, "Adding companies") |
| Remove a company | Delete its line in `config.yaml` (or put `#` in front of it), then restart |
| Search a company only for some countries | *Companies searched*: click the company and type its countries (e.g. `Spain`), or fill in *Only for* when adding it. Saved in `config.yaml` → `company_countries:`. Leave it empty to search the company for every country |
| Change default keywords | `config.yaml` → `search_terms:` (the page remembers your last edits in the browser) |
| Change scoring or verdicts | `config.yaml` → `keywords`, `gaps`, `title_points`, `thresholds` |
| Fix "rate limited" failures | `config.yaml` → raise `workday_pace_seconds` (e.g. 1.5). Lower it for faster searches. A Workday group that still refuses is skipped for the rest of that search |
| Change the default country | `search.html`, search for `defaultCountry` |
| Change the "Posted within" choices | `search.html`, the `<select id="max-age">` options (value = days) |
| Run more or fewer tailoring jobs at once | `app.py` → `MAX_PARALLEL` |
| Change how *Check fit* judges a job | `webapp/fit_prompt.md` (the instructions Claude gets). Thresholds for Good/Partial/Poor: `rating()` in `fit.py` |
| Use a lighter model for *Check fit* | `app.py` → `FIT_MODEL = "sonnet"` |
| Change the stuck-run time limit | `app.py` → `RUN_TIMEOUT` (seconds) |
| Change how tailoring works | Edit the skill: `~/.claude/skills/apply/SKILL.md`. The app needs no change |
| Change when an application shows under *Follow up* | `app.py` → `FOLLOW_UP_DAYS` |
| Use another port | `start.sh` → `--port 8000` |

After editing `app.py` or `config.yaml`, stop the server (Ctrl+C) and start it again. After
editing a page in `static/`, just reload it.

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
