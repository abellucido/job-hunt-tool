"""Fetch one job posting from a link, for "Add a job from a link" on the Dashboard.

Career sites built on Workday, Greenhouse, Lever and SmartRecruiters are read through their
public job APIs (the same ones jobscout searches). Any other page is read for schema.org
JobPosting data, which most career sites and job boards include for search engines, and
failing that for its visible text.

    fetch(url, companies) -> {"title", "company", "location", "posted", "url", "description"}

Raises PostingError with a message for the user when the page has no readable posting
(a login wall, or a page that only fills in with JavaScript); the user can paste the text instead.
"""
import html
import json
import re

import requests
from bs4 import BeautifulSoup

API = {"User-Agent": "Mozilla/5.0 (jobscout personal job search)", "Accept": "application/json"}
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept-Language": "en,es;q=0.8"}
MIN_TEXT = 400          # shorter than this is not a posting (a login page, a cookie notice…)
SHORT_WORDS = 300       # a real posting this short is probably missing its requirements: warn


class PostingError(Exception):
    pass


def html_to_text(s: str) -> str:
    """HTML → plain text with paragraphs and bullets kept (same as jobscout's)."""
    if not s:
        return ""
    soup = BeautifulSoup(html.unescape(s), "html.parser")
    for tag in soup.find_all(["br", "p", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"]):
        tag.insert_before("\n")
        if tag.name == "li":
            tag.insert(0, "• ")
    text = re.sub(r"[ \t\xa0]+", " ", soup.get_text())
    return re.sub(r"\s*\n\s*(\n\s*)*", lambda m: "\n\n" if m.group(1) else "\n", text).strip()


def get_json(method: str, url: str, **kw):
    try:
        r = requests.request(method, url, headers=API, timeout=20, **kw)
    except requests.RequestException as e:
        raise PostingError(f"Couldn't reach the job site: {e}") from None
    if r.status_code == 404 or (r.status_code == 403 and "myworkdayjobs" in url):   # Workday: 403 = closed
        raise PostingError("The job site says this posting doesn't exist. It may have been closed or taken down.")
    if r.status_code == 429:
        raise PostingError("The job site is rate limiting right now. Try again in a few minutes.")
    if not r.ok:
        raise PostingError(f"The job site answered with an error (HTTP {r.status_code}).")
    try:
        return r.json()
    except ValueError:
        raise PostingError("The job site's answer couldn't be read.") from None


def company_name(companies: list[dict], system: str, ident: str, fallback: str) -> str:
    """The name used in config.yaml for this job board, if it's one jobscout already searches."""
    for c in companies:
        if c["system"] == system and c["id"].lower() == ident.lower():
            return c["name"]
    return fallback


# ------------------------------------------------------------------ job systems

def workday(m, url, companies):
    host, tenant, site, path = f"{m['tenant']}.{m['group']}", m["tenant"], m["site"], m["path"]
    d = get_json("GET", f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}")
    info = d.get("jobPostingInfo") or {}
    country = (info.get("jobRequisitionLocation") or {}).get("country", {}).get("descriptor", "")
    org = (d.get("hiringOrganization") or {}).get("name") or ""
    org = re.sub(r"^\d+[\s\-]+|\s+Legal Entity$", "", org).strip()        # "7300-Janssen-Cilag S.A. Legal Entity"
    fallback = org or (tenant.capitalize() if len(tenant) > 3 else tenant.upper())
    return {"title": info.get("title", ""), "company": company_name(companies, "workday", tenant, fallback),
            "location": "; ".join(dict.fromkeys(l for l in [info.get("location", ""), country,
                                                             *info.get("additionalLocations", [])] if l)),
            "posted": (info.get("startDate") or "")[:10], "url": info.get("externalUrl") or url,
            "description": html_to_text(info.get("jobDescription"))}


def greenhouse(m, url, companies):
    board, jid = m["board"], m["id"]
    p = get_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}")
    try:
        name = get_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}").get("name")
    except PostingError:
        name = None
    return {"title": p.get("title", ""), "company": name or company_name(companies, "greenhouse", board, board.capitalize()),
            "location": (p.get("location") or {}).get("name", ""), "posted": (p.get("updated_at") or "")[:10],
            "url": p.get("absolute_url") or url, "description": html_to_text(p.get("content"))}


def lever(m, url, companies):
    co, jid = m["company"], m["id"]
    p = get_json("GET", f"https://api.lever.co/v0/postings/{co}/{jid}")
    cat = p.get("categories", {})
    desc = p.get("descriptionPlain", "") + "\n" + "\n".join(
        l.get("text", "") + "\n" + html_to_text(l.get("content")) for l in p.get("lists", [])) + \
        "\n" + (p.get("additionalPlain") or "")
    return {"title": p.get("text", ""), "company": company_name(companies, "lever", co, co.capitalize()),
            "location": "; ".join(dict.fromkeys(l for l in [cat.get("location", ""), *cat.get("allLocations", [])] if l)),
            "posted": "", "url": p.get("hostedUrl") or url, "description": desc.strip()}


def smartrecruiters(m, url, companies):
    co, jid = m["company"], m["id"]
    p = get_json("GET", f"https://api.smartrecruiters.com/v1/companies/{co}/postings/{jid}")
    loc = p.get("location") or {}
    sections = (p.get("jobAd") or {}).get("sections", {})
    return {"title": p.get("name", ""),
            "company": company_name(companies, "smartrecruiters", co, (p.get("company") or {}).get("name") or co),
            "location": ", ".join(l for l in [loc.get("city", ""), loc.get("country", "").upper()] if l),
            "posted": (p.get("releasedDate") or "")[:10], "url": url,
            "description": "\n\n".join(f"{s.get('title', '')}\n{html_to_text(s.get('text'))}".strip()
                                       for s in sections.values() if isinstance(s, dict))}


SYSTEMS = [
    (re.compile(r"(?P<tenant>[a-z0-9\-]+)\.(?P<group>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                r"(?P<site>[A-Za-z0-9_\-]+)(?P<path>/job/[^?#]+)"), workday),
    (re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?P<board>[\w\-]+)/jobs/(?P<id>\d+)"), greenhouse),
    (re.compile(r"greenhouse\.io/embed/job_app\?(?=.*\bfor=(?P<board>[\w\-]+))(?=.*\btoken=(?P<id>\d+))"), greenhouse),
    (re.compile(r"jobs\.lever\.co/(?P<company>[\w\-]+)/(?P<id>[0-9a-f\-]{36})"), lever),
    (re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?P<company>[\w\-]+)/(?P<id>\d+)"), smartrecruiters),
]


# ------------------------------------------------------------------ any other page

def job_posting_ld(soup: BeautifulSoup) -> dict | None:
    """The schema.org JobPosting object in the page's JSON-LD, if any."""
    def find(node):
        if isinstance(node, list):
            for n in node:
                if (hit := find(n)) is not None:
                    return hit
        elif isinstance(node, dict):
            kind = node.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return node
            return find(node.get("@graph"))
        return None

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            hit = find(json.loads(tag.string or ""))
        except ValueError:
            continue
        if hit:
            return hit
    return None


def ld_location(job: dict) -> str:
    locs = job.get("jobLocation") or []
    out = []
    for loc in locs if isinstance(locs, list) else [locs]:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            country = country.get("name") if isinstance(country, dict) else country
            out.append(", ".join(str(v) for v in [addr.get("addressLocality"), country] if v))
    if job.get("jobLocationType") == "TELECOMMUTE":
        out.append("Remote")
    return "; ".join(dict.fromkeys(l for l in out if l))


def phenom(page: str, url: str) -> dict | None:
    """Careers sites on the Phenom platform (jobs.thermofisher.com and many other large employers)
    embed the job as JSON in `phApp.ddo`. Besides the employer's description it holds a job summary
    with the required skills, education and pay, which are often not in the description itself."""
    m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.", page, re.S)
    if not m:
        return None
    try:
        job = json.loads(m.group(1))["jobDetail"]["data"]["job"]
    except (ValueError, KeyError, TypeError):
        return None
    if not job.get("description"):
        return None
    text = html_to_text(job["description"])
    overview = job.get("ml_job_overview") if isinstance(job.get("ml_job_overview"), dict) else {}
    extra = []
    for key, heading in (("skills", "Skills"), ("education", "Education")):
        items = [i for i in overview.get(key) or [] if isinstance(i, str)]
        if items:
            extra.append(f"{heading}:\n" + "\n".join(f"• {i}" for i in items))
    if overview.get("summary"):
        extra.append(f"Summary:\n{overview['summary']}")
    if extra:
        text += "\n\nFrom the career site's job summary (not part of the employer's description above):\n\n" + "\n\n".join(extra)
    location = "; ".join(dict.fromkeys(
        ", ".join(v for v in (l.get("city"), l.get("country")) if v)
        for l in job.get("multi_location") or [] if isinstance(l, dict))) or \
        ", ".join(v for v in (job.get("city"), job.get("country")) if v)
    return {"title": job.get("title", ""), "company": job.get("companyName", ""), "location": location,
            "posted": str(job.get("postedDate") or job.get("dateCreated") or "")[:10], "url": url, "description": text}


def generic(url: str) -> dict:
    try:
        r = requests.get(url, headers=BROWSER, timeout=20)
    except requests.RequestException as e:
        raise PostingError(f"Couldn't open that link: {e}") from None
    if r.status_code in (401, 403, 999):
        raise PostingError("That site blocks automatic access (it may need a login). Paste the posting text instead.")
    if not r.ok:
        raise PostingError(f"That link answered with an error (HTTP {r.status_code}).")
    if job := phenom(r.text, url):
        return job
    soup = BeautifulSoup(r.text, "html.parser")

    job = job_posting_ld(soup)
    if job and len(html_to_text(job.get("description", ""))) >= MIN_TEXT:
        org = job.get("hiringOrganization")
        return {"title": html.unescape(job.get("title", "")),
                "company": org.get("name", "") if isinstance(org, dict) else str(org or ""),
                "location": ld_location(job), "posted": str(job.get("datePosted", ""))[:10], "url": url,
                "description": html_to_text(job.get("description"))}

    # No structured data: take the page's main text.
    og = soup.find("meta", property="og:title")
    title = (og.get("content") if og else None) or (soup.title.string if soup.title else "") or ""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = html_to_text(str(main))
    if len(text) < MIN_TEXT:
        raise PostingError("Couldn't find a job description on that page (it may need a login or JavaScript "
                           "to show it). Paste the posting text instead.")
    return {"title": title.strip(), "company": "", "location": "", "posted": "", "url": url, "description": text}


# ------------------------------------------------------------------ entry point

def fetch(url: str, companies: list[dict] = ()) -> dict:
    url = url.strip()
    if not re.match(r"https?://", url):
        raise PostingError("That doesn't look like a link. It should start with https://")
    for pattern, reader in SYSTEMS:
        if m := pattern.search(url):
            job = reader(m.groupdict(), url, companies)
            break
    else:
        job = generic(url)
    if len(job["description"]) < MIN_TEXT:
        raise PostingError("The posting was found but its description is nearly empty. Paste the posting text instead.")
    words = len(job["description"].split())
    if words < SHORT_WORDS:
        job["warning"] = (f"This posting is short ({words} words), so some requirements may be missing. Open "
                          "its posting text to check, and if the job page shows more, paste the full text instead.")
    return job
