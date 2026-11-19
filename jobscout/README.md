# jobscout

Finds job postings that match your CV and ranks them, so `/apply` only gets the good ones.

```bash
cd jobscout
python3 jobscout.py                  # full run (~2 min)
python3 jobscout.py --new-only       # only postings not seen in an earlier run
python3 jobscout.py --include-skips  # also list Skip verdicts (with the reason)
python3 jobscout.py --only roche novartis --terms "simulation" "data scientist"
```

Output goes to `runs/<date>/`:
- `shortlist.md`: Apply / Long shot tables with score, NEW marker, flags (required languages,
  seniority), gaps (MLOps, AWS, GMP …), and the top matching CV keywords. Links open the posting.
- `shortlist.csv`: the same data, for Excel.
- `jobs/NNN_Company_Title.txt`: the full posting text. Tailor for one with
  `/apply jobscout/runs/<date>/jobs/NNN_….txt`.
  This avoids Workday pages that WebFetch can't read.

`seen.sqlite` remembers every posting, so later runs mark new ones as **NEW**.

## How it works

1. **Search.** Public career-site JSON APIs, the same ones the companies' own job pages call.
   No login and no LinkedIn/Indeed scraping (their terms forbid it, and they block bots).
   - Workday: Roche, Novartis, Lonza, AstraZeneca, GSK, Pfizer, J&J, Sanofi, Thermo Fisher,
     Philips, HP, ING, ABB, Accenture
   - Greenhouse: Isomorphic Labs, Celonis, Datadog, Typeform, Recursion
   - SmartRecruiters: Bosch
   - Adzuna (optional): an aggregator of many job boards. Get a free key at
     https://developer.adzuna.com, then `export ADZUNA_APP_ID=… ADZUNA_APP_KEY=…`.
2. **Filter** by location (`locations` in config.yaml), title exclusions and posting age.
3. **Score** each posting: CV keywords found (3/2/1 points), plus title points, minus 1 per gap.
4. **Verdict:** score ≥ 30 → Apply, ≥ 20 → Long shot, otherwise Skip. Then:
   - a *required* language you lack (German, French, Dutch, Polish …), or a posting written in
     one, → **Skip**;
   - required fluent Spanish/Catalan, or more than 9 years' experience required, → one level down;
   - "German is a plus" is only noted.

The verdict is a keyword pre-screen. `/apply` does the real requirement-by-requirement analysis.

## Adding companies

- **Workday:** open the company's careers site. If the URL looks like
  `https://<tenant>.wd3.myworkdayjobs.com/<site>`, add `[<tenant>, <tenant>.wd3, <site>]`.
- **Greenhouse:** `job-boards.greenhouse.io/<board>` → add `<board>` under `greenhouse`.
- **Lever:** `jobs.lever.co/<company>` → add under `lever`.
- **SmartRecruiters:** `jobs.smartrecruiters.com/<Company>` → add under `smartrecruiters`.

Test a new entry with `python3 jobscout.py --only <name>`. A failing source is listed at the top
of the report, and the rest of the run continues.
