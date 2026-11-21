#!/usr/bin/env python3
"""jobscout — find job postings that match Abel's CV.

Pulls postings from public career-site APIs (Workday, Greenhouse, Lever,
SmartRecruiters, optionally Adzuna), keeps those in the configured locations,
scores them against CV keywords, flags blockers (required languages,
seniority) and writes a ranked shortlist. Each kept posting is also saved as
a text file you can hand to /apply.

Usage:
    python3 jobscout.py                 # full run with config.yaml
    python3 jobscout.py --new-only      # only postings not seen in earlier runs
    python3 jobscout.py --only roche    # limit to sources whose name contains "roche"
    python3 jobscout.py --terms "data scientist" "simulation"
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
TODAY = dt.date.today()
UA = {"User-Agent": "Mozilla/5.0 (jobscout personal job search)", "Accept": "application/json"}


@dataclass
class Job:
    source: str
    company: str
    title: str
    url: str
    locations: list
    posted: dt.date | None = None
    description: str = ""
    detail: object = None        # callable that fills description/locations/posted
    score: int = 0
    verdict: str = ""
    matched: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    new: bool = False

    @property
    def key(self):
        return re.sub(r"[?#].*$", "", self.url)


class NoJobsHere(Exception):
    """The company's career site lists no jobs in the chosen countries, so it isn't searched."""


# ------------------------------------------------------------------ http

SESSION = requests.Session()
SESSION.headers.update(UA)

# Workday rate-limits per server group (e.g. all *.wd5.myworkdayjobs.com sites together), so
# requests to one group are spaced out. Other sites are not limited.
PACE_SECONDS = 0.35
_pace_locks, _pace_next = {}, {}
_pace_guard = threading.Lock()
BLOCKED = set()     # Workday server groups that kept refusing; skipped for the rest of the run


def workday_group(url):
    m = re.search(r"wd\d+\.myworkdayjobs\.com", url)
    return m.group(0) if m else None


def pace(url):
    group = workday_group(url)
    if not group:
        return
    if group in BLOCKED:
        raise RuntimeError(f"skipped: {group} was rate limiting earlier in this search")
    with _pace_guard:
        lock = _pace_locks.setdefault(group, threading.Lock())
    with lock:
        wait = _pace_next.get(group, 0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _pace_next[group] = time.monotonic() + PACE_SECONDS


def http(method, url, attempts=3, **kw):
    """JSON request with retries. Busy servers (429/5xx) get a growing pause: 5, 15 s.
    A Workday group that still refuses is skipped for the rest of the search."""
    for attempt in range(attempts):
        last = attempt == attempts - 1
        pace(url)       # raises at once if this Workday group is already blocked
        try:
            r = SESSION.request(method, url, timeout=25, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                if last:
                    reason = "too many requests (rate limited)" if r.status_code == 429 else "server error"
                    if r.status_code == 429 and workday_group(url):
                        BLOCKED.add(workday_group(url))
                    raise RuntimeError(f"HTTP {r.status_code} {reason} after {attempts} tries")
                wait = r.headers.get("Retry-After", "")
                time.sleep(min(int(wait), 60) if wait.isdigit() else 5 * 3 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if last:
                raise RuntimeError(f"{method} {url}: {e}") from None
            time.sleep(1.5 * (attempt + 1))


def html_to_text(s):
    if not s:
        return ""
    soup = BeautifulSoup(html.unescape(s), "html.parser")
    for tag in soup.find_all(["br", "p", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"]):
        tag.insert_before("\n")
        if tag.name == "li":
            tag.insert(0, "• ")
    text = re.sub(r"[ \t\xa0]+", " ", soup.get_text())
    return re.sub(r"\s*\n\s*(\n\s*)*", lambda m: "\n\n" if m.group(1) else "\n", text).strip()


def parse_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


# ------------------------------------------------------------------ location matching

def load_countries():
    """countries.yaml: every country the search can use, with other names, main cities and region."""
    return yaml.safe_load((HERE / "countries.yaml").read_text(encoding="utf-8"))


def pick_countries(cfg, names):
    """Location words and remote keywords for the chosen countries. A country in config.yaml's
    `locations:` keeps its own word list; any other comes from countries.yaml."""
    catalog = load_countries()
    known = {k.lower(): k for k in catalog["countries"]}
    configured = {k.lower(): k for k in cfg["locations"]}
    locations, remote = {}, []
    for name in names:
        key = name.strip().lower()
        if key in configured:
            country = configured[key]
            locations[country] = cfg["locations"][country]
        elif key in known:
            country = known[key]
            locations[country] = [country, *catalog["countries"][country]["words"]]
        else:
            raise ValueError(f"Unknown country {name!r}: add it to countries.yaml or to locations: in config.yaml.")
        remote.append(country)
        region = catalog["countries"].get(country, {}).get("region")
        remote += catalog["regions"].get(region, ["Europe", "EMEA", "EU"])   # not in the catalog: old default
    # config.yaml's remote_keywords that aren't country names (e.g. a region you added) still apply
    region_words = {w.lower() for ws in catalog["regions"].values() for w in ws}
    remote += [k for k in cfg["remote_keywords"]
               if k.lower() not in configured and k.lower() not in known and k.lower() not in region_words]
    return locations, list(dict.fromkeys(remote))


class Locator:
    def __init__(self, cfg):
        self.countries = list(cfg["locations"])
        words = [w for ws in cfg["locations"].values() for w in ws]
        self.place = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I)
        self.remote = re.compile(r"\bremote\b", re.I)
        self.remote_ok = re.compile(r"\b(" + "|".join(map(re.escape, cfg["remote_keywords"])) + r")\b", re.I)

    def matches(self, locations):
        text = " | ".join(l for l in locations if l)
        if self.place.search(text):
            return True
        return bool(self.remote.search(text) and self.remote_ok.search(text))

    def facet_ok(self, descriptor):
        return bool(self.place.search(descriptor or ""))


# ------------------------------------------------------------------ sources

def workday(tenant, host, site, name, terms, cfg, loc):
    base = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    public = f"https://{host}.myworkdayjobs.com/{site}"
    company = name or (tenant.capitalize() if len(tenant) > 3 else tenant.upper())

    # Use the tenant's location facet to filter server-side when possible.
    first = http("POST", f"{base}/jobs", json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    facets = {}

    has_country_filter, has_remote = [], []

    def walk(fs):
        for f in fs:
            vals = f.get("values", [])
            if vals and "facetParameter" in vals[0]:
                walk(vals)
            elif f.get("facetParameter") in ("locationCountry", "locations", "primaryLocation", "Location_Country"):
                if vals:                    # the site lists where its jobs are
                    has_country_filter.append(True)
                if any("remote" in str(v.get("descriptor", "")).lower() for v in vals):
                    has_remote.append(True)
                ids = [v["id"] for v in vals if loc.facet_ok(v.get("descriptor"))]
                if ids:
                    facets.setdefault(f["facetParameter"], ids)

    walk(first.get("facets", []))
    # The site lists the countries or places it has jobs in, none is ours and none is remote: searching
    # it worldwide would only download postings to throw away.
    if has_country_filter and not facets and not has_remote:
        raise NoJobsHere()
    if "locationCountry" in facets or "Location_Country" in facets:
        facets = {k: v for k, v in facets.items() if k in ("locationCountry", "Location_Country")}
    elif facets:
        k = next(iter(facets))
        facets = {k: facets[k]}

    seen, jobs = set(), []
    for term in terms:
        for offset in range(0, cfg["max_results_per_term"], 20):
            d = http("POST", f"{base}/jobs", json={"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": term})
            posts = d.get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                j = Job("workday", company, p.get("title", ""), public + path, [p.get("locationsText", "")])

                def fill(j=j, path=path):
                    info = http("GET", base + path)["jobPostingInfo"]
                    j.description = html_to_text(info.get("jobDescription"))
                    country = (info.get("jobRequisitionLocation") or {}).get("country", {}).get("descriptor", "")
                    j.locations = [info.get("location", ""), country, *info.get("additionalLocations", [])]
                    j.posted = parse_date(info.get("startDate"))
                    j.url = info.get("externalUrl") or j.url

                j.detail = fill
                jobs.append(j)
            if len(posts) < 20 or offset + 20 >= d.get("total", 0):
                break
    return jobs


def greenhouse(board, terms, cfg, loc):
    d = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
    out = []
    for p in d.get("jobs", []):
        locs = [p.get("location", {}).get("name", "")] + [o.get("name", "") for o in p.get("offices", [])]
        out.append(Job("greenhouse", board.capitalize(), p.get("title", ""), p.get("absolute_url", ""), locs,
                       parse_date(p.get("updated_at")), html_to_text(p.get("content"))))
    return out


def lever(company, terms, cfg, loc):
    d = http("GET", f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    out = []
    for p in d if isinstance(d, list) else []:
        cat = p.get("categories", {})
        locs = [cat.get("location", ""), *cat.get("allLocations", []), p.get("country", ""), p.get("workplaceType", "")]
        desc = p.get("descriptionPlain", "") + "\n" + "\n".join(
            l.get("text", "") + "\n" + html_to_text(l.get("content")) for l in p.get("lists", []))
        created = dt.date.fromtimestamp(p["createdAt"] / 1000) if p.get("createdAt") else None
        out.append(Job("lever", company.capitalize(), p.get("text", ""), p.get("hostedUrl", ""), locs, created, desc))
    return out


def smartrecruiters(company, terms, cfg, loc):
    base = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    codes = {name: c["code"] for name, c in load_countries()["countries"].items()}
    seen, out = set(), []
    for country in loc.countries:
        cc = codes.get(country)
        for term in terms:
            params = {"q": term, "limit": 100}
            if cc:
                params["country"] = cc
            for p in http("GET", base, params=params).get("content", []):
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                l = p.get("location", {})
                locs = [l.get("city", ""), l.get("country", ""), country if cc else "", "remote" if l.get("remote") else ""]
                j = Job("smartrecruiters", company, p.get("name", ""),
                        f"https://jobs.smartrecruiters.com/{company}/{p['id']}", locs, parse_date(p.get("releasedDate")))

                def fill(j=j, pid=p["id"]):
                    sec = http("GET", f"{base}/{pid}").get("jobAd", {}).get("sections", {})
                    j.description = "\n\n".join(html_to_text(s.get("text")) for s in sec.values() if isinstance(s, dict))

                j.detail = fill
                out.append(j)
    return out


def adzuna(cc, terms, cfg, loc):
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    seen, out = set(), []
    for term in terms:
        d = http("GET", f"https://api.adzuna.com/v1/api/jobs/{cc}/search/1",
                 params={"app_id": app_id, "app_key": key, "what": term, "results_per_page": 50,
                         "max_days_old": cfg["max_age_days"], "content-type": "application/json"})
        for p in d.get("results", []):
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            locs = [p.get("location", {}).get("display_name", ""), *p.get("location", {}).get("area", [])]
            out.append(Job("adzuna", p.get("company", {}).get("display_name", "?"), html_to_text(p.get("title")),
                           p.get("redirect_url", ""), locs, parse_date(p.get("created")),
                           html_to_text(p.get("description")) + "\n[Adzuna snippet — open the link for the full posting]"))
    return out


def build_tasks(cfg, only, countries=None):
    """(name, function, args) per company. With `countries`, companies tagged in company_countries
    for other countries only are left out."""
    src = cfg["sources"]
    # Workday entries: [tenant, host, site] or [tenant, host, site, "Display name"]
    tasks = [(f"workday:{t[0]}", workday, (t[0], t[1], t[2], t[3] if len(t) > 3 else None))
             for t in src.get("workday", [])]
    tasks += [(f"greenhouse:{b}", greenhouse, (b,)) for b in src.get("greenhouse", [])]
    tasks += [(f"lever:{b}", lever, (b,)) for b in src.get("lever", [])]
    tasks += [(f"smartrecruiters:{b}", smartrecruiters, (b,)) for b in src.get("smartrecruiters", [])]
    if os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
        tasks += [(f"adzuna:{c}", adzuna, (c,)) for c in src.get("adzuna_countries", [])]
    if only:
        tasks = [t for t in tasks if any(o.lower() in t[0].lower() for o in only)]
    tags = {k.lower(): v for k, v in (cfg.get("company_countries") or {}).items()}
    if countries and tags:
        wanted = {c.lower() for c in countries}
        skipped = [(t[0], tags[t[0].lower()]) for t in tasks
                   if t[0].lower() in tags and not wanted & {c.lower() for c in tags[t[0].lower()]}]
        if skipped:
            print(f"Not searching {len(skipped)} companies tagged for other countries: "
                  + ", ".join(f"{n.split(':', 1)[1]} ({', '.join(c)})" for n, c in skipped))
        tasks = [t for t in tasks if (t[0], tags.get(t[0].lower())) not in skipped]
    return tasks


# ------------------------------------------------------------------ scoring

REQUIRED = re.compile(r"fluen|native|mother tongue|business[- ]level|proficien|\bC1\b|\bC2\b|excellent|strong|"
                      r"required|must|mandatory|essential|working knowledge|command of|written and (spoken|oral)|"
                      r"(spoken|verbal) and written|speak", re.I)
OPTIONAL = re.compile(r"\bplus\b|advantage|asset|nice[- ]to[- ]have|preferr|desirabl|beneficial|bonus|ideally|"
                      r"is a benefit|would be great|appreciated|optional", re.I)
STOPWORDS = {
    "German": "und der die das mit für wir sie ihre eine sind auf zu von".split(),
    "French": "et les des vous nous pour une dans avec sur est votre".split(),
    "Dutch": "en het van een wij je jouw voor met zijn naar bij".split(),
    "Polish": "oraz się jest dla nas na w z do jako lub".split(),
    "Spanish": "y los las del con para una por nuestro tu en es".split(),
}
ENGLISH = "and the of with for we you your our to in is".split()


def posting_language(text):
    words = re.findall(r"[a-zäöüßéèàçñóíáúłąęśćżź]+", text.lower())[:600]
    counts = {lang: sum(words.count(w) for w in sw) for lang, sw in STOPWORDS.items()}
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return lang if n > 1.3 * sum(words.count(w) for w in ENGLISH) and n > 10 else "English"


class Scorer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kw = [(re.compile(p, re.I), int(w), p) for w, ps in cfg["keywords"].items() for p in ps]
        self.gaps = [(re.compile(p, re.I), p) for p in cfg["gaps"]]
        self.title = [(re.compile(p, re.I), w) for p, w in cfg["title_points"].items()]
        self.lacking = cfg["lacking_languages"]
        self.partial = cfg["partial_languages"]

    @staticmethod
    def label(pattern):
        return re.sub(r"\\b|\\w[+*]|\[- \]| \?|\\|\(|\)|\?|s\?$", "", pattern).replace("[sz]", "s").replace("|", "/")

    def language_flags(self, text):
        sentences = re.split(r"(?<=[.;!?])\s+|\n+", text)
        block, downgrade, notes = [], [], []
        for lang in self.lacking + self.partial:
            rx = re.compile(rf"\b{lang}\b", re.I)
            hits = [s for s in sentences if rx.search(s)]
            # "German and English (must). Italian is a strong plus." → judge the clause around the language.
            clauses = [c for s in hits for c in re.split(r"(?<=\))\s*\.?\s+|,\s+(?:and |while |but )?", s) if rx.search(c)] or hits
            if any(REQUIRED.search(c) and not OPTIONAL.search(c) for c in clauses):
                (block if lang in self.lacking else downgrade).append(f"{lang} required")
            elif any(OPTIONAL.search(c) for c in clauses):
                notes.append(f"{lang} a plus")
        lang = posting_language(text)
        if lang != "English":
            (downgrade if lang in self.partial else block).append(f"posting in {lang}")
        return block, downgrade, notes

    def years_required(self, text):
        yrs = [int(m.group(1)) for m in re.finditer(
            r"(\d{1,2})\s*\+?\s*(?:[-–]\s*\d{1,2}\s*)?(?:years?|yrs)(?:'|’)?\s+(?:of\s+)?(?:\w+\s+){0,4}?experience", text, re.I)]
        return max((y for y in yrs if y < 30), default=0)

    def score(self, j):
        text = j.title + "\n" + j.description
        j.matched = [(self.label(p), w) for rx, w, p in self.kw if rx.search(text)]
        j.gaps = [self.label(p) for rx, p in self.gaps if rx.search(text)]
        title_pts = sum(w for rx, w in self.title if rx.search(j.title))
        j.score = sum(w for _, w in j.matched) - len(j.gaps) + title_pts

        block, downgrade, notes = self.language_flags(text)
        yrs = self.years_required(j.description)
        if yrs > self.cfg["experience_years"] + 3:
            downgrade.append(f"{yrs}+ yrs exp")
        if len(j.description) < 300:
            notes.append("short/no description")
        j.flags = block + downgrade + notes

        t = self.cfg["thresholds"]
        levels = ["Apply", "Long shot", "Skip"]
        lvl = 0 if j.score >= t["apply"] else 1 if j.score >= t["long_shot"] else 2
        if block:
            lvl = 2
        elif downgrade:
            lvl = min(2, lvl + 1)
        j.verdict = levels[lvl]


# ------------------------------------------------------------------ storage & report

def open_db():
    db = sqlite3.connect(HERE / "seen.sqlite")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, company TEXT, title TEXT,
                  first_seen TEXT, last_seen TEXT, score INT, verdict TEXT)""")
    return db


def slug(s, n=60):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:n]


def write_report(jobs, run_dir, stats):
    jobs_dir = run_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for old in jobs_dir.glob("*.txt"):      # same-day re-run replaces the earlier one
        old.unlink()
    order = {"Apply": 0, "Long shot": 1, "Skip": 2}
    jobs.sort(key=lambda j: (order[j.verdict], -j.score))

    for i, j in enumerate(jobs, 1):
        j.file = jobs_dir / f"{i:03d}_{slug(j.company, 20)}_{slug(j.title)}.txt"
        j.file.write_text(
            f"Title: {j.title}\nCompany: {j.company}\nLocation: {'; '.join(l for l in j.locations if l)}\n"
            f"Posted: {j.posted or '?'}\nURL: {j.url}\n\n{j.description}\n", encoding="utf-8")

    with open(run_dir / "shortlist.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["verdict", "score", "new", "company", "title", "location", "posted", "flags", "gaps", "url", "file"])
        for j in jobs:
            w.writerow([j.verdict, j.score, "NEW" if j.new else "", j.company, j.title,
                        "; ".join(l for l in j.locations if l), j.posted or "", "; ".join(j.flags),
                        ", ".join(j.gaps), j.url, j.file.name])

    def cell(s):
        return str(s).replace("|", "/").replace("\n", " ")

    lines = [f"# Job shortlist — {TODAY}", "",
             f"{stats['fetched']} listings scanned across {stats['sources']} sources → {stats['in_location']} in target "
             f"locations → **{stats['Apply']} Apply**, {stats['Long shot']} Long shot, {stats['Skip']} Skip"
             f"{' (Skips hidden; use --include-skips)' if stats['hide_skips'] else ''}.",
             "", "Verdicts are a keyword pre-screen; `/apply` makes the real call. "
             "To tailor for a job: `/apply <path to its jobs/*.txt file>` (or the link).", ""]
    if stats["errors"]:
        lines += ["Sources that failed: " + ", ".join(stats["errors"]), ""]
    for verdict in ("Apply", "Long shot", "Skip"):
        group = [j for j in jobs if j.verdict == verdict]
        if not group:
            continue
        lines += [f"## {verdict} ({len(group)})", "",
                  "| # | Score | | Company | Title | Location | Posted | Flags / gaps | Top matches |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for j in group:
            n = j.file.name[:3]
            loc = ", ".join(dict.fromkeys(l for l in j.locations if l))[:60]
            top = ", ".join(k for k, _ in sorted(j.matched, key=lambda m: -m[1])[:6])
            fg = "; ".join(j.flags + ([f"gaps: {', '.join(j.gaps)}"] if j.gaps else []))
            lines.append(f"| {n} | {j.score} | {'NEW' if j.new else ''} | {cell(j.company)} | "
                         f"[{cell(j.title)}]({j.url}) | {cell(loc)} | {j.posted or ''} | {cell(fg)} | {cell(top)} |")
        lines.append("")
    (run_dir / "shortlist.md").write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=HERE / "config.yaml")
    ap.add_argument("--only", nargs="+", help="only sources whose name contains one of these")
    ap.add_argument("--terms", nargs="+", help="override search_terms")
    ap.add_argument("--new-only", action="store_true", help="hide postings seen in earlier runs")
    ap.add_argument("--include-skips", action="store_true", help="also list Skip verdicts in the report")
    ap.add_argument("--countries", nargs="+", help="search these countries (from config.yaml locations or countries.yaml)")
    ap.add_argument("--run-dir", type=Path, help="where to write the report (default: runs/<today>)")
    ap.add_argument("--max-age-days", type=int, help="only postings from the last N days (overrides config)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    global PACE_SECONDS
    PACE_SECONDS = float(cfg.get("workday_pace_seconds", PACE_SECONDS))
    if args.max_age_days:
        cfg["max_age_days"] = args.max_age_days
    if args.countries:
        try:
            cfg["locations"], cfg["remote_keywords"] = pick_countries(cfg, args.countries)
        except ValueError as e:
            sys.exit(str(e))
    cfg["title_exclude"] = re.compile(re.sub(r"\n\s*", "", cfg["title_exclude"]), re.I)
    terms = args.terms or cfg["search_terms"]
    loc, scorer = Locator(cfg), Scorer(cfg)
    tasks = build_tasks(cfg, args.only, list(cfg["locations"]) if args.countries else None)
    if not tasks:
        sys.exit("No sources selected.")

    # 1. search all sources in parallel
    stats = {"sources": len(tasks), "errors": []}
    jobs = []
    print(f"Searching {len(tasks)} sources for {len(terms)} terms...")
    with ThreadPoolExecutor(cfg["max_workers"]) as ex:
        futs = {name: ex.submit(fn, *a, terms, cfg, loc) for name, fn, a in tasks}
        for name, fut in futs.items():
            try:
                found = fut.result()
                for j in found:
                    j.task = name               # which company it came from, for the summary below
                jobs += found
                print(f"  {name:32s} {len(found):4d}")
            except NoJobsHere:
                stats.setdefault("no_jobs_here", []).append(name)
                print(f"  {name:32s}    - no jobs in {', '.join(loc.countries)}")
            except Exception as e:
                stats["errors"].append(name)
                print(f"  {name:32s} FAILED: {e}", file=sys.stderr)
    stats["fetched"] = len(jobs)

    # 2. cheap filters before fetching full postings
    uniq = {}
    for j in jobs:
        uniq.setdefault(j.key, j)
    jobs = [j for j in uniq.values() if not cfg["title_exclude"].search(j.title)]

    # Workday list pages only say "3 Locations", so their location is checked after the detail fetch.
    def pre_ok(j):
        if j.detail and j.source == "workday":
            return True
        return loc.matches(j.locations)

    jobs = [j for j in jobs if pre_ok(j)]

    # 3. fetch full postings
    need = [j for j in jobs if j.detail]
    print(f"Fetching {len(need)} full postings...")

    def fetch(j):
        try:
            j.detail()
        except Exception as e:
            j.flags.append(f"detail failed: {e}")

    with ThreadPoolExecutor(cfg["max_workers"]) as ex:
        list(ex.map(fetch, need))

    cutoff = TODAY - dt.timedelta(days=cfg["max_age_days"])
    jobs = [j for j in jobs if loc.matches(j.locations) and (j.posted is None or j.posted >= cutoff)]
    stats["in_location"] = len(jobs)
    with_jobs = sorted({getattr(j, "task", j.company).split(":", 1)[-1] for j in jobs})
    print(f"\nCompanies with jobs in {', '.join(loc.countries)}: {len(with_jobs)} of {len(tasks)} searched"
          + (f" ({', '.join(with_jobs)})" if with_jobs else "")
          + (". Few of your companies hire here: consider adding local ones (Companies searched)."
             if len(with_jobs) < max(3, len(tasks) // 5) else "."))

    # 4. score, remember, report
    for j in jobs:
        scorer.score(j)
    db = open_db()
    now = TODAY.isoformat()
    for j in jobs:
        row = db.execute("SELECT first_seen FROM jobs WHERE key=?", (j.key,)).fetchone()
        j.new = row is None
        db.execute("""INSERT INTO jobs VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                      last_seen=excluded.last_seen, score=excluded.score, verdict=excluded.verdict""",
                   (j.key, j.company, j.title, now, now, j.score, j.verdict))
    db.commit()

    stats.update({v: sum(j.verdict == v for j in jobs) for v in ("Apply", "Long shot", "Skip")},
                 hide_skips=not args.include_skips)
    shown = [j for j in jobs if (j.new or not args.new_only) and (j.verdict != "Skip" or args.include_skips)]
    run_dir = args.run_dir or HERE / "runs" / now
    write_report(shown, run_dir, stats)

    print(f"\n{len(jobs)} postings in target locations; "
          + ", ".join(f"{v}: {sum(j.verdict == v for j in jobs)}" for v in ("Apply", "Long shot", "Skip")))
    print(f"Report: {run_dir / 'shortlist.md'}")
    for j in [j for j in shown if j.verdict == "Apply"][:15]:
        print(f"  {j.score:3d} {'NEW ' if j.new else '    '}{j.company:12.12s} {j.title[:70]}")


if __name__ == "__main__":
    main()
