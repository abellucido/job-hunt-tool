"""The companies jobscout searches: list them, check whether one is included, add new ones.

Companies live in jobscout/config.yaml under `sources:`. New ones are added as text, so the
comments in that file are kept. Supported systems: Workday, Greenhouse, Lever, SmartRecruiters.
"""
import difflib
import re
import shutil
import unicodedata
from datetime import date
from pathlib import Path

import requests
import yaml

UA = {"User-Agent": "Mozilla/5.0 (jobscout personal job search)", "Accept": "application/json"}
SYSTEMS = {"workday": "Workday", "greenhouse": "Greenhouse", "lever": "Lever", "smartrecruiters": "SmartRecruiters"}


def norm(s: str) -> str:
    """"Thermo Fisher" → "thermofisher", "Enagás" → "enagas"."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def workday_name(entry: list) -> str:
    if len(entry) > 3 and entry[3]:
        return str(entry[3])
    tenant = entry[0]
    return tenant.capitalize() if len(tenant) > 3 else tenant.upper()


# ------------------------------------------------------------------ list

def list_companies(config_path: Path) -> list[dict]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    src = cfg.get("sources", {})
    out = [{"name": workday_name(e), "system": "workday", "id": e[0], "detail": f"{e[1]} · {e[2]}"}
           for e in src.get("workday", [])]
    for system in ("greenhouse", "lever", "smartrecruiters"):
        out += [{"name": s[:1].upper() + s[1:], "system": system, "id": s, "detail": s} for s in src.get(system) or []]
    return sorted(out, key=lambda c: c["name"].lower())


def find_existing(companies: list[dict], name: str = "", system: str = "", ident: str = "") -> dict | None:
    """Match by system + id (from a link), or by name."""
    for c in companies:
        if system and ident and c["system"] == system and c["id"].lower() == ident.lower():
            return c
    q = norm(name)
    if q:
        for c in companies:
            if q in (norm(c["name"]), norm(c["id"])):
                return c
    return None


def similar(companies: list[dict], name: str) -> list[str]:
    names = {norm(c["name"]): c["name"] for c in companies}
    return [names[n] for n in difflib.get_close_matches(norm(name), names, n=3, cutoff=0.7)]


# ------------------------------------------------------------------ links

def parse_link(text: str) -> dict | None:
    """Recognise a career-site or job link. Returns {system, id, ...} or None."""
    m = re.search(r"([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?([A-Za-z0-9_\-]+)", text)
    if m and m.group(3).lower() not in ("wday", "job"):
        tenant, group, site = m.groups()
        return {"system": "workday", "id": tenant, "host": f"{tenant}.{group}", "site": site}
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_\-]+)", text)
    if m and m.group(1) not in ("embed", "v1"):
        return {"system": "greenhouse", "id": m.group(1).lower()}
    m = re.search(r"jobs\.lever\.co/([A-Za-z0-9_\-]+)", text)
    if m:
        return {"system": "lever", "id": m.group(1).lower()}
    m = re.search(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_\-]+)", text)
    if m:
        return {"system": "smartrecruiters", "id": m.group(1)}
    return None


def unsupported_link(text: str) -> str | None:
    if "myworkdaysite.com" in text:
        return "This Workday address style (myworkdaysite.com) isn't supported yet."
    if "eu.lever.co" in text:
        return "Lever's EU sites (jobs.eu.lever.co) aren't supported yet."
    if re.search(r"successfactors|taleo|avature|icims|phenom|oraclecloud|workable|teamtailor", text, re.I):
        return "This company uses a job system jobscout can't read (only Workday, Greenhouse, Lever and SmartRecruiters)."
    return None


# ------------------------------------------------------------------ verify

def verify(src: dict) -> dict:
    """One request to the job system. Returns {ok, jobs, name, error, unverified}."""
    try:
        if src["system"] == "workday":
            url = f"https://{src['host']}.myworkdayjobs.com/wday/cxs/{src['id']}/{src['site']}/jobs"
            r = requests.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                              headers=UA, timeout=20)
            if r.status_code == 429:
                return {"ok": True, "unverified": True,
                        "error": "Workday is rate limiting right now, so the site couldn't be tested."}
            if not r.ok:
                return {"ok": False, "error": f"That Workday site didn't answer (HTTP {r.status_code}). Check the link."}
            return {"ok": True, "jobs": r.json().get("total")}
        if src["system"] == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{src['id']}/jobs", headers=UA, timeout=20)
            if not r.ok:
                return {"ok": False, "error": "No Greenhouse job board with that name."}
            board = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{src['id']}", headers=UA, timeout=20)
            return {"ok": True, "jobs": len(r.json().get("jobs", [])), "name": board.json().get("name") if board.ok else None}
        if src["system"] == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{src['id']}", params={"mode": "json"}, headers=UA, timeout=20)
            if not r.ok or not isinstance(r.json(), list):
                return {"ok": False, "error": "No Lever job board with that name."}
            return {"ok": True, "jobs": len(r.json())}
        if src["system"] == "smartrecruiters":
            r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{src['id']}/postings",
                             params={"limit": 1}, headers=UA, timeout=20)
            if not r.ok:
                return {"ok": False, "error": "No SmartRecruiters company with that name."}
            d = r.json()
            name = d["content"][0]["company"]["name"] if d.get("content") else None
            return {"ok": True, "jobs": d.get("totalFound", 0), "name": name}
    except (requests.RequestException, ValueError, KeyError) as e:
        return {"ok": False, "error": f"Couldn't reach the job system: {e}"}
    return {"ok": False, "error": "Unknown system."}


def guess_by_name(name: str) -> dict | None:
    """Try Greenhouse, Lever and SmartRecruiters boards named like the company. Workday sites
    can't be guessed (their address has a site name), so those need a link."""
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower())
    slugs = list(dict.fromkeys(s for s in ["".join(words), "-".join(words), (words or [""])[0]] if len(s) >= 3))
    for slug in slugs:
        for system in ("greenhouse", "smartrecruiters", "lever"):
            result = verify({"system": system, "id": slug})
            if not result.get("ok") or not result.get("jobs"):
                continue
            # The board must really be this company (e.g. "tcs" is also Thornbury Community Services).
            board_name = result.get("name") or ""
            if system == "lever" and slug != norm(name):
                continue
            if board_name and difflib.SequenceMatcher(None, norm(board_name), norm(name)).ratio() < 0.6 \
                    and norm(name) not in norm(board_name):
                continue
            return {"system": system, "id": slug, "name": board_name or name, **result}
    return None


# ------------------------------------------------------------------ check & add

def check(config_path: Path, query: str) -> dict:
    """What would happen if this company were added? Doesn't change anything."""
    query = query.strip()
    companies = list_companies(config_path)
    link = parse_link(query)
    if link:
        existing = find_existing(companies, system=link["system"], ident=link["id"])
        if existing:
            return {"status": "exists", "company": existing}
        result = verify(link)
        if not result["ok"]:
            return {"status": "error", "message": result["error"]}
        name = result.get("name") or (workday_name([link["id"]]) if link["system"] == "workday" else link["id"])
        return {"status": "found", "source": {**link, "name": name}, "jobs": result.get("jobs"),
                "warning": result.get("error") if result.get("unverified") else None}
    if re.match(r"https?://", query):
        return {"status": "error", "message": unsupported_link(query) or
                "That link isn't a Workday, Greenhouse, Lever or SmartRecruiters job page. "
                "Open one job on the company's career site and paste that job's link."}
    existing = find_existing(companies, name=query)
    if existing:
        return {"status": "exists", "company": existing}
    guess = guess_by_name(query)
    if guess:
        return {"status": "found", "source": {k: guess[k] for k in ("system", "id", "name")}, "jobs": guess.get("jobs")}
    return {"status": "not_found", "similar": similar(companies, query),
            "message": "Couldn't find it automatically. Open the company's career site, click any job, "
                       "and paste that job's link here."}


def add(config_path: Path, source: dict) -> dict:
    """Add a company to config.yaml (after checking again that it isn't there yet)."""
    system, ident = source.get("system"), str(source.get("id", ""))
    if system not in SYSTEMS or not re.fullmatch(r"[A-Za-z0-9_\-]+", ident):
        return {"ok": False, "message": "Invalid company details."}
    companies = list_companies(config_path)
    existing = find_existing(companies, system=system, ident=ident)
    if existing:
        return {"ok": True, "message": f"{existing['name']} is already included.", "company": existing}

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    today = date.today().isoformat()
    if system == "workday":
        host, site = str(source.get("host", "")), str(source.get("site", ""))
        if not (re.fullmatch(r"[a-z0-9\-]+\.wd\d+", host) and re.fullmatch(r"[A-Za-z0-9_\-]+", site)):
            return {"ok": False, "message": "Invalid Workday details."}
        name = str(source.get("name") or ident).replace('"', "'")
        start = next(i for i, l in enumerate(lines) if re.match(r"\s+workday:", l))
        last = max(i for i, l in enumerate(lines) if i > start and re.match(r"\s+- \[", l)
                   and all(re.match(r"\s{4,}|\s*#|\s*$", x) for x in lines[start + 1:i]))
        entry = f"    - [{ident}, {host}, {site}, \"{name}\"]"
        lines.insert(last + 1, f"{entry:<70}# added in Job Hunt {today}")
    else:
        pattern = re.compile(rf"^(\s*{system}:\s*\[)([^\]]*)(\].*)$")
        i = next((i for i, l in enumerate(lines) if pattern.match(l)), None)
        if i is None:
            return {"ok": False, "message": f"Couldn't find the {SYSTEMS[system]} list in config.yaml."}
        head, items, tail = pattern.match(lines[i]).groups()
        items = f"{items.strip()}, {ident}" if items.strip() else ident
        lines[i] = f"{head}{items}{tail}"

    backup = config_path.with_suffix(".yaml.bak")
    shutil.copy(config_path, backup)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:                                    # make sure the file still reads and has the new entry
        if not find_existing(list_companies(config_path), system=system, ident=ident):
            raise ValueError("entry missing after write")
    except Exception as e:
        shutil.copy(backup, config_path)
        return {"ok": False, "message": f"Couldn't update config.yaml ({e}); nothing was changed."}
    added = find_existing(list_companies(config_path), system=system, ident=ident)
    return {"ok": True, "message": f"Added {added['name']}. It will be part of your next search.", "company": added}
