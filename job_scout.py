#!/usr/bin/env python3
"""
job_scout.py - free job discovery. Polls public ATS job APIs (Greenhouse,
Lever, Ashby, SmartRecruiters, Workday, Oracle Recruiting Cloud) for a list
of target companies - no paid scraping API, no LLM tokens.

Standalone version: unlike the private-repo original, this build has no
dependency on an application tracker.

For every NEW role that matches the title/location filters it:
  1. fetches the full description and classifies sponsorship (Blocked /
     Unclear - never "Allowed", since absence of blocking language proves
     nothing either way);
  2. appends it to the triage queue and sends a phone alert (ntfy) unless
     it was auto-blocked on sponsorship.

Every run also diffs each company's current listing against its queued
entries: an id absent for CLOSE_AFTER_MISSES consecutive complete (not
paginated/capped) runs is marked closed; an id that reappears is reopened;
and a new id matching a closed entry's (title, location) fingerprint is
recorded as a repost of it (a same-role Workday req number reappearing
under a new id is common on repost, unlike Greenhouse/Lever, which usually
just reissue the same id) - only alerting if the prior posting had been
auto-blocked and the new one isn't.

Usage:
    python job_scout.py --init              create data/config.json
    python job_scout.py --check             per-company job/match counts, no state change
    python job_scout.py --dry-run           print what would be queued/sent, no state change
    python job_scout.py                     normal run (schedule this)
    python job_scout.py --queue [--all] [--json]   show the triage queue (default: state=new only)
    python job_scout.py --mark <id> <new|shortlisted|applied|dismissed|closed|open>

Runtime files live in ./data/: config.json (companies/filters - the ntfy
topic is NOT stored here, see notify() below), state.json, queue.json.
Override the data dir with JOB_SCOUT_HOME.
"""
import argparse
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR))

import atomic_json  # noqa: E402
from keyword_matching import contains_term  # noqa: E402

HOME = Path(os.environ.get("JOB_SCOUT_HOME", REPO_DIR / "data"))
UA = {"User-Agent": "job-scout/1.0", "Accept": "application/json"}
QUEUE_STATES = ("new", "shortlisted", "applied", "dismissed", "auto_blocked")

CLOSE_AFTER_MISSES = 2     # consecutive complete-listing runs an id must be absent before we call it closed
REPOST_WINDOW_DAYS = 180   # a same-fingerprint match older than this is a coincidence, not a repost
MASS_VANISH_GUARD = 0.5    # if more than this fraction of a company's open entries vanish in one run,
                           # treat it as a broken fetch (bad slug, API change) and skip closing anything

BLOCK_PATTERNS = [
    r"(?:will|would|does|do|can|could|is|are)\s+not\s+(?:\w+\s+){0,3}sponsor",
    r"\bcannot\s+(?:\w+\s+){0,3}sponsor",
    r"unable\s+to\s+(?:\w+\s+){0,3}sponsor",
    r"\bno\s+(?:work\s+)?(?:visa\s+)?sponsorship",
    r"visa\s+sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"sponsor[^.]{0,120}(?:now|currently)[^.]{0,20}(?:or|and)\s+in\s+the\s+future",
    r"(?:now|currently)[^.]{0,20}(?:or|and)\s+in\s+the\s+future[^.]{0,120}sponsor",
    r"not\s+eligible\s+for[^.]{0,60}(?:h-?1b|employer.sponsored)",
    r"\bITAR\b",
    r"\b(?:active|current)\s+(?:top\s+secret|secret|ts/sci)\b",
    r"must\s+be\s+a\s+u\.?s\.?\s+citizen",
    r"u\.?s\.?\s+citizens?\s+(?:only|required)",
    r"(?:u\.?s\.?\s+citizenship|permanent\s+residen(?:t|cy))\s+(?:is\s+)?required",
]
BLOCK_RES = [re.compile(p, re.I) for p in BLOCK_PATTERNS]


# ---------------------------------------------------------------- http / text

def http(url, data=None, headers=None, timeout=20, raw=False):
    h = dict(UA)
    body = None
    if data is not None and not raw:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    elif raw:
        body = data
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = r.read()
    return payload if raw else json.loads(payload)


def strip_html(text):
    text = html.unescape(text or "")
    text = html.unescape(text)  # Greenhouse double-escapes
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------- fetchers

def greenhouse(c):
    d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    jobs = [dict(id=str(j["id"]), title=j["title"], location=(j.get("location") or {}).get("name", ""),
                 url=j["absolute_url"], posted=(j.get("first_published") or "")[:10]) for j in d.get("jobs", [])]
    return jobs, True  # single uncapped call: always the full board


def lever(c):
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    d = http(f"https://{host}/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in d:
        desc = j.get("descriptionPlain") or strip_html(j.get("description", ""))
        for lst in j.get("lists") or []:
            desc += " " + (lst.get("text") or "") + " " + strip_html(lst.get("content", ""))
        desc += " " + (j.get("additionalPlain") or "")
        posted = ""
        ms = j.get("createdAt")
        if isinstance(ms, (int, float)):
            posted = date.fromtimestamp(ms / 1000).isoformat()
        out.append(dict(id=j["id"], title=j["text"], location=(j.get("categories") or {}).get("location", "") or "",
                        url=j["hostedUrl"], desc=desc, posted=posted))
    return out, True  # single uncapped call: always the full board


def ashby(c):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}")
    jobs = [dict(id=j["id"], title=j["title"], location=j.get("location", "") or "", url=j.get("jobUrl", ""),
                 desc=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                 posted=(j.get("publishedAt") or "")[:10]) for j in d.get("jobs", [])]
    return jobs, True  # single uncapped call: always the full board


def smartrecruiters(c):
    out, offset = [], 0
    complete = False
    while offset < 2000:
        d = http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings?limit=100&offset={offset}")
        rows = d.get("content", [])
        for j in rows:
            loc = j.get("location") or {}
            out.append(dict(id=j["id"], title=j["name"],
                            location=", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                            url=f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                            posted=(j.get("releasedDate") or "")[:10]))
        offset += 100
        if len(rows) < 100:
            complete = True  # ran out of rows before hitting the 2000 ceiling: this is everything
            break
    return out, complete


_RELATIVE_DAYS_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s*day)", re.I)


def _workday_posted(text):
    """Workday's postedOn is relative text ("Posted 5 Days Ago", "Posted Today", "Posted 30+ Days
    Ago") not a real date - approximate it as an ISO date so postings can be sorted/filtered by
    recency. "30+" is a floor, not exact, since Workday caps the display at that bucket."""
    m = _RELATIVE_DAYS_RE.search(text or "")
    if not m:
        return ""
    if m.group(1).lower() == "today":
        days = 0
    elif m.group(1).lower() == "yesterday":
        days = 1
    else:
        days = int(m.group(2))
    return (date.today() - timedelta(days=days)).isoformat()


def workday(c):
    """Unofficial endpoint. Needs host, tenant, site. 'search' is a term or a list of terms; each
    term is queried separately (max_per_term results, default 60) and results are merged by id."""
    base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    out, seen = [], set()
    page = 20  # Workday's unofficial endpoint 400s on limit > 20 - confirmed by testing, not documented
    complete = True  # AND across terms: one truncated term makes the whole listing untrustworthy for removal
    for term in terms:
        offset = 0
        term_complete = False
        while offset < cap:
            d = http(base, data={"appliedFacets": {}, "limit": page, "offset": offset, "searchText": term})
            rows = d.get("jobPostings", [])
            for j in rows:
                path = j.get("externalPath")
                if not path or path in seen:  # some rows are placeholder cards with no title/path
                    continue
                seen.add(path)
                out.append(dict(id=path, title=j["title"], location=j.get("locationsText", "") or "",
                                url=f"https://{c['host']}/{c['site']}{path}", posted=_workday_posted(j.get("postedOn"))))
            offset += page
            if len(rows) < page:
                term_complete = True  # ran out of rows before hitting the per-term cap
                break
        complete = complete and term_complete
    return out, complete


def oracle(c):
    """Oracle Recruiting Cloud (Fusion HCM) - unofficial/undocumented endpoint. Needs host, site
    (Oracle's siteNumber, e.g. "CX_1001"). Same per-term paged search shape as workday(), but
    Oracle's quirks (reverse-engineered against a real tenant, not in any doc):
      - the search term goes in finder as "keyword=", not a top-level searchText param;
      - limit/offset MUST live inside the finder string too - Oracle silently ignores them as
        top-level query params and always returns a fixed first page of 25;
      - expand=requisitionList is required or the response has only facet metadata, no jobs."""
    base = f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    page = 50
    out, seen = [], set()
    complete = True  # AND across terms: one truncated term makes the whole listing untrustworthy for removal
    for term in terms:
        offset = 0
        term_complete = False
        while offset < cap:
            finder = f"findReqs;siteNumber={c['site']},limit={page},offset={offset}"
            if term:
                finder += f",keyword={term}"
            url = base + "?" + urllib.parse.urlencode({"onlyData": "true", "expand": "requisitionList", "finder": finder})
            d = http(url)
            items = d.get("items") or [{}]
            rows = items[0].get("requisitionList") or []
            for j in rows:
                jid = str(j.get("Id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append(dict(id=jid, title=j.get("Title", "") or "", location=j.get("PrimaryLocation", "") or "",
                                url=f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}/job/{jid}",
                                posted=(j.get("PostedDate") or "")[:10]))
            offset += page
            if len(rows) < page:
                term_complete = True  # ran out of rows before hitting the per-term cap
                break
        complete = complete and term_complete
    return out, complete


FETCH = dict(greenhouse=greenhouse, lever=lever, ashby=ashby, smartrecruiters=smartrecruiters, workday=workday,
             oracle=oracle)
FETCH_WORKERS = 12  # fetches are I/O-bound (network wait); parallelizing across companies cuts wall-clock a lot


def fetch_company(c):
    """Runs on a worker thread. Retries once after a 5s sleep on any error - absorbs a scheduled
    run firing right as the runner's network isn't fully up yet."""
    try:
        jobs, complete = FETCH[c["ats"]](c)
        return c["name"], jobs, complete, None
    except Exception:
        time.sleep(5)
        try:
            jobs, complete = FETCH[c["ats"]](c)
            return c["name"], jobs, complete, None
        except Exception as e2:
            return c["name"], None, None, e2


def describe(c, job):
    """Full description text for one job. Lever/Ashby already carry it from the list call."""
    if job.get("desc"):
        return job["desc"]
    ats = c["ats"]
    if ats == "greenhouse":
        d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs/{job['id']}")
        return strip_html(d.get("content", ""))
    if ats == "smartrecruiters":
        d = http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings/{job['id']}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return strip_html(" ".join((s or {}).get("text", "") for s in secs.values()))
    if ats == "workday":
        d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}{job['id']}")
        return strip_html((d.get("jobPostingInfo") or {}).get("jobDescription", ""))
    if ats == "oracle":
        finder = f"ById;Id={job['id']}"
        url = (f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?"
               + urllib.parse.urlencode({"onlyData": "true", "finder": finder}))
        d = http(url)
        items = d.get("items") or []
        if not items:
            return ""
        it = items[0]
        parts = [it.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
        return strip_html(" ".join(p for p in parts if p))
    return ""


# ------------------------------------------------------------------ analysis

def matches(job, f):
    # Title include/exclude use word-boundary matching (keyword_matching.contains_term) so a short
    # term like "intern" doesn't false-positive inside "Internal"/"International". Locations stay
    # plain substring: filters like ", nj" rely on punctuation that word-boundary matching would
    # reject (the comma has no alnum neighbor to anchor against).
    t, loc = job["title"], job["location"].lower()
    inc = f.get("title_include", [])
    exc = f.get("title_exclude", [])
    locs = [x.lower() for x in f.get("locations_include", [])]
    if inc and not any(contains_term(t, x) for x in inc):
        return False
    if any(contains_term(t, x) for x in exc):
        return False
    if locs and not any(x in loc for x in locs):
        return False
    return True


def classify_sponsorship(text):
    """Blocked on explicit language, else Unclear. Never Allowed."""
    if not text:
        return "Unclear", "Description unavailable; not checked."
    for rx in BLOCK_RES:
        m = rx.search(text)
        if m:
            s, e = max(0, m.start() - 50), min(len(text), m.end() + 50)
            return "Blocked", "..." + text[s:e].strip() + "..."
    return "Unclear", "No sponsorship language in the description."


_EXPERIENCE_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*\+?\s*years?|"      # "3-5 years" / "3 to 5 years"
    r"(\d{1,2})\s*\+\s*years?|"                                     # "5+ years"
    r"(?:minimum|min\.?|at least)\s*(?:of\s*)?(\d{1,2})\s*years?",  # "minimum of 3 years"
    re.I,
)
_ENTRY_PHRASE_RE = re.compile(
    r"\bentry[- ]level\b|\bnew grad(?:uate)?s?\b|\brecent graduate\b|\bcampus hire\b|"
    r"\bno (?:prior )?experience (?:required|necessary)\b",
    re.I,
)
_SENIOR_TITLE_RE = re.compile(r"\b(senior|sr\.?|vice president|vp|svp|principal|lead|staff)\b", re.I)
_MID_TITLE_RE = re.compile(r"\bassociate\b", re.I)


def classify_experience(title, text):
    """Best-effort years-of-experience bucket. Explicit numbers in the description win over title
    words, since a title alone ("Analyst") says less than a description that actually states "3-5
    years required". No explicit signal at all is Unclear, not assumed entry-level - same "never
    guess beyond the evidence" rule as sponsorship classification."""
    hay = text or ""
    m = _ENTRY_PHRASE_RE.search(hay)
    if m:
        s, e = max(0, m.start() - 40), min(len(hay), m.end() + 40)
        return "Entry (0-1y)", hay[s:e].strip()
    m = _EXPERIENCE_YEARS_RE.search(hay)
    if m:
        min_years = min(int(g) for g in m.groups() if g)
        s, e = max(0, m.start() - 40), min(len(hay), m.end() + 40)
        evidence = hay[s:e].strip()
        if min_years <= 1:
            return "Entry (0-1y)", evidence
        if min_years <= 4:
            return "Mid (2-4y)", evidence
        return "Senior (5+y)", evidence
    if _SENIOR_TITLE_RE.search(title or ""):
        return "Senior (5+y)", f"inferred from title: {title}"
    if _MID_TITLE_RE.search(title or ""):
        return "Mid (2-4y)", f"inferred from title: {title}"
    return "Unclear", "No years-of-experience language found."


_SALARY_RANGE_RE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?\s*(?:-|–|to)\s*\$?\s?(\d{2,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?",
    re.I,
)
_SALARY_SINGLE_RE = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})*(?:\.\d+)?)\s*(k)?\b", re.I)
_SALARY_CONTEXT_RE = re.compile(r"\b(?:salary|compensation|base pay|total comp|pay range)\b", re.I)
# finance JDs are full of unrelated dollar figures ("$2B in assets", "$500M portfolio") - a nearby
# context word alone isn't always enough, so also reject anything sitting next to an asset-scale word
_SALARY_DISQUALIFY_RE = re.compile(
    r"\b(million|billion|trillion|aum|assets under management|in assets|portfolio|book of business)\b", re.I)


def _salary_num(digits, k_suffix):
    n = float(digits.replace(",", ""))
    return n * 1000 if k_suffix else n


def classify_salary(text):
    """Best-effort salary range from explicit description text, gated on BOTH a nearby salary/
    compensation context word and a plausible annual-salary magnitude ($20k-$600k) - a raw dollar
    regex alone would happily "find" a salary in "manages a $500M portfolio". Unclear when no
    confident match, never guessed from title/company alone."""
    hay = text or ""
    for m in _SALARY_RANGE_RE.finditer(hay):
        context = hay[max(0, m.start() - 80):m.end() + 20]
        nearby = hay[max(0, m.start() - 20):m.end() + 20]
        if not _SALARY_CONTEXT_RE.search(context) or _SALARY_DISQUALIFY_RE.search(nearby):
            continue
        lo, hi = _salary_num(m.group(1), m.group(2)), _salary_num(m.group(3), m.group(4))
        lo, hi = min(lo, hi), max(lo, hi)
        if not (20000 <= lo <= 600000 and 20000 <= hi <= 600000):
            continue
        evidence = hay[max(0, m.start() - 40):min(len(hay), m.end() + 40)].strip()
        return f"${int(lo):,} - ${int(hi):,}", evidence
    for m in _SALARY_SINGLE_RE.finditer(hay):
        context = hay[max(0, m.start() - 40):m.end() + 10]
        nearby = hay[max(0, m.start() - 20):m.end() + 20]
        if not _SALARY_CONTEXT_RE.search(context) or _SALARY_DISQUALIFY_RE.search(nearby):
            continue
        val = _salary_num(m.group(1), m.group(2))
        if not (20000 <= val <= 600000):
            continue
        evidence = hay[max(0, m.start() - 40):min(len(hay), m.end() + 40)].strip()
        return f"${int(val):,}", evidence
    return "Unclear", ""


def keyword_hits(title, text, keywords):
    hay = f"{title} {text}".lower()
    return [k for k in keywords if re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", hay)]


_REQ_NUMBER_RE = re.compile(r"\b[a-z]*-?\d{4,}(?:-\d+)*\b")  # also swallow a trailing -2/-3 repost suffix


def role_fingerprint(title, location):
    """Identity for 'is this the same role reposted under a new id' - not a job id, since a repost
    on Greenhouse/Lever usually mints a new one while Workday keeps the same req number, so id alone
    can't tell a repost from a genuinely different opening. Strips requisition-number-shaped tokens
    (e.g. "R249058-2") since those change on repost even when the role itself didn't."""
    def norm(s):
        s = _REQ_NUMBER_RE.sub("", (s or "").lower())
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    return f"{norm(title)}|{norm(location)}"


# --------------------------------------------------------------------- notify

def notify(cfg, entry, dry):
    line = (f"{entry['company']}: {entry['role']} ({entry['location'] or 'n/a'}) | "
            f"sponsorship {entry['sponsorship_status']}"
            + (f" | {', '.join(entry['kw_hits'][:5])}" if entry["kw_hits"] else ""))
    kind = cfg.get("type", "stdout")
    if dry or kind == "stdout":
        print(("[dry-run] " if dry else "[alert] ") + line + "\n    " + entry["link"])
        return
    topic = cfg.get("topic") or os.environ.get("NTFY_TOPIC")
    if kind == "ntfy" and not topic:
        print(f"! notify skipped: no ntfy topic (set NTFY_TOPIC env var or cfg.notify.topic): {line}", file=sys.stderr)
        return
    try:
        if kind == "ntfy":
            server = cfg.get("server", "https://ntfy.sh").rstrip("/")
            hdr = {"Title": f"New job: {entry['company']}".encode("ascii", "ignore").decode(), "Click": entry["link"]}
            http(f"{server}/{topic}", data=line.encode(), headers=hdr, raw=True)
        elif kind == "telegram":
            http(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                 data={"chat_id": cfg["chat_id"], "text": f"{line}\n{entry['link']}"})
    except Exception as e:  # an alert failure must not lose the queue entry
        print(f"! notify failed: {e}", file=sys.stderr)


# ------------------------------------------------------------------- commands

def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def cmd_init():
    HOME.mkdir(parents=True, exist_ok=True)
    cfg_path = HOME / "config.json"
    if cfg_path.exists():
        print(f"{cfg_path} already exists; not overwriting.")
        return
    example = REPO_DIR / "config.example.json"
    cfg = json.loads(example.read_text(encoding="utf-8"))
    atomic_json.write(str(cfg_path), cfg)
    print(f"Wrote {cfg_path}\nSet the NTFY_TOPIC env var (or GitHub Actions secret) to your ntfy.sh topic - "
          f"it is deliberately not stored in config.json since this repo is public.")


def cmd_queue(args):
    q = load_json(HOME / "queue.json", [])
    if not args.all:
        q = [e for e in q if e["state"] == "new" and not e.get("closed_on")]
    if args.days is not None:
        cutoff = (date.today() - timedelta(days=args.days)).isoformat()
        # postings with no parseable date are kept, not dropped - "unknown" isn't the same as "old"
        q = [e for e in q if not e.get("posted") or e["posted"] >= cutoff]
    def sort_key(e):
        posted = e.get("posted") or ""
        try:
            ordinal = date.fromisoformat(posted).toordinal()
        except ValueError:
            # no posted date: rank by when we first saw it instead of letting every undated
            # posting tie and fall through entirely to the keyword-count tiebreak
            try:
                ordinal = date.fromisoformat(e.get("first_seen") or "").toordinal()
            except ValueError:
                ordinal = 0
        return (-ordinal, -len(e["kw_hits"]), e["company"])

    q = sorted(q, key=sort_key)
    if args.json:
        print(json.dumps(q, indent=1))
        return
    if args.html:
        updated_at = datetime.fromtimestamp((HOME / "queue.json").stat().st_mtime) if (HOME / "queue.json").exists() else None
        write_queue_html(q, args.html, updated_at)
        print(f"wrote {len(q)} entries to {args.html}")
        return
    for e in q:
        print(f"{e['id']}  {e['company']}: {e['role']} ({e['location']}) posted {posted_label(e)} "
              f"[{e['state']}, spons {e['sponsorship_status']}, exp {e.get('experience', 'Unclear')}, "
              f"salary {e.get('salary', 'Unclear')}, kw {', '.join(e['kw_hits']) or 'none'}]\n    {e['link']}")
    print(f"{len(q)} queue entr{'y' if len(q) == 1 else 'ies'}.")


def posted_label(e):
    posted = e.get("posted") or ""
    if posted == date.today().isoformat():
        return "today"
    if posted:
        return posted
    first_seen = e.get("first_seen") or ""
    return f"seen {first_seen[5:]}" if first_seen else "unknown"  # MM-DD, no real posted date to trust


# states shown collapsed below the main "new" table, in this order; any other state found (e.g.
# a custom state from a hand-edit) is appended after these
_SECONDARY_STATE_ORDER = ["shortlisted", "applied", "auto_blocked", "dismissed"]


def write_queue_html(q, path, updated_at=None):
    """Static page, no server: q is already sorted newest-posted-first by cmd_queue. Each row is a
    plain <a> to the live posting so double-clicking the file and clicking a link is the whole workflow.
    "new" postings get their own table up top; every other open state (shortlisted, applied, ...)
    collapses into a <details> section; closed/removed entries (any state) go in one final section so
    a growing history doesn't bury what's actionable today."""
    today = date.today().isoformat()

    def row_html(e):
        posted = e.get("posted") or ""
        closed = bool(e.get("closed_on"))
        badge = " today" if posted == today else ""
        kw = ", ".join(e["kw_hits"]) if e["kw_hits"] else "—"
        spons_evidence = html.escape(e.get("sponsorship_evidence") or "no blocking language found")
        exp = e.get("experience") or "Unclear"
        exp_evidence = html.escape(e.get("experience_evidence") or "")
        salary = e.get("salary") or "Unclear"
        salary_evidence = html.escape(e.get("salary_evidence") or "")
        role = html.escape(e["role"])
        if e.get("repost_count"):
            role += f' <span class="repost" title="repost of {html.escape(e.get("repost_of") or "?")}">repost x{e["repost_count"]}</span>'
        role_cell = (f'<a href="{html.escape(e["link"])}" target="_blank" rel="noopener">{role}</a>'
                     if not closed else role)
        row_title = ""
        if closed:
            row_title = (f' title="closed {html.escape(e["closed_on"])} ({html.escape(e.get("closed_reason") or "")}); '
                         f'last live {html.escape(e.get("last_seen_live") or "unknown")}"')
        state_cell = f'<td>{html.escape(e["state"])}</td>' if closed else ""
        return (
            f'<tr class="{e["state"]}{badge}{" closed" if closed else ""}"{row_title}>'
            f'<td class="posted">{html.escape(posted_label(e))}</td>'
            f'<td>{html.escape(e["company"])}</td>'
            f'<td>{role_cell}</td>'
            f'<td>{html.escape(e["location"])}</td>'
            f'<td title="{spons_evidence}">{html.escape(e["sponsorship_status"])}</td>'
            f'<td title="{exp_evidence}">{html.escape(exp)}</td>'
            f'<td title="{salary_evidence}" class="salary">{html.escape(salary)}</td>'
            f'<td class="kw">{html.escape(kw)}</td>'
            f'{state_cell}'
            f'</tr>'
        )

    head = ("<tr><th>Posted</th><th>Company</th><th>Role</th><th>Location</th><th>Sponsorship</th>"
            "<th>Experience</th><th>Salary</th><th>Keywords</th></tr>")
    closed_head = head.replace("</tr>", "<th>State</th></tr>")

    open_q = [e for e in q if not e.get("closed_on")]
    closed_q = [e for e in q if e.get("closed_on")]
    new_rows = [e for e in open_q if e["state"] == "new"]
    by_state = {}
    for e in open_q:
        if e["state"] != "new":
            by_state.setdefault(e["state"], []).append(e)
    ordered_states = [s for s in _SECONDARY_STATE_ORDER if s in by_state] + \
                      [s for s in by_state if s not in _SECONDARY_STATE_ORDER]

    sections = "".join(
        f'<details><summary>{html.escape(state)} ({len(by_state[state])})</summary>'
        f'<table><thead>{head}</thead><tbody>{"".join(row_html(e) for e in by_state[state])}</tbody></table>'
        f'</details>'
        for state in ordered_states
    )
    if closed_q:
        sections += (
            f'<details><summary>closed / removed ({len(closed_q)})</summary>'
            f'<table><thead>{closed_head}</thead><tbody>{"".join(row_html(e) for e in closed_q)}</tbody></table>'
            f'</details>'
        )

    updated_label = updated_at.strftime("%Y-%m-%d %H:%M") if updated_at else "unknown"

    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Job Scout Queue</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafafa; color: #111; }}
h1 {{ font-size: 1.2rem; }}
.meta {{ color: #666; font-size: 0.85rem; margin-top: -0.5rem; margin-bottom: 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #ddd; text-align: left; font-size: 0.9rem; }}
th {{ position: sticky; top: 0; background: #fafafa; }}
tr.today {{ background: #eaffea; font-weight: 600; }}
tr.closed {{ color: #999; }}
tr.closed a, tr.closed {{ text-decoration: line-through; }}
td.posted {{ white-space: nowrap; }}
td.kw {{ color: #555; font-size: 0.85rem; }}
td.salary {{ white-space: nowrap; }}
span.repost {{ color: #b45309; font-weight: 600; font-size: 0.8rem; text-decoration: none; }}
details {{ margin-bottom: 0.5rem; }}
summary {{ cursor: pointer; font-weight: 600; padding: 4px 0; }}
#filterBar {{ position: sticky; top: 0; background: #fafafa; padding: 0.5rem 0; margin-bottom: 0.5rem; z-index: 1; }}
#filterBox {{ width: 100%; max-width: 480px; padding: 8px 10px; font-size: 1rem; box-sizing: border-box; }}
#filterCount {{ color: #666; font-size: 0.85rem; margin-left: 8px; }}
tr.hidden-by-filter {{ display: none; }}
.chips {{ margin-top: 6px; }}
.chip {{ display: inline-block; padding: 3px 10px; margin: 2px 4px 2px 0; border: 1px solid #ccc;
         border-radius: 12px; background: #fff; font-size: 0.8rem; cursor: pointer; color: #333; }}
.chip:hover {{ background: #eee; }}
.chip.active {{ background: #333; color: #fff; border-color: #333; }}
</style></head>
<body>
<h1>Job Scout Queue - {len(open_q)} open, {len(closed_q)} closed</h1>
<p class="meta">Data last updated {updated_label}</p>
<div id="filterBar">
<input type="search" id="filterBox" placeholder="Filter: comma = OR, space = AND (e.g. python, sql bloomberg)" autocomplete="off">
<span id="filterCount"></span>
<div class="chips">
<button type="button" class="chip" data-term="entry">Fresher (Entry)</button>
<button type="button" class="chip" data-term="mid (2-4y)">Mid (2-4y)</button>
<button type="button" class="chip" data-term="senior">Senior (5+y)</button>
</div>
</div>
<h2>New ({len(new_rows)})</h2>
<table id="q">
<thead>{head}</thead>
<tbody>
{''.join(row_html(e) for e in new_rows)}
</tbody>
</table>
{sections}
<script>
(function() {{
  // Live client-side filter, no server: comma-separated groups are OR'd, terms within a group
  // are AND'd (e.g. "python, sql bloomberg" = python OR (sql AND bloomberg)). Lets one shared
  // page/URL serve different people with different interests - each viewer just types their own
  // terms; nothing is sent anywhere and nothing is saved except this browser's own last search.
  var box = document.getElementById('filterBox');
  var countEl = document.getElementById('filterCount');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var rows = Array.prototype.slice.call(document.querySelectorAll('table tbody tr'));
  var forcedOpen = [];

  function apply() {{
    var raw = box.value.trim().toLowerCase();
    forcedOpen.forEach(function(d) {{ d.removeAttribute('open'); }});
    forcedOpen = [];
    if (!raw) {{
      rows.forEach(function(tr) {{ tr.classList.remove('hidden-by-filter'); }});
      countEl.textContent = '';
      chips.forEach(function(c) {{ c.classList.remove('active'); }});
      try {{ localStorage.setItem('jobScoutFilter', ''); }} catch (e) {{}}
      return;
    }}
    var groups = raw.split(',').map(function(g) {{ return g.trim().split(/\\s+/).filter(Boolean); }})
                     .filter(function(g) {{ return g.length; }});
    var shown = 0;
    rows.forEach(function(tr) {{
      var text = tr.textContent.toLowerCase();
      var match = groups.some(function(terms) {{ return terms.every(function(t) {{ return text.indexOf(t) !== -1; }}); }});
      tr.classList.toggle('hidden-by-filter', !match);
      if (match) {{
        shown++;
        var details = tr.closest('details');
        if (details && !details.open) {{ details.open = true; forcedOpen.push(details); }}
      }}
    }});
    countEl.textContent = 'showing ' + shown + ' of ' + rows.length;
    try {{ localStorage.setItem('jobScoutFilter', box.value); }} catch (e) {{}}
    var current = raw;
    chips.forEach(function(c) {{ c.classList.toggle('active', current === c.dataset.term); }});
  }}

  chips.forEach(function(c) {{
    c.addEventListener('click', function() {{
      var term = c.dataset.term;
      box.value = (box.value.trim().toLowerCase() === term) ? '' : term;
      apply();
    }});
  }});

  try {{
    var saved = localStorage.getItem('jobScoutFilter');
    if (saved) box.value = saved;
  }} catch (e) {{}}
  box.addEventListener('input', apply);
  apply();
}})();
</script>
</body></html>"""
    Path(path).write_text(page, encoding="utf-8")


def cmd_mark(args):
    q = load_json(HOME / "queue.json", [])
    hit = [e for e in q if e["id"] == args.id]
    if not hit:
        sys.exit(f"no queue entry {args.id}")
    e = hit[0]
    if args.state == "closed":  # manual: for a dead link the scout's own liveness check missed
        e["closed_on"], e["closed_reason"] = date.today().isoformat(), "manual"
    elif args.state == "open":
        e["closed_on"], e["closed_reason"], e["miss_count"] = None, None, 0
    elif args.state in QUEUE_STATES:
        e["state"] = args.state
    else:
        sys.exit(f"state must be one of {QUEUE_STATES + ('closed', 'open')}")
    atomic_json.write(str(HOME / "queue.json"), q)
    print(f"{args.id} -> {args.state}")


def run(args):
    cfg = load_json(HOME / "config.json", None)
    if cfg is None:
        sys.exit(f"No config at {HOME / 'config.json'}. Run: python job_scout.py --init")
    filters = cfg.get("filters", {})
    keywords = cfg.get("keywords", [])
    state = load_json(HOME / "state.json", {})
    queue = load_json(HOME / "queue.json", [])
    queued_ids = {e["id"] for e in queue}
    run_id = "scout_" + date.today().isoformat()
    counts = dict(companies=0, errors=0, matched=0, blocked=0, queued=0, alerted=0,
                  closed=0, reopened=0, reposts=0)

    fetched = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(fetch_company, c): c for c in cfg["companies"]}
        for fut in as_completed(futures):
            name, jobs, complete, err = fut.result()
            fetched[name] = (jobs, complete, err)

    # One shared pool for every description fetch across every company, not one pool per company -
    # several companies finishing their listing fetch around the same time and each spinning up
    # their own pool caused a brief concurrency spike that tripped Workday's rate limiting (429s).
    describe_pool = ThreadPoolExecutor(max_workers=8)
    try:
        for c in cfg["companies"]:
            name = c["name"]
            jobs, complete, err = fetched[name]
            if err is not None:
                counts["errors"] += 1
                print(f"! {name} ({c['ats']}): {err}", file=sys.stderr)
                continue
            counts["companies"] += 1
            hits = [j for j in jobs if matches(j, filters)]
            if args.check:
                warn = "  <- 0 jobs, check slug" if not jobs else ""
                trunc = "  [truncated - raise max_per_term or narrow search terms]" if not complete else ""
                print(f"ok {name}: {len(jobs)} jobs, {len(hits)} match{warn}{trunc}")
                continue

            seen = set(state.get(name, []))
            first_run = name not in state
            fresh = [j for j in (hits if first_run else [j for j in hits if j["id"] not in seen])
                     if f"{name}:{j['id']}" not in queued_ids]
            descriptions = {}
            if fresh:
                futures = {describe_pool.submit(describe, c, j): j for j in fresh}
                for fut in as_completed(futures):
                    j = futures[fut]
                    try:
                        descriptions[j["id"]] = fut.result()
                    except Exception as e:
                        print(f"! {name} description for {j['title']}: {e}", file=sys.stderr)
                        descriptions[j["id"]] = ""
            _process_company(c, name, fresh, descriptions, first_run, jobs, complete, seen, state, queue, queued_ids,
                              keywords, cfg, args, counts)
            if not args.dry_run:
                _update_liveness(name, jobs, complete, queue, counts)
    finally:
        describe_pool.shutdown()

    if args.check:
        return
    if not args.dry_run:
        HOME.mkdir(parents=True, exist_ok=True)
        atomic_json.write(str(HOME / "state.json"), state)
        atomic_json.write(str(HOME / "queue.json"), queue)
    print(f"done: {counts['companies']} companies ({counts['errors']} errors), {counts['matched']} new matches, "
          f"{counts['blocked']} auto-blocked on sponsorship, {counts['queued']} queued, {counts['alerted']} alerts, "
          f"{counts['closed']} closed, {counts['reopened']} reopened, {counts['reposts']} reposts.")


def _find_repost_source(queue, name, fp, live_ids, eid):
    """A prior queue entry is this job's repost source if: same company, same role fingerprint,
    its own id is no longer in the live listing (so it's not just a second concurrent opening for
    the same role), and it's recent enough that the match isn't just coincidence."""
    cutoff = (date.today() - timedelta(days=REPOST_WINDOW_DAYS)).isoformat()
    candidates = [e for e in queue if e["company"] == name and e["id"] != eid
                  and role_fingerprint(e["role"], e["location"]) == fp
                  and e["id"].split(":", 1)[1] not in live_ids
                  and e.get("first_seen", "") >= cutoff]
    return max(candidates, key=lambda e: e.get("first_seen", "")) if candidates else None


def _process_company(c, name, fresh, descriptions, first_run, jobs, complete, seen, state, queue, queued_ids,
                      keywords, cfg, args, counts):
    live_ids = {jj["id"] for jj in jobs}
    today = date.today().isoformat()
    for j in fresh:
        counts["matched"] += 1
        eid = f"{name}:{j['id']}"
        text = descriptions.get(j["id"], "")
        spons, evidence = classify_sponsorship(text)
        experience, experience_evidence = classify_experience(j["title"], text)
        salary, salary_evidence = classify_salary(text)
        fp = role_fingerprint(j["title"], j["location"])
        # a capped/paginated listing can't prove a prior id is gone - it may just be outside the
        # window - so only trust absence as repost evidence when this run's listing was complete
        prior = None if (first_run or not complete) else _find_repost_source(queue, name, fp, live_ids, eid)

        entry_state = "auto_blocked" if spons == "Blocked" else "new"
        repost_count, repost_of, reposted_on = 0, None, None
        if prior:
            repost_count = prior.get("repost_count", 0) + 1
            repost_of, reposted_on = prior["id"], today
            if spons != "Blocked" and prior.get("state") in ("dismissed", "applied", "shortlisted"):
                entry_state = prior["state"]  # a repost of a role Yazad already acted on inherits that decision
            if not prior.get("closed_on"):
                prior["closed_on"], prior["closed_reason"] = today, "reposted"

        entry = dict(id=eid, company=name, role=j["title"], location=j["location"], link=j["url"],
                     posted=j.get("posted") or "", source="job-scout", first_seen=today,
                     sponsorship_status=spons, sponsorship_evidence=evidence,
                     experience=experience, experience_evidence=experience_evidence,
                     salary=salary, salary_evidence=salary_evidence,
                     kw_hits=keyword_hits(j["title"], text, keywords),
                     state=entry_state, first_run=first_run,
                     last_seen_live=today, miss_count=0, closed_on=None, closed_reason=None,
                     repost_count=repost_count, repost_of=repost_of, reposted_on=reposted_on)
        queue.append(entry)
        queued_ids.add(eid)
        if prior:
            counts["reposts"] += 1
        if spons == "Blocked":
            counts["blocked"] += 1
            continue
        counts["queued"] += 1
        # first run seeds the queue silently; a repost only alerts if it flips from blocked to open -
        # otherwise it's the same decision Yazad already made, just resurfacing under a new id
        if not first_run and (not prior or prior.get("state") == "auto_blocked"):
            notify(cfg.get("notify", {}), entry, args.dry_run)
            counts["alerted"] += 1
    if not args.dry_run:
        state[name] = sorted(seen | {j["id"] for j in jobs})  # union: a capped search must not re-alert on drift
    elif first_run:
        print(f"[dry-run] {name}: first run, would queue {len(fresh)} current matches silently")


def _update_liveness(name, jobs, complete, queue, counts):
    """Detects closed/reopened postings for one company's queue entries by diffing against the FULL
    current listing (`jobs`, not the title/location-filtered `hits`) - so editing filters never closes
    an entry. Closing requires CLOSE_AFTER_MISSES consecutive complete (unpaginated-cap) runs without
    the id showing up, since a single miss on a paginated/capped board just means it fell outside the
    search window, not that the posting is gone."""
    if not jobs:
        print(f"! {name}: empty listing, skipping close/reopen check (broken slug or API?)", file=sys.stderr)
        return
    live_ids = {j["id"] for j in jobs}
    today = date.today().isoformat()
    mine = [e for e in queue if e["company"] == name]
    open_mine = [e for e in mine if not e.get("closed_on")]
    missing = [e for e in open_mine if e["id"].split(":", 1)[1] not in live_ids]

    for e in mine:
        if e["id"].split(":", 1)[1] not in live_ids:
            continue
        e["last_seen_live"] = today
        e["miss_count"] = 0
        if e.get("closed_on") and e.get("closed_reason") != "manual":  # same id came back: a real reopen
            e["closed_on"], e["closed_reason"] = None, None
            e["repost_count"] = e.get("repost_count", 0) + 1
            e["reposted_on"] = today
            counts["reopened"] += 1

    # A mass vanish (likely a broken fetch, bad slug, API change) needs stronger evidence before
    # closing anything - but must not block closure forever: raising the bar rather than returning
    # early still lets miss_count climb, so a genuine mass closure eventually goes through instead of
    # permanently freezing every entry the moment the ratio is first crossed.
    mass_vanish = len(open_mine) >= 3 and len(missing) / len(open_mine) > MASS_VANISH_GUARD
    if mass_vanish:
        print(f"! {name}: {len(missing)}/{len(open_mine)} open queue entries vanished at once, "
              f"requiring stronger evidence before closing (possible broken fetch)", file=sys.stderr)
    if not complete:
        return  # a truncated/capped listing can't prove absence - a job outside the window isn't closed
    close_after = CLOSE_AFTER_MISSES * 3 if mass_vanish else CLOSE_AFTER_MISSES
    for e in missing:
        e["miss_count"] = e.get("miss_count", 0) + 1
        if e["miss_count"] >= close_after:
            e["closed_on"], e["closed_reason"] = today, "removed"
            counts["closed"] += 1


def main():
    ap = argparse.ArgumentParser(description="Free ATS job discovery, standalone (no tracker dedup).")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--all", action="store_true", help="with --queue: include every state")
    ap.add_argument("--json", action="store_true", help="with --queue: JSON output")
    ap.add_argument("--days", type=int, help="with --queue: only postings posted within the last N days")
    ap.add_argument("--html", metavar="PATH", help="with --queue: write a clickable HTML page instead of stdout")
    ap.add_argument("--mark", nargs=2, metavar=("ID", "STATE"))
    args = ap.parse_args()
    if args.init:
        return cmd_init()
    if args.queue:
        return cmd_queue(args)
    if args.mark:
        args.id, args.state = args.mark
        return cmd_mark(args)
    run(args)


if __name__ == "__main__":
    main()
