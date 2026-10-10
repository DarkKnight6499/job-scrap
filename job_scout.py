#!/usr/bin/env python3
"""
job_scout.py - free job discovery. Polls public ATS job APIs (Greenhouse,
Lever, Ashby, SmartRecruiters, Workday, Oracle Recruiting Cloud, Personio)
for a list of target companies - no paid scraping API, no LLM tokens.

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
import random
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_DIR))

import atomic_json  # noqa: E402
from keyword_matching import contains_term  # noqa: E402

HOME = Path(os.environ.get("JOB_SCOUT_HOME", REPO_DIR / "data"))
UA = {"User-Agent": "job-scout/1.0", "Accept": "application/json"}
RETRY_429_MAX = 4  # Workday 429s on bursts; a short backoff clears them (40 of 40 recovered in testing)
QUEUE_STATES = ("new", "shortlisted", "applied", "dismissed", "opt_only", "auto_blocked")

CLOSE_AFTER_MISSES = 2     # consecutive complete-listing runs an id must be absent before we call it closed
REPOST_WINDOW_DAYS = 270   # a same-fingerprint match older than this (measured from when the prior
                           # posting actually went quiet, not from when it was first seen - see
                           # _find_repost_source) is a coincidence, not a repost
MASS_VANISH_GUARD = 0.5    # if more than this fraction of a company's open entries vanish in one run,
                           # treat it as a broken fetch (bad slug, API change) and skip closing anything

GHOST_MIN_COUNT = 3        # a role posted at least this many times under the same fingerprint...
GHOST_MIN_SPAN_DAYS = 120  # ...spanning at least this many days looks like an evergreen/ghost req,
                           # not a normal one-off backfill after someone leaves

FLOOD_MIN = 10             # a company that has run before and suddenly shows more than max(FLOOD_MIN, FLOOD_FRACTION of its
FLOOD_FRACTION = 0.5       # matches) as new is almost surely an id-scheme or search-term change, not real postings: queue
                           # them silently and send one summary instead of one alert per job
ALERT_MAX_ATTEMPTS = 3     # failed alerts are retried on later runs until this many total attempts
ALERT_CAP_PER_RUN = 30     # per-run ceiling on individual alerts; the rest queue silently and roll into one summary
ENRICH_RETRY_BUDGET = 40    # per run: failed description fetches and undated Workday entries retried at most this many times
ENRICH_MAX_ATTEMPTS = 5    # then the entry is left as it is (one try per day)
RECALL_MAX = 150           # --recall queues at most this many never-queued matching roles per run
PRUNE_AFTER_DAYS = 60      # queue.json only ever accumulates (matches() re-runs at intake, never
                           # retroactively against what's already queued), so without a cutoff it
                           # grows forever and the generated docs/index.html gets slow to render.
                           # Measured against the live queue on 2026-09-27: 60 days drops ~26% of
                           # entries and lines up with how job hunting actually works - most listings
                           # either fill or go quiet within 4-8 weeks of the employer's own posted
                           # date, so anything older is unlikely to still be an actively-screening
                           # req. 30 days was too aggressive (cuts ~47%, including postings still in
                           # a normal review cycle); 90+ days barely trims the queue at all.

# Workday's detail-endpoint startDate is not the fixed value it looks like - confirmed live: the
# same unedited posting returned startDate one calendar day apart in two checks made hours apart,
# so it drifts across Workday's own day boundary the same way postedOn's relative text does, just
# with finer granularity. An entry captured right at the boundary can freeze the wrong date forever
# unless it's re-checked once the drift window has passed. REVERIFY_MIN_AGE_DAYS gives Workday's
# clock time to settle before re-checking; REVERIFY_MAX_AGE_DAYS bounds how long a not-yet-reverified
# entry stays eligible, so this never re-fetches the whole queue on every run.
REVERIFY_MIN_AGE_DAYS = 1
REVERIFY_MAX_AGE_DAYS = 3

BLOCK_PATTERNS = [
    # Same-sentence character window (bounded by the next period, like the other patterns
    # below), not a strict word count - a strict {0,3}-word cap missed real phrasing like
    # "do not offer any type of employment-based immigration sponsorship" or "will not provide
    # any assistance ... in support of any other form of immigration sponsorship", both of which
    # put well over 3 words between the negation and "sponsor".
    r"(?:will|would|does|do|can|could|is|are)\s+not\s+[^.]{0,150}?sponsor",
    r"\bcannot\s+(?:\w+\s+){0,3}sponsor",
    r"unable\s+to\s+(?:\w+\s+){0,3}sponsor",
    r"\bno\s+(?:work\s+)?(?:visa\s+)?sponsorship",
    r"visa\s+sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"not\s+(?:available|offered|provided)\s+for[^.]{0,60}sponsorship",
    r"\bsponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided)",
    r"sponsor[^.]{0,120}(?:now|currently|current)[^.]{0,20}(?:or|and)\s+in\s+the\s+future",
    r"(?:now|currently|current)[^.]{0,20}(?:or|and)\s+in\s+the\s+future[^.]{0,120}sponsor",
    r"not\s+eligible\s+for[^.]{0,80}(?:h-?1b|employer.sponsored|sponsorship|work\s+visa)",
    r"without\s+the\s+need\s+for[^.]{0,150}(?:sponsor|employer.sponsored|employer\s+support)",
    r"\bITAR\b",
    r"\b(?:active|current)\s+(?:top\s+secret|secret|ts/sci)\b",
    r"must\s+be\s+a\s+u\.?s\.?\s+citizen",
    r"u\.?s\.?\s+citizens?\s+(?:only|required)",
    r"(?:u\.?s\.?\s+citizenship|permanent\s+residen(?:t|cy))\s+(?:is\s+)?required",
    r"requires?\s+permanent\s+(?:work\s+)?authorization\s+to\s+work",
    r"(?:limited|restricted)\s+to\s+(?:persons|people|individuals|candidates|applicants)\s+with\s+(?:an?\s+)?(?:indefinite|unrestricted|permanent)\s+(?:right|authori[sz]ation)\s+to\s+work",
    r"not\s+eligible\s+for[^.]{0,150}\b(?:opt|cpt)\b",
    # "immigration support/assistance" wording that never says "sponsor" (USAA 2026-10-03: "do not apply ...
    # if you will need immigration support (H-1B, TN, STEM OPT)")
    r"(?:do\s+not|don'?t|should\s+not)\s+apply[^.]{0,150}(?:immigration|visa|sponsor|work\s+authorization)",
    r"\bno\s+(?:\w+\s+)?(?:immigration|visa)\s+(?:support|assistance|sponsorship)",
    r"(?:not|unable|cannot|can'?t)\s+(?:\w+\s+){0,4}(?:offer|provide|available|support|assist|extend)[^.]{0,60}(?:immigration|visa)\s+(?:support|assistance|services)",
    r"not\s+eligible\s+for[^.]{0,80}(?:immigration|visa)\s+(?:support|assistance)",
    r"requir\w*\s+(?:\w+\s+){0,3}(?:immigration|visa|employer)\s+(?:support|assistance|sponsorship)[^.]{0,150}(?:not\s+(?:be\s+)?(?:considered|eligible)|ineligible)",
    r"(?:authorized|eligible)\s+to\s+work[^.]{0,80}without[^.]{0,60}(?:sponsor|employer\s+support|visa\s+support|immigration)",
]
BLOCK_RES = [re.compile(p, re.I) for p in BLOCK_PATTERNS]

# Wording that rules out OPT/STEM OPT too (citizenship, clearance, permanent work authorization, or an
# explicit OPT/CPT exclusion). Anything else in BLOCK_PATTERNS only refuses employer sponsorship, which
# a candidate on F-1 OPT / STEM OPT does not need, so it classifies as "OPT Only" instead of "Blocked".
HARD_BLOCK_PATTERNS = [
    r"\bITAR\b",
    r"\b(?:active|current)\s+(?:top\s+secret|secret|ts/sci)\b",
    r"must\s+be\s+a\s+u[.\s]{0,2}s[.\s]{0,2}\s*citizen",  # abbreviation periods are already spaces after normalization
    r"u[.\s]{0,2}s[.\s]{0,2}\s*citizens?\s+(?:only|required)",
    r"(?:u[.\s]{0,2}s[.\s]{0,2}\s*citizenship|permanent\s+residen(?:t|cy))\s+(?:is\s+)?required",
    r"requires?\s+permanent\s+(?:work\s+)?authorization\s+to\s+work",
    r"(?:limited|restricted)\s+to\s+(?:persons|people|individuals|candidates|applicants)\s+with\s+(?:an?\s+)?(?:indefinite|unrestricted|permanent)\s+(?:right|authori[sz]ation)\s+to\s+work",
    r"not\s+eligible\s+for[^.]{0,150}\b(?:opt|cpt)\b",
]
HARD_BLOCK_RES = [re.compile(p, re.I) for p in HARD_BLOCK_PATTERNS]
_OPT_WORD_RE = re.compile(r"\b(?:opt|cpt|stem|f-?1)\b", re.I)
OPT_WINDOW_CHARS = 200  # an OPT/STEM/F-1 mention this close to a no-sponsorship phrase is read as an OPT exclusion

# Abbreviations whose internal periods break the "[^.]{0,N}" same-sentence
# windows above (a period-based window can't span "U.S." without this) -
# normalized out of the text before matching, in classify_sponsorship() only.
_ABBREV_RE = re.compile(r"\bU\.S\.(?:A\.)?|\bU\.K\.", re.I)  # no trailing \b: it never matches before a space


def _normalize_abbreviations(text):
    """Replaces each abbreviation's internal periods with spaces (never removes
    characters), so match offsets into the ORIGINAL text still line up - lets
    classify_sponsorship() slice the evidence excerpt straight out of `text`."""
    return _ABBREV_RE.sub(lambda m: m.group(0).replace(".", " "), text)


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
    for attempt in range(RETRY_429_MAX + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == RETRY_429_MAX:
                raise
            try:
                delay = float(e.headers.get("Retry-After"))
            except (TypeError, ValueError):
                delay = 2 ** (attempt + 1)
            time.sleep(delay + random.random())
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == RETRY_429_MAX:  # connection resets mid-pagination must not lose a whole deep fetch
                raise
            time.sleep(2 ** (attempt + 1) + random.random())
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
            # createdAt is an absolute UTC timestamp - must convert with an explicit UTC tz, not
            # date.fromtimestamp()'s local-machine tz, or the same posting gets a different date
            # depending on whether this ran on the UTC GitHub Actions cron or a local US-Eastern
            # run (confirmed: a controlled 2026-09-26T02:00 UTC timestamp came out as 09-25 local)
            posted = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
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
    ceiling = int(c.get("max_postings", 2000))
    while offset < ceiling:
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


_RELATIVE_DAYS_RE = re.compile(r"posted\s+(today|yesterday|(\d+)(\+?)\s*day)", re.I)


_MULTI_LOC_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def _workday_location(text, path):
    """Workday shows "2 Locations" for multi-site postings, hiding the city from locations_exclude;
    the city is still in externalPath (/job/London/...), so append it."""
    text = text or ""
    if _MULTI_LOC_RE.match(text):
        m = re.match(r"/job/([^/]+)/", path or "")
        if m:
            return f"{text} ({m.group(1).replace('-', ' ')})"
    return text


def _workday_posted(text):
    """Workday's postedOn is relative text ("Posted 5 Days Ago", "Posted Today", "Posted 30+ Days
    Ago") not a real date - approximate it as an ISO date so postings can be sorted/filtered by
    recency. "30+" is a floor, not a real day count (Workday caps the display at that bucket, so
    the true age could be 30 days or 300) - computing today-minus-30 for it would print a specific
    date that looks exact but is actually a guess, so treat it as unknown ("") instead. This is
    meant as a last-resort fallback when the detail endpoint (which usually has the real date via
    startDate - see _workday_detail_posted) couldn't be reached at all."""
    m = _RELATIVE_DAYS_RE.search(text or "")
    if not m:
        return ""
    if m.group(1).lower() == "today":
        days = 0
    elif m.group(1).lower() == "yesterday":
        days = 1
    elif m.group(3) == "+":
        return ""  # "30+ Days Ago" - a floor, not a count; don't dress it up as a real date
    else:
        days = int(m.group(2))
    return (date.today() - timedelta(days=days)).isoformat()


def _workday_detail_posted(info):
    """Workday's job-DETAIL response (unlike the bulk search listing) usually carries a startDate -
    prefer it over the relative postedOn bucket text, and over the bulk listing's own postedOn
    (which some tenants, e.g. Ares, omit from the bulk response entirely).

    Returns (date, source) - source is "exact" for startDate, "approx" for the relative-text
    fallback (derived from a coarse day-count string, not a timestamp - see _workday_posted), or
    "unknown" for neither. Callers surface this so a date on the tracker page never looks more
    certain than it actually is.

    "exact" is still not perfectly stable, confirmed live: the same unedited posting returned
    startDate one calendar day apart in two checks made hours apart, meaning Workday recomputes it
    relative to its own server clock at request time too, the same way postedOn's relative text is,
    just with finer granularity and a much smaller/rarer drift window (one day, around Workday's own
    day boundary, vs. every single request for postedOn). That's still far more reliable than the
    relative-text fallback, so "exact" stays the right label relative to "approx" - but it's why
    _reverify_recent_posted() re-checks a freshly captured value a day or two later instead of
    trusting it permanently from the moment of first capture."""
    start = (info.get("startDate") or "")[:10]
    try:
        if start and date.fromisoformat(start) <= date.today() + timedelta(days=1):
            return start, "exact"
    except ValueError:
        pass
    fallback = _workday_posted(info.get("postedOn"))
    return fallback, ("approx" if fallback else "unknown")


def _workday_refresh_posted(c, job_id, site):
    """Re-fetches just the detail endpoint's date fields (no description/salary/etc - this only
    runs to re-verify a date, so there's no reason to re-parse and re-classify the whole posting)."""
    d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}{job_id}")
    return _workday_detail_posted(d.get("jobPostingInfo") or {})


def workday(c):
    """Unofficial endpoint. Needs host, tenant, site. 'search' is a term or a list of terms; each
    term is queried separately (max_per_term results, default 60) and results are merged by id.
    'site' may also be a list of site paths under the same tenant - some subsidiaries post to a
    separate career site sharing the parent's Workday tenant (e.g. PGIM/Prudential: same host and
    tenant, "PGIM_Careers" vs "Careers"), and the same requisition can appear on both - tracking
    them as one company lets the existing repost-fingerprint matching recognize that instead of
    silently double-counting it as two unrelated postings under two different company names."""
    sites = c["site"] if isinstance(c["site"], list) else [c["site"]]
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    ceiling = int(c.get("term_ceiling", 1000))
    out, seen = [], set()
    page = 20  # Workday's unofficial endpoint 400s on limit > 20 - confirmed by testing, not documented
    complete = True  # AND across terms/sites: one truncated one makes the whole listing untrustworthy for removal

    def fetch_term(site, term):
        base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}/jobs"
        rows_all, offset, term_complete, limit, total = [], 0, False, cap, None
        while offset < limit:
            d = http(base, data={"appliedFacets": {}, "limit": page, "offset": offset, "searchText": term})
            rows = d.get("jobPostings", [])
            rows_all.extend(rows)
            if offset == 0 and isinstance(d.get("total"), int):
                total = d["total"]  # only the first page reliably carries total
                limit = max(cap, min(total, ceiling))  # page past cap, up to ceiling, so a broad term can still complete
            offset += page
            if len(rows) < page or (total is not None and offset >= total):
                term_complete = True  # ran out of rows before hitting the limit
                break
        return rows_all, term_complete

    # Slow tenants opt in via config "term_workers" (RBC answers in ~5s per request, so 16 sequential
    # multi-page terms took 4 min). Default stays 1: running every tenant's terms in parallel got
    # 429s on unrelated tenants from the shared per-IP load. map() keeps term order, so output is unchanged.
    combos = [(site, term) for site in sites for term in terms]
    workers = max(1, min(int(c.get("term_workers", 1)), len(combos)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda st: fetch_term(*st), combos))
    for (site, _term), (rows, term_complete) in zip(combos, results):
        for j in rows:
            path = j.get("externalPath")
            key = (site, path)
            if not path or not j.get("title") or key in seen:  # some rows are placeholder cards with no title/path
                continue
            seen.add(key)
            out.append(dict(id=path, title=j["title"], location=_workday_location(j.get("locationsText"), path),
                            url=f"{c.get('url_base') or 'https://' + c['host']}/{site}{path}", site=site,
                            posted=_workday_posted(j.get("postedOn"))))
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
    ceiling = int(c.get("term_ceiling", 1000))
    page = 50
    out, seen = [], set()
    complete = True  # AND across terms: one truncated term makes the whole listing untrustworthy for removal
    for term in terms:
        offset, limit, total = 0, cap, None
        term_complete = False
        while offset < limit:
            finder = f"findReqs;siteNumber={c['site']},limit={page},offset={offset}"
            if term:
                finder += f",keyword={term}"
            url = base + "?" + urllib.parse.urlencode({"onlyData": "true", "expand": "requisitionList", "finder": finder})
            d = http(url)
            items = d.get("items") or [{}]
            rows = items[0].get("requisitionList") or []
            if offset == 0 and isinstance(items[0].get("TotalJobsCount"), int):
                total = items[0]["TotalJobsCount"]
                limit = max(cap, min(total, ceiling))  # page past cap, up to ceiling, so a broad term can still complete
            for j in rows:
                jid = str(j.get("Id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append(dict(id=jid, title=j.get("Title", "") or "", location=j.get("PrimaryLocation", "") or "",
                                url=f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}/job/{jid}",
                                posted=(j.get("PostedDate") or "")[:10]))
            offset += page
            if len(rows) < page or (total is not None and offset >= total):
                term_complete = True  # ran out of rows before hitting the limit
                break
        complete = complete and term_complete
    return out, complete


def personio(c):
    """Personio's public XML job feed - no auth, no API key. Needs 'slug' (the company subdomain
    in https://{slug}.jobs.personio.de). Verified live against a real tenant (ottonova.jobs.
    personio.de) on 2026-09-27, including a real Risk Management & Actuary posting - confirms the
    feed genuinely carries finance-relevant roles, not just this doc example.

    Each tenant must have turned the XML feed on themselves (Settings > Recruiting > Career page),
    so a candidate company with no career site on personio.de, or one that never enabled it, 404s
    or returns an empty <workzag-jobs/> - both look like "0 jobs" via --check, not an error, so
    verify a candidate by curling the URL directly before adding it to config.json, same as every
    other ATS here.

    The feed embeds the full description already (unlike Greenhouse/SmartRecruiters/Workday/
    Oracle, which need a second per-job detail call) - see the jobDescriptions/jobDescription/value
    CDATA blocks below - so describe() never needs a personio-specific branch."""
    raw = http(f"https://{c['slug']}.jobs.personio.de/xml?language=en", raw=True)
    root = ET.fromstring(raw)
    jobs = []
    for pos in root.findall("position"):
        jid = pos.findtext("id", "") or ""
        offices = [pos.findtext("office", "") or ""]
        offices += [o.text or "" for o in pos.findall("additionalOffices/office")]
        desc = " ".join(strip_html(v.text or "") for v in pos.findall("jobDescriptions/jobDescription/value"))
        jobs.append(dict(id=jid, title=pos.findtext("name", "") or "",
                         location=", ".join(o for o in offices if o),
                         url=f"https://{c['slug']}.jobs.personio.de/job/{jid}?language=en",
                         desc=desc, posted=(pos.findtext("createdAt", "") or "")[:10]))
    return jobs, True  # single uncapped call: always the full board


def goldman(c):
    """Goldman Sachs' own careers site (higher.gs.com) - unofficial GraphQL gateway, reverse-engineered
    and verified live 2026-10-02 (not in any doc). No config beyond 'search' terms. `experiences` must
    be non-empty (an empty list is a validation error); the list call already returns descriptionHtml,
    so describe() needs no goldman branch. `posted` is lastPostedDate (the real posting day; createdDate trails it by a day)."""
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 100))
    ceiling = int(c.get("term_ceiling", 1000))
    page = 50
    query = ("query GetRoles($searchQueryInput: RoleSearchQueryInput!) { roleSearch(searchQueryInput: "
             "$searchQueryInput) { totalCount items { roleId jobTitle division descriptionHtml lastPostedDate createdDate "
             "locations { city state country } externalSource { sourceId } } } }")
    out, seen = [], set()
    complete = True
    for term in terms:
        offset, term_complete, limit, total = 0, False, cap, None
        while offset < limit:
            body = {"operationName": "GetRoles", "query": query, "variables": {"searchQueryInput": {
                "page": {"pageSize": page, "pageNumber": offset // page},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"}, "filters": [],
                "experiences": ["PROFESSIONAL", "EARLY_CAREER"], "searchTerm": term}}}
            d = http("https://api-higher.gs.com/gateway/api/v1/graphql", data=body)
            if d.get("errors"):
                raise RuntimeError(f"goldman graphql: {d['errors'][0].get('message')}")
            rows = ((d.get("data") or {}).get("roleSearch") or {}).get("items") or []
            if offset == 0 and isinstance(((d.get("data") or {}).get("roleSearch") or {}).get("totalCount"), int):
                total = d["data"]["roleSearch"]["totalCount"]
                limit = max(cap, min(total, ceiling))  # page past cap, up to ceiling, so a broad term can still complete
            for j in rows:
                jid = str(j.get("roleId") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = [", ".join(x for x in (l.get("city"), l.get("state"), l.get("country")) if x)
                        for l in (j.get("locations") or [])]
                src = (j.get("externalSource") or {}).get("sourceId") or jid.split("_")[0]
                out.append(dict(id=jid, title=j.get("jobTitle", "") or "", location=" | ".join(locs),
                                url=f"https://higher.gs.com/roles/{src}",
                                posted=(j.get("lastPostedDate") or j.get("createdDate") or "")[:10],
                                desc=strip_html(j.get("descriptionHtml", ""))))
            offset += page
            if len(rows) < page or (total is not None and offset >= total):
                term_complete = True
                break
        complete = complete and term_complete
    return out, complete


def successfactors(c):
    """SAP SuccessFactors Career Site Builder tenants that expose the public RSS job feed. Needs
    host and locale (e.g. jobs.scotiabank.com / en_US). Verified live 2026-10-02 against Scotiabank,
    Standard Chartered and SMBC. Feed shape: /services/rss/job/?locale=..&keywords=(term)&startrow=N,
    20 items per page, each with the full description, link, guid and pubDate; the location is the
    trailing parenthetical of the title. Only tenants using the Career Site Builder front end have
    this feed (Moody's career8 login pages do not), so verify a host by curl before adding it."""
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 60))
    page = 20
    out, seen = [], set()
    complete = True
    for term in terms:
        offset, term_complete, term_seen = 0, False, set()
        while offset < cap:
            q = {"locale": c.get("locale", "en_US"), "startrow": offset}
            if term:
                q["keywords"] = f"({term})"
            raw = http(f"https://{c['host']}/services/rss/job/?" + urllib.parse.urlencode(q), raw=True, timeout=40,
                       headers={"Accept": "application/rss+xml, */*"})  # default json-only Accept gets a 406
            items = ET.fromstring(raw).findall("channel/item")
            fresh = 0
            for it in items:
                link = (it.findtext("link", "") or "").strip()
                parts = urllib.parse.urlsplit(link)
                link = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))  # drop RSS tracking params
                jid = parts.path.rstrip("/").rsplit("/", 1)[-1]
                if not jid:
                    continue
                if jid not in term_seen:
                    term_seen.add(jid)
                    fresh += 1  # fresh is per search, so a page that only repeats earlier searches still pages on
                if jid in seen:
                    continue
                seen.add(jid)
                title = (it.findtext("title", "") or "").strip()
                m = re.match(r"^(.*)\s\(([^()]*)\)$", title)
                title, loc = (m.group(1), m.group(2)) if m else (title, "")
                try:
                    posted = parsedate_to_datetime(it.findtext("pubDate", "")).date().isoformat()
                except (TypeError, ValueError):
                    posted = ""
                out.append(dict(id=jid, title=title, location=loc, url=link, posted=posted,
                                desc=strip_html(it.findtext("description", ""))))
            offset += page
            if len(items) < page or not fresh:
                term_complete = True
                break
        complete = complete and term_complete
    return out, complete


def _icims_location(text):
    """iCIMS shows "US-NY-New York" / "PH-Makati City Manila" (country-[state-]city); rewrite it as
    "New York, NY, US" so the shared location filters read it like every other ATS."""
    parts = [x.strip() for x in (text or "").split("-", 2)]
    if len(parts) >= 2 and len(parts[0]) == 2 and parts[0].isalpha():
        return ", ".join(reversed(parts))
    return (text or "").strip()


def icims(c):
    """iCIMS career portals (server-rendered HTML job cards, no API). Needs 'hosts' (one or more
    portal hosts, e.g. MSCI splits global/us/uk/rsa across four). Verified live 2026-10-02 against
    MSCI. List pages carry title, id, location and posted date; the full description comes from each
    job page's JSON-LD block via describe(). Paged with pr=0,1,.. until a page has no cards."""
    hosts = c.get("hosts") or [c["host"]]
    cap_pages = int(c.get("max_pages", 20))
    out, seen = [], set()
    complete = True
    for host in hosts:
        for pr in range(cap_pages):
            raw = http(f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={pr}", raw=True, timeout=40,
                       headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
            cards = raw.split('class="iCIMS_JobCardItem"')[1:]
            for card in cards:
                m = re.search(r'href="(https://[^"]+/jobs/(\d+)/[^"?]*)', card)
                if not m or m.group(2) in seen:  # one posting can appear on several of a tenant's portals
                    continue
                seen.add(m.group(2))
                title = re.search(r"<h3[^>]*>\s*(.*?)\s*</h3>", card, re.S)
                loc = (re.search(r"Job Locations</span>.*?<span[^>]*>\s*(.*?)\s*</span>", card, re.S)
                       or re.search(r">Locations?</span>\s*<span[^>]*>\s*(.*?)\s*</span>", card, re.S))  # Stifel-style cards
                posted = re.search(r'<span title="(\d{1,2})/(\d{1,2})/(\d{4})', card)
                out.append(dict(id=m.group(2), title=strip_html(title.group(1)) if title else "",
                                location=_icims_location(strip_html(loc.group(1)) if loc else ""),
                                url=m.group(1),
                                posted=f"{posted.group(3)}-{int(posted.group(1)):02d}-{int(posted.group(2)):02d}" if posted else ""))
            if not cards:
                break
        else:
            complete = False  # hit the page cap on this portal
    return out, complete


def _icims_description(job):
    """(description, posted) from a job page's JSON-LD. The list card often has no posted date (Schwab's
    doesn't), so describe() returns the JSON-LD datePosted, which refines it for each new match."""
    raw = http(job["url"] + "?in_iframe=1", raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    m = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        return "", ""
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return "", ""
    posted = str(data.get("datePosted") or "")[:10]
    return strip_html(data.get("description", "")), posted if re.match(r"\d{4}-\d\d-\d\d$", posted) else ""


def eightfold(c):
    """Eightfold career sites (static assets on static.vscdn.net) - public /api/apply/v2/jobs endpoint.
    Needs host and domain (e.g. careers.newyorklife.com / newyorklife.com). Verified live 2026-10-02
    against New York Life (279 jobs, whole board) and HSBC (portal.careers.hsbc.com, thousands of jobs,
    so it opts into 'search' terms). The endpoint caps every page at 10 results whatever `num` says,
    so it pages with start += 10, once per search term (no 'search' key = the whole board, capped by
    max_per_term). The list rows carry no description; describe() reads it from the per-job endpoint."""
    page = 10
    terms = c.get("search") or [""]
    terms = [terms] if isinstance(terms, str) else terms
    cap = int(c.get("max_per_term", 1500))
    ceiling = int(c.get("term_ceiling", 1000))
    out, seen = [], set()
    complete = True
    for term in terms:
        start, term_complete, limit = 0, False, cap
        while start < limit:
            q = urllib.parse.urlencode({"domain": c["domain"], "start": start, "num": page, "query": term})
            d = http(f"https://{c['host']}/api/apply/v2/jobs?{q}", headers={"Accept": "application/json"})
            total = d.get("count")
            if start == 0 and isinstance(total, int):
                limit = max(cap, min(total, ceiling))  # page past cap, up to ceiling, so a broad term can still complete
            rows = d.get("positions") or []
            for j in rows:
                jid = str(j.get("id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = j.get("locations") or [j.get("location") or ""]
                posted = ""
                if j.get("t_create"):
                    posted = datetime.fromtimestamp(int(j["t_create"]), timezone.utc).date().isoformat()
                out.append(dict(id=jid, title=j.get("name", "") or "", location=" | ".join(str(x) for x in locs if x),
                                url=j.get("canonicalPositionUrl") or f"https://{c['host']}/careers/job/{jid}", posted=posted))
            start += page
            if len(rows) < page or (total is not None and start >= total):
                term_complete = True
                break
        complete = complete and term_complete
    return out, complete


def _jsonld_description(url):
    """Description from a job page's schema.org JobPosting JSON-LD block (used by TalentBrew)."""
    raw = http(url, raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', raw, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("description"):
            return strip_html(data["description"])
    return ""


def talentbrew(c):
    """Radancy TalentBrew career sites (job URLs like /en/job/<city>/<title>/<site id>/<job id>, search
    at /search-jobs). Needs host. Verified live 2026-10-02 against Moody's. The site's own AJAX
    endpoint /search-jobs/results returns JSON whose "results" field is the job-card HTML (title,
    location, MM/DD/YYYY posted, data-job-id) 50 per page with data-total-pages; the description
    comes from each job page's JSON-LD via describe()."""
    out, seen = [], set()
    total_pages = 1
    page = 1
    while page <= min(total_pages, int(c.get("max_pages", 40))):
        q = urllib.parse.urlencode({
            "ActiveFacetID": 0, "CurrentPage": page, "RecordsPerPage": 50, "Distance": 50, "RadiusUnitType": 0,
            "Keywords": "", "Location": "", "ShowRadius": "False", "IsPagination": "False", "FacetType": 0,
            "SearchResultsModuleName": "Search Results", "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": 0, "SortDirection": 0, "SearchType": 5, "ResultsType": 0})
        d = http(f"https://{c['host']}/search-jobs/results?{q}", headers={"X-Requested-With": "XMLHttpRequest"}, timeout=40)
        h = d.get("results", "")
        m = re.search(r'data-total-pages="(\d+)"', h)
        if m:
            total_pages = int(m.group(1))
        for card in re.finditer(r'<a href="(/[^"]+/job/[^"]+)" data-job-id="(\d+)">(.*?)</a>', h, re.S):
            jid = card.group(2)
            if jid in seen:
                continue
            seen.add(jid)
            body = card.group(3)
            title = re.search(r"<h2[^>]*>(.*?)</h2>", body, re.S)
            loc = re.search(r'class="job-location">(.*?)</span>', body, re.S)
            posted = re.search(r'class="job-date-posted">(\d{2})/(\d{2})/(\d{4})', body)
            out.append(dict(id=jid, title=strip_html(title.group(1)) if title else "",
                            location=strip_html(loc.group(1)) if loc else "",
                            url=f"https://{c['host']}{card.group(1)}",
                            posted=f"{posted.group(3)}-{posted.group(1)}-{posted.group(2)}" if posted else ""))
        page += 1
    return out, page > total_pages


def avature(c):
    """Avature career sites with a server-rendered list page. Needs host and list_path (the tenant's
    job-list page, e.g. "careers/OpenRoles"; it differs per tenant). Verified live 2026-10-02 against
    Two Sigma. 10 results per page, paged with jobOffset; each result is an <article class="article
    article--result"> holding the JobDetail link (title), then one <span class="paragraph_inner-span">
    per field, the first being the location. Descriptions come from each JobDetail page's JSON-LD."""
    out, seen = [], set()
    offset, page = 0, int(c.get("page_size", 10))
    cap = int(c.get("max_jobs", 1000))
    dates = {}
    if c.get("feed"):
        # Two Sigma's RSS feed carries real pubDates but only its 20 oldest roles (paging params are ignored),
        # so newer jobs keep a blank `posted` and fall back to first_seen.
        try:
            for it in ET.fromstring(http(f"https://{c['host']}/{c['list_path']}/feed/", raw=True, timeout=40,
                                         headers={"Accept": "application/rss+xml, */*"})).findall("channel/item"):
                try:
                    dates[(it.findtext("link", "") or "").rstrip("/").rsplit("/", 1)[-1]] = parsedate_to_datetime(
                        it.findtext("pubDate", "")).date().isoformat()
                except (TypeError, ValueError):
                    pass
        except Exception:  # noqa: BLE001  the feed is a bonus; the list pages are the source of truth
            pass
    while offset < cap:
        raw = http(f"https://{c['host']}/{c['list_path']}?jobOffset={offset}", raw=True, timeout=40,
                   headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        arts = raw.split('class="article article--result')[1:]
        for a in arts:
            if c.get("variant") == "details":
                # Macquarie-style cards: JobDetail?jobId=N link, then icon rows for ID, location, "dd Mon yyyy" posted date
                m = re.search(r'<a class="link" href="([^"]+JobDetail\?jobId=(\d+))"[^>]*>\s*(.*?)\s*</a>', a, re.S)
                if not m or m.group(2) in seen:
                    continue
                seen.add(m.group(2))
                loc = re.search(r'alt="Office Location:"[^>]*>\s*<p>\s*(.*?)\s*</p>', a, re.S)
                pd = re.search(r'alt="Posted Date:"[^>]*>\s*<p>\s*(.*?)\s*</p>', a, re.S)
                posted = ""
                try:
                    posted = datetime.strptime(strip_html(pd.group(1)), "%d %b %Y").date().isoformat() if pd else ""
                except ValueError:
                    pass
                out.append(dict(id=m.group(2), title=strip_html(m.group(3)), url=html.unescape(m.group(1)),
                                location=strip_html(loc.group(1)) if loc else "", posted=posted))
                continue
            m = re.search(r'<a class="link" href="([^"]+/JobDetail/[^"]+/(\d+))"[^>]*>\s*(.*?)\s*</a>', a, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            spans = re.findall(r'<span class="paragraph_inner-span">\s*(.*?)\s*</span>', a, re.S)
            out.append(dict(id=m.group(2), title=strip_html(m.group(3)), url=html.unescape(m.group(1)),
                            location=strip_html(spans[0]) if spans else "", posted=dates.get(m.group(2), "")))
        offset += page
        if len(arts) < page:
            return out, True
    return out, False


def deshaw(c):
    """D. E. Shaw's own careers site (Next.js, server-rendered). The job list is the set of
    /careers/<slug>-<id> links on /careers/choose-your-path; each job page embeds the full job as
    __NEXT_DATA__ JSON (jobData: displayName, jobLocations, description fields), so one page fetch per
    job gives title, location and description. Verified live 2026-10-02 (about 95 jobs). The
    "all-positions-in-..." slugs are category landing pages, not jobs, and are skipped. datePosted on
    the page is just the render time, so `posted` stays blank."""
    raw = http("https://www.deshaw.com/careers/choose-your-path", raw=True, timeout=40,
               headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    paths = sorted(set(re.findall(r'href="(/careers/(?!all-positions)[a-z0-9-]+-\d{4})"', raw)))

    def one(path):
        page = http("https://www.deshaw.com" + path, raw=True, timeout=40,
                    headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
        jd = json.loads(m.group(1))["props"]["pageProps"]["jobData"]
        desc = jd.get("jobDescription") or {}
        text = " ".join(strip_html(x or "") for x in (desc.get("websiteDescription"), desc.get("responsibilitiesHtml"),
                                                       desc.get("peopleWeAreLookingForStr")))
        locs = [x.get("name", "") for x in (jd.get("jobMetadata") or {}).get("jobLocations") or []]
        return dict(id=str(jd["id"]), title=jd.get("displayName", ""), location=" | ".join(x for x in locs if x),
                    url="https://www.deshaw.com" + path, posted="", desc=text)

    out, complete = [], True
    with ThreadPoolExecutor(max_workers=6) as pool:
        for r in pool.map(lambda x: _try(one, x), paths):
            if r is None:
                complete = False  # a page failed: don't let a partial list close queued postings
            else:
                out.append(r)
    return out, complete


def _try(fn, arg):
    try:
        return fn(arg)
    except Exception:  # noqa: BLE001
        return None


def brassring(c):
    """Kenexa/IBM BrassRing "TGnewUI" career sites. Needs host, partnerid, siteid. Verified live
    2026-10-02 against UBS (jobs.ubs.com, about 530 jobs). The AJAX search needs a browser-like session,
    reverse-engineered from the page: (1) GET the search page with a cookie jar, which sets tg_session
    cookies; (2) the cookie tg_session_<partner>_<site> is the `encryptedsessionvalue` for every call;
    (3) every POST also needs header RFT = the hidden __RequestVerificationToken input (apply.js reads
    it the same way) plus Referer/Origin; (4) POST /Search/Ajax/MatchedJobs returns page 1 (50 jobs),
    POST /Search/Ajax/ProcessSortAndShowMoreJobs with pageNumber 2.. returns the rest. Each job is a
    Questions list of name/value pairs; the list call already includes the full jobdescription, so
    describe() needs no brassring branch. Location is formtext23 ("United States - New York")."""
    import http.cookiejar
    base = f"https://{c['host']}"
    pid, sid = str(c["partnerid"]), str(c["siteid"])
    ref = (f"{base}/TGnewUI/Search/home/HomeWithPreLoad?partnerid={pid}&siteid={sid}"
           f"&PageType=searchResults&SearchType=linkquery&LinkID={c.get('linkid', '')}")
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    page = opener.open(urllib.request.Request(ref, headers=ua), timeout=60).read().decode("utf-8", "ignore")
    tok = next(ck.value for ck in cj if ck.name == f"tg_session_{pid}_{sid}")
    rft = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', page).group(1)

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
            **ua, "Content-Type": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01", "Referer": ref, "Origin": base, "RFT": rft})
        return json.loads(opener.open(req, timeout=60).read())

    kw, loc = "FORMTEXT2,FORMTEXT21,AutoReq,Department,JobTitle", "FORMTEXT2,FORMTEXT23,Location"
    first = post("/TgNewUI/Search/Ajax/MatchedJobs", {
        "PartnerId": pid, "SiteId": sid, "Keyword": "", "Location": "", "KeywordCustomSolrFields": kw,
        "LocationCustomSolrFields": loc, "FacetFilterFields": None, "TurnOffHttps": False, "Latitude": 0,
        "Longitude": 0, "PowerSearchOptions": {"PowerSearchOption": []}, "encryptedsessionvalue": tok})
    total = int(first.get("JobsCount") or first.get("TotalJobsCount") or 0)
    rows = list((first.get("Jobs") or {}).get("Job") or [])
    page_no = 2
    while len(rows) < total and page_no <= int(c.get("max_pages", 40)):
        d = post("/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs", {
            "partnerId": pid, "siteId": sid, "keyword": "", "location": "", "keywordCustomSolrFields": kw,
            "locationCustomSolrFields": loc, "linkId": "", "Latitude": 0, "Longitude": 0,
            "facetfilterfields": {"Facet": []}, "powersearchoptions": {"PowerSearchOption": []},
            "SortType": "LastUpdated", "pageNumber": page_no, "encryptedSessionValue": tok})
        got = (d.get("Jobs") or {}).get("Job") or []
        if not got:
            break
        rows.extend(got)
        page_no += 1
    out, seen = [], set()
    for j in rows:
        q = {x.get("QuestionName"): x.get("Value") for x in j.get("Questions") or []}
        jid = str(q.get("reqid") or "")
        if not jid or jid in seen:
            continue
        seen.add(jid)
        posted = ""
        try:
            posted = datetime.strptime(q.get("lastupdated") or "", "%d-%b-%Y").date().isoformat()
        except ValueError:
            pass
        out.append(dict(id=jid, title=strip_html(q.get("jobtitle") or ""), location=strip_html(q.get("formtext23") or ""),
                        url=j.get("Link") or ref, posted=posted, desc=strip_html(q.get("jobdescription") or "")))
    return out, len(rows) >= total


def jibe(c):
    """iCIMS Career Sites ("Jibe") front ends - public /api/jobs?page=N&limit=100 on the careers host.
    Needs host and job_path (the site's public job URL prefix, e.g. "us/jobs"). Verified live 2026-10-02
    against Principal Financial (careers.principal.com, 139 jobs). Each job carries title, location
    fields, the full description/responsibilities/qualifications and an exact posted_date, so no
    describe() call is needed."""
    out, seen = [], set()
    page, limit = 1, 100
    while page <= int(c.get("max_pages", 30)):
        d = http(f"https://{c['host']}/api/jobs?page={page}&limit={limit}", headers={"Accept": "application/json"}, timeout=40)
        rows = d.get("jobs") or []
        for r in rows:
            j = r.get("data") or {}
            jid = str(j.get("req_id") or j.get("slug") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            loc = j.get("full_location") or ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country_code")) if x)
            text = " ".join(strip_html(j.get(k) or "") for k in ("description", "responsibilities", "qualifications"))
            out.append(dict(id=jid, title=j.get("title", "") or "", location=loc or "",
                            url=f"https://{c['host']}/{c.get('job_path', 'jobs').strip('/')}/{j.get('slug') or jid}",
                            posted=str(j.get("posted_date") or j.get("create_date") or "")[:10], desc=text))
        if len(rows) < limit:
            return out, True
        page += 1
    return out, False


def taleo(c):
    """Oracle Taleo Enterprise career sections - POST /careersection/rest/jobboard/searchjobs (25 per page).
    Needs host, section and portal (the number in the section's jobsearch.ftl?portal= URL). The list
    columns are title, locations (JSON string) and an exact posted date. Verified live 2026-10-02
    against Equitable (equitable.taleo.net, section eqh_1)."""
    host, sec, portal = c["host"], c["section"], c["portal"]
    empty = lambda ids: [dict(id=i, selectedValues=[]) for i in ids]
    out, seen, page = [], set(), 1
    while page <= int(c.get("max_pages", 60)):
        body = {"multilineEnabled": False, "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": empty(["POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE", "JOB_LEVEL"])},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": empty(["ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB", "EMPLOYEE_STATUS", "STUDY_LEVEL", "WILL_TRAVEL", "JOB_SHIFT"])},
                "pageNo": page}
        d = http(f"https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}", data=body, timeout=40,
                 headers={"Accept": "application/json", "tz": "GMT-05:00", "tzname": "America/New_York", "X-Requested-With": "XMLHttpRequest"})
        rows = d.get("requisitionList") or []
        for r in rows:
            jid = str(r.get("contestNo") or r.get("jobId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            col = r.get("column") or []
            try:
                locs = json.loads(col[1]) if len(col) > 1 else []
                loc = "; ".join(locs) if isinstance(locs, list) else str(locs)
            except ValueError:
                loc = col[1] if len(col) > 1 else ""
            posted = ""
            try:
                posted = datetime.strptime(col[2].strip(), "%b %d, %Y").date().isoformat() if len(col) > 2 else ""
            except ValueError:
                pass
            out.append(dict(id=jid, title=strip_html(col[0]) if col else "", location=loc,
                            url=f"https://{host}/careersection/{sec}/jobdetail.ftl?job={jid}&lang=en", posted=posted))
        if len(rows) < int((d.get("pagingData") or {}).get("pageSize") or 25):
            return out, True
        page += 1
    return out, False


def hrmdirect(c):
    """ClearCompany HRM Direct boards (e.g. Oppenheimer, opco.hrmdirect.com) - server-rendered list at
    /employment/job-openings.php, one detail page per req for location and description. No posted date is
    published, so first_seen stands in. Verified live 2026-10-03 (51 reqs)."""
    base = f"https://{c['host']}/employment"
    raw = http(f"{base}/job-openings.php?search=true&nohd=&dept=-1&office=-1&cust_sort1=-1", raw=True, timeout=40,
               headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
    out, seen = [], set()
    for req, title in re.findall(r"""<a href="job-opening\.php\?req=(\d+)[^"]*">([^<]+)</a>""", raw):
        if req in seen:
            continue
        seen.add(req)
        url = f"{base}/job-opening.php?req={req}&&nohd"
        page = http(url, raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
        text = strip_html(page)
        m = re.search(r"Location:\s*(.+?)\s+Type of Hire:", text)
        out.append(dict(id=req, title=strip_html(title), location=m.group(1) if m else "", url=url, posted="", desc=text[:30000]))
    return out, True


def commerzbank(c):
    """Commerzbank's own job board (jobs.commerzbank.com) - public POST api-jobs.commerzbank.com/search/ with
    an OData-style SearchParameters body; FirstItem is 1-based. Each item carries title, URI, locations and
    an exact PublicationStartDate. Verified live 2026-10-03 (450 jobs). The description comes from describe()."""
    out, seen, first, step = [], set(), 1, 100
    while first < int(c.get("max_jobs", 2000)):
        body = {"SearchParameters": {"FirstItem": first, "CountItem": step, "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
                                     "MatchedObjectDescriptor": ["ID", "PositionTitle", "PositionURI", "PositionLocation.CountryName", "PositionLocation.CityName", "PublicationStartDate"]},
                "SearchCriteria": [], "LanguageCode": "EN"}
        d = http("https://api-jobs.commerzbank.com/search/", data=body, timeout=40,
                 headers={"Accept": "application/json", "Origin": "https://jobs.commerzbank.com", "Referer": "https://jobs.commerzbank.com/"})
        items = (d.get("SearchResult") or {}).get("SearchResultItems") or []
        for it in items:
            m = it.get("MatchedObjectDescriptor") or {}
            jid = str(m.get("ID") or it.get("MatchedObjectId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            locs = ["{}, {}".format(x.get("CityName", ""), x.get("CountryName", "")).strip(", ") for x in m.get("PositionLocation") or []]
            out.append(dict(id=jid, title=m.get("PositionTitle", "") or "", location="; ".join(locs), url=m.get("PositionURI") or f"https://jobs.commerzbank.com/index.php?ac=jobad&id={jid}",
                            posted=str(m.get("PublicationStartDate") or "")[:10]))
        if len(items) < step:
            return out, True
        first += step
    return out, False


def dzbank(c):
    """DZ Bank's karriere.dzbank.de publishes the whole board, descriptions included, as one JSON array at
    /bin/dzbank/sapjobs.json (SuccessFactors behind it). Verified live 2026-10-03 (56 jobs, dd.mm.yyyy dates)."""
    rows = http("https://karriere.dzbank.de/bin/dzbank/sapjobs.json", timeout=40)
    out = []
    for r in rows:
        posted = ""
        try:
            posted = datetime.strptime(r.get("publiziert") or "", "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass
        out.append(dict(id=str(r.get("jobReqId")), title=r.get("stellenbezeichnung", "") or "", location=r.get("standort", "") or "",
                        url=r.get("joblink") or "", posted=posted, desc=strip_html(r.get("beschreibung") or "")))
    return [j for j in out if j["id"] and j["url"]], True


def marketaxess(c):
    """MarketAxess serves its Greenhouse Harvest data through a public proxy: harvest.php?path=/jobs/ (opened_at,
    offices) and /job_posts/ (title, location, HTML content). Detail URL is /careers/current-openings/detail/<slug>-<job_id>.
    Verified live 2026-10-03 (6 open jobs)."""
    base = "https://www.marketaxess.com/harvest.php?path="
    hdr = {"Accept": "application/json"}
    opened = {str(j.get("id")): str(j.get("opened_at") or "")[:10] for j in http(base + "/jobs/", headers=hdr, timeout=40)}
    out = []
    for p in http(base + "/job_posts/", headers=hdr, timeout=40):
        jid = str(p.get("job_id") or p.get("id"))
        title = p.get("title", "") or ""
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        out.append(dict(id=jid, title=title, location=(p.get("location") or {}).get("name", ""),
                        url=f"https://www.marketaxess.com/careers/current-openings/detail/{slug}-{jid}",
                        posted=opened.get(jid, ""), desc=strip_html(p.get("content") or "")))
    return out, True


def phenom(c):
    """Phenom People career sites. POST https://<host>/widgets with ddoKey "refineSearch" after one GET of the
    search page for the session cookie. Needs host, ref_num (the site's refNum), page_id (the search page's
    pageId) and optionally path (search page path, default /us/en/search-results) and lang/country. The list
    carries title, location and an exact postedDate; descriptions come from each job page's JSON-LD.
    Verified live 2026-10-03 against Franklin Templeton (198 jobs)."""
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    hdr = {"User-Agent": UA["User-Agent"]}
    host, country = c["host"], c.get("country", "us")
    opener.open(urllib.request.Request(f"https://{host}{c.get('path', '/us/en/search-results')}", headers=hdr), timeout=40).read()
    out, seen, offset, size = [], set(), 0, 50
    while offset < int(c.get("max_jobs", 2000)):
        body = {"sortBy": "", "subsearch": "", "from": offset, "jobs": True, "counts": True,
                "all_fields": ["category", "country", "state", "city", "type"], "pageName": "search-results", "size": size,
                "clearAll": False, "jdsource": "facets", "isSliderEnable": False, "pageId": c["page_id"], "siteType": "external",
                "keywords": "", "global": True, "selected_fields": {}, "sort": {"order": "desc", "field": "postedDate"},
                "lang": c.get("lang", "en_us"), "deviceType": "desktop", "country": country, "refNum": c["ref_num"], "ddoKey": "refineSearch"}
        raw = opener.open(urllib.request.Request(f"https://{host}/widgets", data=json.dumps(body).encode(),
                                                 headers=dict(hdr, **{"Content-Type": "application/json"})), timeout=40).read()
        data = json.loads(raw).get("refineSearch") or {}
        rows = (data.get("data") or {}).get("jobs") or []
        for r in rows:
            jid = str(r.get("jobSeqNo") or r.get("jobId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            slug = re.sub(r"[^A-Za-z0-9]+", "-", r.get("title", "") or "").strip("-")
            out.append(dict(id=jid, title=r.get("title", "") or "", location=r.get("location") or r.get("cityStateCountry") or "",
                            url=f"https://{host}/{country}/{c.get('lang', 'en_us')[:2]}/job/{jid}/{slug}",
                            posted=str(r.get("postedDate") or r.get("dateCreated") or "")[:10]))
        if len(rows) < size:
            return out, True
        offset += size
    return out, False


FETCH = dict(greenhouse=greenhouse, lever=lever, ashby=ashby, smartrecruiters=smartrecruiters, workday=workday,
             oracle=oracle, personio=personio, goldman=goldman, successfactors=successfactors, icims=icims, eightfold=eightfold, talentbrew=talentbrew, avature=avature, deshaw=deshaw, brassring=brassring, jibe=jibe, taleo=taleo, hrmdirect=hrmdirect, commerzbank=commerzbank, dzbank=dzbank, marketaxess=marketaxess, phenom=phenom)
STALE_ALERT_HOURS = 10  # page shows a red banner when the last scout run is older than this
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


def _timed_fetch(c):
    start = time.time()
    name, jobs, complete, err = fetch_company(c)
    return name, jobs, complete, err, time.time() - start


def fetch_all(companies):
    fetched, secs = {}, {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(_timed_fetch, c): c for c in companies}
        for fut in as_completed(futures):
            name, jobs, complete, err, took = fut.result()
            fetched[name] = (jobs, complete, err)
            secs[name] = took
    return fetched, secs


def _fetch_cost(c):
    if c["ats"] != "workday":
        return 1
    sites = c["site"] if isinstance(c["site"], list) else [c["site"]]
    terms = c.get("search") or [""]
    return len(sites) * (1 if isinstance(terms, str) else len(terms))


FETCH_TIMES_PATH = HOME / "fetch_times.json"  # committed, so every shard job of a run splits identically


def shard_companies(companies, index, total, times=None):
    """Deterministic longest-first split onto the least-loaded shard, using measured seconds per
    company (falls back to the call-count estimate for a company with no measurement yet)."""
    times = times or {}
    ordered = sorted(companies, key=lambda c: (-times.get(c["name"], _fetch_cost(c)), c["name"]))
    loads = [0.0] * total
    mine = []
    for c in ordered:
        k = loads.index(min(loads))
        loads[k] += times.get(c["name"], _fetch_cost(c))
        if k == index:
            mine.append(c)
    return mine


def update_fetch_times(directory, path=None):
    """Blends each shard file's measured seconds into the stored per-company times (moving average, so one 429-retry spike doesn't skew the split)."""
    path = Path(path or FETCH_TIMES_PATH)
    times = load_json(path, {})
    for p in sorted(Path(directory).glob("*.json")):
        for name, row in json.loads(p.read_text(encoding="utf-8")).items():
            took = row.get("secs")
            if took is not None:
                times[name] = round(0.5 * times[name] + 0.5 * took, 2) if name in times else round(took, 2)
    atomic_json.write(str(path), times)


def cmd_fetch_shard(args):
    cfg = load_json(HOME / "config.json", None)
    if cfg is None:
        sys.exit(f"No config at {HOME / 'config.json'}.")
    index, total = (int(x) for x in args.fetch_shard.split("/"))
    mine = shard_companies(cfg["companies"], index, total, load_json(FETCH_TIMES_PATH, {}))
    fetched, secs = fetch_all(mine)
    out = {name: dict(jobs=jobs, complete=complete, err=None if err is None else str(err), secs=secs[name])
           for name, (jobs, complete, err) in fetched.items()}
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    print(f"shard {index}/{total}: fetched {len(out)} companies -> {args.out}")


def load_fetched(directory, companies):
    """Merges every shard file; a company missing from all of them (failed shard) is an error, which
    run() already treats as skip-without-closing-anything."""
    merged = {}
    for p in sorted(Path(directory).glob("*.json")):
        merged.update(json.loads(p.read_text(encoding="utf-8")))
    return {c["name"]: ((merged[c["name"]]["jobs"], merged[c["name"]]["complete"],
                         merged[c["name"]]["err"]) if c["name"] in merged
                        else (None, None, "shard result missing"))
            for c in companies}


def describe(c, job):
    """Full description text for one job, plus a Workday date refinement. Returns (text, posted,
    posted_source) - posted_source is "exact" for Workday's detail-endpoint startDate, "approx"
    for its relative-text fallback, and "" for every other ATS (their list call already returns an
    exact native date, so there's nothing to refine - see the per-fetcher functions above). Lever/
    Ashby already carry the description from the list call."""
    if job.get("desc"):
        return job["desc"], "", ""
    ats = c["ats"]
    if ats == "greenhouse":
        d = http(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs/{job['id']}")
        return strip_html(d.get("content", "")), "", ""
    if ats == "smartrecruiters":
        d = http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings/{job['id']}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        return strip_html(" ".join((s or {}).get("text", "") for s in secs.values())), "", ""
    if ats == "workday":
        cand = [job["site"]] if job.get("site") else (c["site"] if isinstance(c["site"], list) else [c["site"]])
        d, err = None, None
        for site in cand:  # queue entries without a per-job site try each configured site
            try:
                d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}{job['id']}")
                break
            except Exception as e:  # noqa: BLE001
                err = e
        if d is None:
            raise err
        info = d.get("jobPostingInfo") or {}
        posted, posted_source = _workday_detail_posted(info)
        return strip_html(info.get("jobDescription", "")), posted, posted_source
    if ats == "oracle":
        finder = f"ById;Id={job['id']}"
        url = (f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?"
               + urllib.parse.urlencode({"onlyData": "true", "finder": finder}))
        d = http(url)
        items = d.get("items") or []
        if not items:
            return "", "", ""
        it = items[0]
        parts = [it.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
        return strip_html(" ".join(p for p in parts if p)), "", ""
    if ats == "icims":
        text, posted = _icims_description(job)
        return text, posted, "exact" if posted else ""
    if ats == "avature":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        raw = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", raw)
        return strip_html(raw)[:30000], "", ""  # Avature job pages carry no JSON-LD; whole-page text is enough for the sponsorship/experience regexes
    if ats == "commerzbank":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        return strip_html(re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", raw))[:30000], "", ""
    if ats == "phenom":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        for m in re.finditer(r'"description":"((?:[^"\\]|\\.)*)"', raw):
            try:
                text = strip_html(json.loads('"' + m.group(1) + '"'))
            except ValueError:
                continue
            if len(text) > 200:  # the page also carries short site-level descriptions
                return text, "", ""
        return _jsonld_description(job["url"]), "", ""
    if ats == "talentbrew":
        return _jsonld_description(job["url"]), "", ""
    if ats == "taleo":
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        i = raw.find("api.fillList('requisitionDescriptionInterface', 'descRequisition'")
        if i < 0:
            return "", "", ""
        arr = raw[i:raw.find("]);", i)]
        parts = [urllib.parse.unquote(m) for m in re.findall(r"'([^']*)'", arr)[3:] if len(m) > 40]  # description fields are the long URL-encoded strings
        return strip_html(" ".join(p.replace("!*!", " ") for p in parts))[:30000], "", ""
    if ats == "successfactors":  # list call carries desc, but a queue entry has only its link; read the page's description block
        raw = http(job["url"], raw=True, timeout=40, headers={"Accept": "text/html, */*"}).decode("utf-8", "ignore")
        i = raw.find('itemprop="description"')
        if i < 0:
            return "", "", ""
        return strip_html(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw[raw.find(">", i) + 1:]))[:30000], "", ""
    if ats == "eightfold":
        d = http(f"https://{c['host']}/api/apply/v2/jobs/{job['id']}?domain={c['domain']}",
                 headers={"Accept": "application/json"})
        return strip_html(d.get("job_description", "")), "", ""
    return "", "", ""


# ------------------------------------------------------------------ analysis

def _fold_accents(s):
    """"México"/"Bogotá"/"São Paulo" -> "Mexico"/"Bogota"/"Sao Paulo" so plain-ASCII
    locations_exclude terms still match accented location strings from non-US ATS entries."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


_MEXICO_RE = re.compile(r"(?<!new )(?<![a-z0-9])mexico(?![a-z0-9])")

# "Dublin" collides with two real US instances (Cardinal Health's "OH-Dublin-Cardinal Place" and
# Fifth Third Bank's "Dublin, OH") among otherwise-all-Ireland hits, so it needs the same
# negative-lookaround treatment as Mexico rather than a plain locations_exclude entry.
_DUBLIN_RE = re.compile(r"(?<!oh-)(?<![a-z0-9])dublin(?![a-z0-9])(?!, oh\b)")

# Same story for "Athens": bare "Athens" (PwC's Greece office listings) vs. Gopuff's "Athens, OH"/
# "Athens, GA" (real US college towns) - the state suffix is what disambiguates, not the word itself.
_ATHENS_RE = re.compile(r"(?<![a-z0-9])athens(?![a-z0-9])(?!, oh\b)(?!, ga\b)")

# Bare "Vancouver" (Workday shows just the city) is Canadian; Vancouver, WA is a real US city.
_VANCOUVER_RE = re.compile(r"(?<![a-z0-9])vancouver(?![a-z0-9])(?!, wa\b)(?!, washington\b)")

# FIS (and similar Workday tenants) write locations as "<ISO country code> <site code> ...", e.g.
# "IND HYDB 18-23 OB3"; US sites start with "US", so only non-US country codes are listed here.
# Saxo Bank's Copenhagen roles carry the bare Workday location "Headquarters"; as a locations_exclude term it also
# dropped US sites like "US-NJ-Princeton-100-Headquarters" and "Office - USA - CA - Headquarters", so only the
# whole-string form is excluded.
_BARE_HQ_RE = re.compile(r"^\s*headquarters\s*$")

# Canadian Ontario, but not Ontario, California / Oregon / New York / Ohio.
_ONTARIO_RE = re.compile(r"\bontario\b(?!,?\s*(?:ca|california|or|oregon|ny|oh|ohio)\b)")

# Lever-style boards (Tala) give a bare country code as the whole location ("PH", "MX"). Only codes that are not US
# state or territory abbreviations are listed, so "IN", "DE", "CA", "ID", "AR" never match.
_BARE_NON_US_CODE_RE = re.compile(
    r"^\s*(?:ph|mx|uk|gb|sg|hk|jp|kr|cn|br|pl|au|my|th|vn|tw|nz|ng|ke|pk|bd|lk|ae|il|ch|se|dk|fi|pt|ro|bg|cz|hu|ua|tr|cl)\s*$"
)

_COUNTRY_CODE_SITE_RE = re.compile(
    r"^(ind|esp|pol|phl|cyp|hgk|can|gbr|irl|deu|fra|ita|nld|sgp|jpn|aus|bra|mex|chn|zaf|are|isr|che|swe|nor|dnk"
    r"|fin|prt|rou|bgr|cze|hun|ltu|lva|est|ukr|tur|arg|chl|col|per|pak|bgd|lka|mys|tha|vnm|idn|kor|twn|nzl|egy"
    r"|mar|ken|nga|sau|qat|kwt) [a-z]{4}(?![a-z])"
)

# Generic non-US shapes so each new overseas Workday site stops needing its own exclude term
# (candidates are US-only). "<ISO3> - City" / "IND-BLR-Site", "CA-<province>-" and "DE/FR/...-<state>-"
# triples, Canadian province codes and Canadian postal codes. Raw (un-lowered) location for postal.
_ISO3_DASH_RE = re.compile(
    r"^(ind|esp|pol|phl|cyp|hgk|can|gbr|irl|deu|fra|ita|nld|sgp|jpn|aus|bra|mex|chn|zaf|isr|che|swe|dnk"
    r"|fin|prt|rou|bgr|cze|hun|ltu|lva|ukr|tur|arg|chl|pak|bgd|lka|mys|tha|vnm|idn|kor|twn|nzl|egy|ken|nga"
    r"|sau|qat|kwt)\s*-\s*[a-z]"
)
_ISO2_SUBDIV_RE = re.compile(
    r"^(?:ca-(?:on|bc|qc|ab|ns|nb|mb|sk|nl|pe|yt|nt|nu)-[a-z]|(?:de|fr|it|nl|es|au|jp|cn|br|mx|ie|sg|se|pl)-[a-z]{2,3}-[a-z])"
)
_CA_POSTAL_RE = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d\b")
_CA_PROVINCE_CODE_RE = re.compile(r",\s*(?:on|bc|qc|ab|ns|nb|mb|sk|nl|pe)\s*(?:,|$)", re.I)


def matches(job, f):
    # Title include/exclude use word-boundary matching (keyword_matching.contains_term) so a short
    # term like "intern" doesn't false-positive inside "Internal"/"International". locations_include
    # stays plain substring: filters like ", nj" rely on punctuation that word-boundary matching
    # would reject (the comma has no alnum neighbor to anchor against). locations_exclude uses
    # word-boundary matching instead, on an accent-folded location string, so a bare country name
    # matches regardless of where it sits ("Mexico" / "Argentina - Buenos Aires" / "Ciudad de
    # Mexico, Mexico") while still not false-matching inside a longer word ("Indianapolis" for
    # "india"). "Mexico" gets its own regex with a negative lookbehind for "new " since "New
    # Mexico" is a real US state name containing "Mexico" as its own bounded word - no amount of
    # word-boundary anchoring alone can tell those apart. "Georgia" (also a real US state name) and
    # "Jersey" (a Channel Island, also a real US state's shorthand) are deliberately left out of
    # locations_exclude for the same reason and aren't worth the collision risk.
    t, loc = " ".join(job["title"].split()), job["location"].lower()  # collapse double spaces so excludes match
    loc_folded = _fold_accents(loc)
    inc = f.get("title_include", [])
    exc = f.get("title_exclude", [])
    locs = [x.lower() for x in f.get("locations_include", [])]
    loc_exc = f.get("locations_exclude", [])
    if inc and not any(contains_term(t, x) for x in inc):
        return False
    if any(contains_term(t, x) for x in exc):
        return False
    if _BARE_HQ_RE.match(loc_folded):
        return False
    if _BARE_NON_US_CODE_RE.match(loc_folded):
        return False
    if _ONTARIO_RE.search(loc_folded):
        return False
    if _MEXICO_RE.search(loc_folded):
        return False
    if _DUBLIN_RE.search(loc_folded):
        return False
    if _ATHENS_RE.search(loc_folded):
        return False
    if _VANCOUVER_RE.search(loc_folded):
        return False
    if _COUNTRY_CODE_SITE_RE.match(loc_folded):
        return False
    if (_ISO3_DASH_RE.match(loc_folded) or _ISO2_SUBDIV_RE.match(loc_folded)
            or _CA_POSTAL_RE.search(job["location"]) or _CA_PROVINCE_CODE_RE.search(job["location"])):
        return False
    if locs and not any(x in loc for x in locs):
        return False
    if any(contains_term(loc_folded, x) for x in loc_exc):
        return False
    return True


def classify_sponsorship(text):
    """Blocked on language that rules out OPT too (citizenship, clearance, permanent authorization, OPT
    exclusion), "OPT Only" on plain no-sponsorship language, else Unclear. Never Allowed."""
    if not text:
        return "Unclear", "Description unavailable; not checked."
    normalized = _normalize_abbreviations(text)  # same length as `text` - offsets stay valid

    def evidence(m):
        s, e = max(0, m.start() - 50), min(len(text), m.end() + 50)
        return "..." + text[s:e].strip() + "..."
    for rx in HARD_BLOCK_RES:
        m = rx.search(normalized)
        if m:
            return "Blocked", evidence(m)
    for rx in BLOCK_RES:
        m = rx.search(normalized)
        if m:
            window = normalized[max(0, m.start() - OPT_WINDOW_CHARS):m.end() + OPT_WINDOW_CHARS]
            return ("Blocked" if _OPT_WORD_RE.search(window) else "OPT Only"), evidence(m)
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
    """Best-effort years-of-experience, keyed off the LOWER bound stated (e.g. "5+ years" or "3-5
    years" both key off 3/5, not an average or the upper end) since that's the number that actually
    gates whether someone can apply. Returns (years, evidence): years is an exact integer whenever
    the description states one, or a representative anchor (0 for entry-level phrasing, 3/6 for a
    title-inferred guess) when it doesn't. Explicit numbers in the description win over title words,
    since a title alone ("Analyst") says less than a description that actually states "3-5 years
    required". No signal at all is (None, ...) - Unclear, not assumed entry-level - same "never
    guess beyond the evidence" rule as sponsorship classification."""
    hay = text or ""
    m = _ENTRY_PHRASE_RE.search(hay)
    if m:
        s, e = max(0, m.start() - 40), min(len(hay), m.end() + 40)
        return 0, hay[s:e].strip()
    m = _EXPERIENCE_YEARS_RE.search(hay)
    if m:
        min_years = min(int(g) for g in m.groups() if g)
        s, e = max(0, m.start() - 40), min(len(hay), m.end() + 40)
        return min_years, hay[s:e].strip()
    if _SENIOR_TITLE_RE.search(title or ""):
        return 6, f"inferred from title: {title}"
    if _MID_TITLE_RE.search(title or ""):
        return 3, f"inferred from title: {title}"
    return None, "No years-of-experience language found."


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


def _classify_ghosts(queue):
    """Flags evergreen/repeat-posting ("ghost") lineages: a role classified as "Likely" here has
    appeared GHOST_MIN_COUNT+ times under the same company+fingerprint, spanning GHOST_MIN_SPAN_DAYS+
    days, with each appearance starting only after the previous one went quiet (serial, not two
    genuinely concurrent openings for a common title). Deliberately never asserts the opposite - a
    role with no repost history yet is "Unclear", not "Clear", the same caution classify_sponsorship
    uses, since a role just hasn't had the chance to repeat yet doesn't mean it never will.

    This is unwindowed by design (checks the WHOLE queue, not just entries linked via repost_of/
    REPOST_WINDOW_DAYS) and recomputed queue-wide on every run, not classified once at creation like
    sponsorship - the signal only exists at the lineage level, and a chain that REPOST_WINDOW_DAYS
    fails to link (a very long gap between reposts) should still be visible here. It only ever
    tags entries red for review; nothing gets hidden or removed based on this."""
    groups = {}
    for e in queue:
        key = (e["company"], role_fingerprint(e["role"], e["location"]))
        groups.setdefault(key, []).append(e)

    for entries in groups.values():
        for e in entries:
            e["ghost_status"], e["ghost_evidence"] = "Unclear", ""
        if len(entries) < GHOST_MIN_COUNT:
            continue
        entries.sort(key=lambda e: e.get("first_seen") or "")
        serial = True
        for prev, cur in zip(entries, entries[1:]):
            boundary = prev.get("closed_on") or prev.get("last_seen_live") or prev.get("first_seen") or ""
            if not boundary or (cur.get("first_seen") or "") < boundary:
                serial = False
                break
        if not serial:
            continue
        first_seen, last_seen = entries[0].get("first_seen") or "", entries[-1].get("first_seen") or ""
        try:
            span = (date.fromisoformat(last_seen) - date.fromisoformat(first_seen)).days
        except ValueError:
            continue
        if span < GHOST_MIN_SPAN_DAYS:
            continue
        evidence = (f"posted {len(entries)}x since {first_seen} (span {span}d), "
                    f"most recently {last_seen}")
        for e in entries:
            e["ghost_status"], e["ghost_evidence"] = "Likely", evidence


def _reverify_recent_posted(queue, cfg):
    """Re-checks posted/posted_source once for each Workday entry first_seen between
    REVERIFY_MIN_AGE_DAYS and REVERIFY_MAX_AGE_DAYS ago - startDate can drift by a day depending on
    exactly when it's queried relative to Workday's own day boundary (see _workday_detail_posted),
    so a value captured right at first sighting isn't fully trustworthy until re-observed a day or
    two later. Marks every eligible entry posted_reverified=True whether or not the value actually
    changed, so each entry is only ever re-checked once - not every run for its whole eligible
    window, which would multiply this into dozens of redundant refetches per entry."""
    by_company = {c["name"]: c for c in cfg["companies"]}
    today = date.today()
    lo = (today - timedelta(days=REVERIFY_MAX_AGE_DAYS)).isoformat()
    hi = (today - timedelta(days=REVERIFY_MIN_AGE_DAYS)).isoformat()
    changed = checked = 0
    for e in queue:
        if e.get("posted_reverified") or e.get("closed_on"):
            continue
        first_seen = e.get("first_seen") or ""
        if not (lo <= first_seen <= hi):
            continue
        c = by_company.get(e["company"])
        if not c or c.get("ats") != "workday":
            continue
        job_id = e["id"].split(":", 1)[1]
        site = e.get("site") or c["site"]
        if isinstance(site, list):
            site = site[0]  # pre-existing entry with no stored site - best-effort on a multi-site company
        checked += 1
        try:
            posted, posted_source = _workday_refresh_posted(c, job_id, site)
        except Exception:
            continue  # likely closed since first_seen - the separate liveness check handles that
        finally:
            time.sleep(0.3)  # Workday 429s on bursts - see describe_pool's own comment in run()
        e["posted_reverified"] = True
        if posted and posted != e.get("posted"):
            e["posted"], e["posted_source"] = posted, posted_source
            changed += 1
    if checked:
        print(f"reverify: checked {checked} recent Workday postings, corrected {changed}")
    return changed


def _retry_enrichment(queue, cfg, keywords, dry=False):
    """Retries what failed at intake, a few per run and at most once a day per entry: (1) description fetches
    that errored, which left sponsorship/experience/salary unchecked, and (2) undated Workday entries,
    which fall outside the one-shot 1-to-3-day date recheck above. Success reclassifies the entry."""
    by_company = {c["name"]: c for c in cfg["companies"]}
    today = date.today().isoformat()
    budget, fixed = ENRICH_RETRY_BUDGET, 0
    for e in queue:
        if budget <= 0:
            break
        if e.get("closed_on") or e.get("enrich_next", "") > today or e.get("enrich_attempts", 0) >= ENRICH_MAX_ATTEMPTS:
            continue
        c = by_company.get(e["company"])
        if not c:
            continue
        desc_failed = e.get("description_status") == "failed"
        undated_workday = c.get("ats") == "workday" and not e.get("posted")
        if not (desc_failed or undated_workday):
            continue
        budget -= 1
        e["enrich_attempts"] = e.get("enrich_attempts", 0) + 1
        e["enrich_next"] = (date.today() + timedelta(days=1)).isoformat()
        job = dict(id=e["id"].split(":", 1)[1], url=e["link"], site=e.get("site"), title=e["role"], location=e["location"])
        try:
            text, posted, posted_source = describe(c, job)
        except Exception:  # noqa: BLE001  still failing: try again tomorrow, up to the attempt limit
            continue
        finally:
            if c.get("ats") == "workday":
                time.sleep(0.3)  # Workday 429s on bursts
        if posted and not e.get("posted"):
            e["posted"], e["posted_source"] = posted, posted_source
            fixed += 1
        if desc_failed and text:
            e["sponsorship_status"], e["sponsorship_evidence"] = classify_sponsorship(text)
            e["experience"], e["experience_evidence"] = classify_experience(e["role"], text)
            e["salary"], e["salary_evidence"] = classify_salary(text)
            e["kw_hits"] = keyword_hits(e["role"], text, keywords)
            e["description_status"] = "ok"
            if e["state"] == "new" and e["sponsorship_status"] == "Blocked":
                e["state"] = "auto_blocked"
            elif e["state"] == "new" and e["sponsorship_status"] == "OPT Only":
                e["state"] = "opt_only"
            fixed += 1
    if fixed:
        print(f"retry: recovered {fixed} descriptions or posted dates")
    return fixed


# --------------------------------------------------------------------- notify

def notify(cfg, entry, dry):
    """Returns True when the alert was delivered (or printed), False when it failed or was skipped."""
    line = (f"{entry['company']}: {entry['role']} ({entry['location'] or 'n/a'}) | "
            f"sponsorship {entry['sponsorship_status']}"
            + (f" | {', '.join(entry['kw_hits'][:5])}" if entry["kw_hits"] else ""))
    kind = cfg.get("type", "stdout")
    if dry or kind == "stdout":
        print(("[dry-run] " if dry else "[alert] ") + line + "\n    " + entry["link"])
        return True
    topic = cfg.get("topic") or os.environ.get("NTFY_TOPIC")
    if kind == "ntfy" and not topic:
        print(f"! notify skipped: no ntfy topic (set NTFY_TOPIC env var or cfg.notify.topic): {line}", file=sys.stderr)
        return False
    try:
        if kind == "ntfy":
            server = cfg.get("server", "https://ntfy.sh").rstrip("/")
            tracker_url = cfg.get("tracker_url")
            hdr = {"Title": f"New job: {entry['company']}".encode("ascii", "ignore").decode(), "Click": entry["link"]}
            if tracker_url:  # secondary button - keeps the main tap on the job link, adds a way into the full queue
                hdr["Actions"] = f"view, Open Jobs Tracker, {tracker_url}"
            http(f"{server}/{topic}", data=line.encode(), headers=hdr, raw=True)
        elif kind == "telegram":
            http(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                 data={"chat_id": cfg["chat_id"], "text": f"{line}\n{entry['link']}"})
    except Exception as e:  # an alert failure must not lose the queue entry
        print(f"! notify failed: {e}", file=sys.stderr)
        return False
    return True


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
        updated_at = (datetime.fromtimestamp((HOME / "queue.json").stat().st_mtime, tz=ZoneInfo("America/New_York"))
                      if (HOME / "queue.json").exists() else None)
        # public page: never expose applied/shortlisted/dismissed/blocked status; opt_only is a property of the posting, not a decision
        q = [e for e in q if e["state"] in ("new", "opt_only")]
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
    # "~" flags a date derived from Workday's relative "Posted N Days Ago" text rather than an
    # exact timestamp (see describe()/_workday_detail_posted's posted_source) - it's not wrong,
    # but it's a day-count bucket, not a real recorded date, so it shouldn't look as certain as one.
    prefix = "~" if e.get("posted_source") == "approx" else ""
    if posted == date.today().isoformat():
        return prefix + "today"
    if posted:
        return prefix + posted
    first_seen = e.get("first_seen") or ""
    return f"seen {first_seen[5:]}" if first_seen else "unknown"  # MM-DD, no real posted date to trust


# states shown collapsed below the main "new" table, in this order; any other state found (e.g.
# a custom state from a hand-edit) is appended after these
DATE_FILTER_DAYS = [0, 1, 3, 7, 14, 30, 45, 60, 90, 180, 365, 730]
_SECONDARY_STATE_ORDER = ["shortlisted", "applied", "opt_only", "auto_blocked", "dismissed"]
_STATE_LABELS = {"opt_only": "OPT only (no employer sponsorship stated)", "auto_blocked": "auto_blocked (citizenship, clearance or OPT excluded)"}


# Cloudflare Worker that returns a posting's full JD text (see worker/README.md). Blank = the Copy JD
# button reports "not configured" instead of failing silently.
JD_PROXY_URL = "https://job-scout-jd-proxy.yazad-jobscout.workers.dev"

ACTIONS_TD = '<td class="act"><button type="button" class="skip-btn copy-act">Copy JD</button></td>'

HIDE_COMPANY = "JPMorgan Chase"

# Title-only, word-boundary matched in the page script; unticked = no restriction.
ROLE_CATEGORIES = {
    "Treasury/ALM/Liquidity": [
        "treasury", "alm", "asset liability management", "asset/liability management",
        "asset-liability", "liquidity risk", "liquidity management", "lcr", "nsfr", "liquidity",
    ],
    "FP&A": ["fp&a", "fp & a", "financial planning and analysis", "financial planning & analysis"],
    "Credit Risk": [
        "credit risk", "credit analyst", "credit analysis", "counterparty credit risk",
        "credit portfolio",
    ],
    "Fixed Income": ["fixed income"],
    "Operational Risk": [
        "operational risk", "business risk", "internal controls", "enterprise risk",
        "risk governance", "governance risk",
    ],
    "Quantitative Risk/Analytics": [
        "quantitative risk", "quantitative analyst", "quant analyst", "quantitative analytics",
    ],
    "Capital Planning": [
        "capital planning", "resolution planning", "ccar", "icaap", "basel", "rwa",
        "pillar 3", "capital adequacy", "stress testing", "regulatory reporting",
    ],
    "Model Risk/Validation": ["model risk", "model validation", "model risk management"],
    "Market Risk": ["market risk", "value at risk", "var", "trading risk"],
    "Capital Markets/IB": ["capital markets", "investment banking", "equity capital markets"],
    "Risk & Controls (General)": [
        "risk management", "risk compliance", "risk controls", "risk control", "security risk",
        "information security", "third party risk", "third-party risk", "technology risk",
        "financial risk",
    ],
    "Portfolio/Asset Management": [
        "portfolio management", "asset management", "investment management",
        "portfolio analytics", "investment operations",
    ],
    "Underwriting": ["underwriting"],
    "Real Estate Finance": ["real estate", "commercial real estate", "real assets"],
    "Private Equity/Credit": ["private equity", "private credit", "private capital", "capital advisory"],
    "Wealth Management": ["wealth management", "registered client", "private banking"],
    "Equity Research": ["equity research"],
    "Corporate Finance/Development": ["corporate finance", "corporate development"],
}

_EXTRA_SCRIPT = r"""
<script>
(function() {
  var CATEGORIES = __CATEGORIES__;
  var COMPANY = __COMPANY__;
  var JD_COMPANIES = __JD_COMPANIES__;
  var bar = document.getElementById('filterBar');
  var rows = Array.prototype.slice.call(document.querySelectorAll('table tbody tr'));
  var HIDE = ['hidden-by-filter', 'hidden-by-category', 'hidden-by-company', 'hidden-by-location'];
  var COL_COMPANY = 'company', COL_ROLE = 'role', COL_LOCATION = 'location';
  // Cell lookup by header text, never by position: tables differ (the dedup page adds columns).
  function cellOf(tr, name) {
    var table = tr.closest('table');
    if (!table) return null;
    if (!table._cols) {
      table._cols = {};
      Array.prototype.forEach.call(table.querySelectorAll('thead th'), function(th, i) {
        table._cols[th.textContent.trim().toLowerCase()] = i;
      });
    }
    var i = table._cols[name];
    return i === undefined ? null : (tr.cells[i] || null);
  }
  function textOf(tr, name) { var c = cellOf(tr, name); return c ? c.textContent : ''; }

  var counters = [];
  document.querySelectorAll('h2, summary').forEach(function(el) {
    var table = el.tagName === 'SUMMARY' ? el.parentElement.querySelector('table') : el.nextElementSibling;
    if (!table || table.tagName !== 'TABLE') return;
    var m = el.textContent.match(/^(.*)\((\d+)\)$/);
    if (m) counters.push({el: el, table: table, label: m[1]});
  });
  function recount() {
    counters.forEach(function(c) {
      var n = Array.prototype.filter.call(c.table.querySelectorAll('tbody tr'), function(tr) {
        return !HIDE.some(function(k) { return tr.classList.contains(k); });
      }).length;
      c.el.textContent = c.label + '(' + n + ')';
    });
  }
  ['filterSearchBtn', 'expMin', 'expMax', 'expIncludeNA', 'expReset', 'dateMin', 'dateMax', 'dateIncludeNA'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener(el.tagName === 'BUTTON' ? 'click' : 'change', function() {
      setTimeout(function() { applyCategory(); applyCompany(); applyLocation(); }, 0);
    });
  });

  // Categories
  var wrap = document.createElement('div');
  wrap.className = 'exp-filter';
  wrap.innerHTML = 'Categories: ' + CATEGORIES.map(function(c) {
    return '<label style="margin-right: 10px; white-space: nowrap; display: inline-block;"><input type="checkbox" data-cat-key="' + c.key + '"> ' +
           c.label.replace(/&/g, '&amp;') + '</label>';
  }).join('');
  if (bar) bar.appendChild(wrap);
  var checkboxes = Array.prototype.slice.call(wrap.querySelectorAll('input[type=checkbox]'));
  function escapeRegex(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  CATEGORIES.forEach(function(c) {
    c.re = c.terms.length ? new RegExp('\\b(?:' + c.terms.map(function(t) { return escapeRegex(t.toLowerCase()); }).join('|') + ')\\b') : null;
  });
  rows.forEach(function(tr) {
    var role = textOf(tr, COL_ROLE).toLowerCase();
    var matched = [];
    CATEGORIES.forEach(function(c) { if (c.re && c.re.test(role)) matched.push(c.key); });
    if (!matched.length) matched.push('other');
    tr.dataset.cats = matched.join(',');
  });
  function applyCategory() {
    var ticked = checkboxes.filter(function(cb) { return cb.checked; }).map(function(cb) { return cb.dataset.catKey; });
    rows.forEach(function(tr) {
      var rc = tr.dataset.cats ? tr.dataset.cats.split(',') : [];
      tr.classList.toggle('hidden-by-category', ticked.length > 0 && !ticked.some(function(k) { return rc.indexOf(k) !== -1; }));
    });
    recount();
    try { localStorage.setItem('jobScoutCategoryTicks', JSON.stringify(ticked)); } catch (e) {}
  }
  try {
    var saved = JSON.parse(localStorage.getItem('jobScoutCategoryTicks') || '[]');
    checkboxes.forEach(function(cb) { cb.checked = saved.indexOf(cb.dataset.catKey) !== -1; });
  } catch (e) {}
  checkboxes.forEach(function(cb) { cb.addEventListener('change', applyCategory); });
  var metroBox = null;
  var resetBtn = document.getElementById('expReset');
  if (resetBtn) resetBtn.addEventListener('click', function() {
    checkboxes.forEach(function(cb) { cb.checked = false; });
    applyCategory();
    if (metroBox) { metroBox.checked = false; applyLocation(); }
  });

  // Hide one company
  var na = document.getElementById('expIncludeNA');
  var anchor = na ? na.closest('label') : null;
  var label = document.createElement('label');
  label.style.marginLeft = '10px';
  label.innerHTML = '<input type="checkbox" id="hideCompanyBox"> hide ' + COMPANY;
  if (anchor) anchor.insertAdjacentElement('afterend', label);
  else if (bar) bar.appendChild(label);
  var hideBox = label.querySelector('input');
  function applyCompany() {
    rows.forEach(function(tr) {
      var c = textOf(tr, COL_COMPANY).trim();
      tr.classList.toggle('hidden-by-company', hideBox.checked && c === COMPANY);
    });
    recount();
    try { localStorage.setItem('jobScoutHideCompany', hideBox.checked ? '1' : '0'); } catch (e) {}
  }
  try { hideBox.checked = localStorage.getItem('jobScoutHideCompany') === '1'; } catch (e) {}
  hideBox.addEventListener('change', applyCompany);

  // NY metro only: NY/NJ/CT area by location text; upstate NY cities excluded.
  var METRO_RE = /\b(new york|nyc|manhattan|brooklyn|queens|bronx|long island|westchester|white plains|new jersey|nj|jersey city|newark|hoboken|harrison|stamford|greenwich|norwalk|connecticut|ct|ny)\b/;
  var UPSTATE_RE = /\b(buffalo|albany|rochester|syracuse|ithaca|binghamton|utica)\b/;
  var metroLabel = document.createElement('label');
  metroLabel.style.marginLeft = '10px';
  metroLabel.innerHTML = '<input type="checkbox" id="metroOnlyBox"> NY metro only (NY/NJ/CT)';
  label.insertAdjacentElement('afterend', metroLabel);
  metroBox = metroLabel.querySelector('input');
  function applyLocation() {
    rows.forEach(function(tr) {
      var loc = textOf(tr, COL_LOCATION).toLowerCase();
      var ok = METRO_RE.test(loc) && !UPSTATE_RE.test(loc);
      tr.classList.toggle('hidden-by-location', metroBox.checked && !ok);
    });
    recount();
    try { localStorage.setItem('jobScoutMetroOnly', metroBox.checked ? '1' : '0'); } catch (e) {}
  }
  try { metroBox.checked = localStorage.getItem('jobScoutMetroOnly') === '1'; } catch (e) {}
  metroBox.addEventListener('change', applyLocation);
  applyCategory();
  applyCompany();
  applyLocation();

  // Copy JD: window.JD_PROXY_URL_OVERRIDE lets the local dedup page point at its own server.
  var JD_URL = window.JD_PROXY_URL_OVERRIDE || __JD_URL__;
  document.addEventListener('click', function(ev) {
    var btn = ev.target.closest ? ev.target.closest('button.copy-act') : null;
    if (!btn) return;
    var tr = btn.closest('tr');
    var td = btn.parentElement;
    var old = td.querySelector('.jd-err');
    if (old) old.remove();
    function fail(msg) {
      btn.disabled = false;
      btn.textContent = 'Copy JD';
      var e = document.createElement('span');
      e.className = 'jd-err';
      e.style.cssText = 'color:#c0392b;font-size:0.75rem;display:block';
      e.textContent = msg;
      td.appendChild(e);
    }
    if (!JD_URL) { fail('JD proxy not configured'); return; }
    var roleCell = cellOf(tr, COL_ROLE);
    var roleLink = roleCell ? roleCell.querySelector('a') : null;
    var company = textOf(tr, COL_COMPANY).trim();
    btn.disabled = true;
    btn.textContent = 'Fetching...';
    fetch(JD_URL, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        company: company,
        role: (roleLink ? roleLink.textContent : textOf(tr, COL_ROLE)).trim(),
        link: roleLink ? roleLink.href : '',
        location: textOf(tr, COL_LOCATION).trim(),
        jid: tr.getAttribute('data-jid') || '',
        cfg: JD_COMPANIES[company] || null
      })
    }).then(function(resp) {
      if (!resp.ok) throw new Error('server returned ' + resp.status);
      return resp.json();
    }).then(function(data) {
      if (!data.ok) throw new Error(data.stderr || 'request failed');
      return navigator.clipboard.writeText(data.text).then(function() {
        btn.textContent = 'Copied';
        setTimeout(function() { btn.textContent = 'Copy JD'; btn.disabled = false; }, 2000);
      });
    }).catch(function(err) { fail('Failed: ' + err.message); });
  });
})();
</script>
<style>
tr.hidden-by-category, tr.hidden-by-company, tr.hidden-by-location { display: none; }
.skip-btn { display: inline-block; width: 5.5rem; margin: 0 4px 0 0; padding: 3px 0; font-size: 0.85rem; cursor: pointer; white-space: nowrap; text-align: center; }
td.act { white-space: nowrap; }
</style>
"""


def _jd_company_map():
    """Company -> just the fields a stateless proxy needs to rebuild that ATS's detail URL."""
    out = {}
    for c in load_json(HOME / "config.json", {}).get("companies", []):
        ats = c.get("ats")
        if ats in ("greenhouse", "lever", "ashby", "smartrecruiters"):
            entry = dict(ats=ats, slug=c["slug"])
            if c.get("region"):
                entry["region"] = c["region"]
        elif ats == "workday":
            sites = c["site"] if isinstance(c["site"], list) else [c["site"]]
            entry = dict(ats=ats, host=c["host"], tenant=c["tenant"], sites=sites)
        elif ats == "oracle":
            entry = dict(ats=ats, host=c["host"])
        else:
            continue
        out[c["name"]] = entry
    return out


def _extra_script():
    cats = [dict(key=f"cat{i}", label=label, terms=terms) for i, (label, terms) in enumerate(ROLE_CATEGORIES.items())]
    cats.append(dict(key="other", label="Other (no category)", terms=[]))
    return (_EXTRA_SCRIPT
            .replace("__CATEGORIES__", json.dumps(cats))
            .replace("__COMPANY__", json.dumps(HIDE_COMPANY))
            .replace("__JD_COMPANIES__", json.dumps(_jd_company_map(), separators=(",", ":")))
            .replace("__JD_URL__", json.dumps(JD_PROXY_URL)))


def write_queue_html(q, path, updated_at=None):
    """Static page, no server: q is already sorted newest-posted-first by cmd_queue. Each row is a
    plain <a> to the live posting so double-clicking the file and clicking a link is the whole workflow.
    "new" postings get their own table up top; every other open state (shortlisted, applied, ...)
    collapses into a <details> section; closed/removed entries (any state) go in one final section so
    a growing history doesn't bury what's actionable today."""
    def row_html(e):
        posted = e.get("posted") or ""
        closed = bool(e.get("closed_on"))
        # No relative label ("today", "seen MM-DD") is baked in here: this is a static page with
        # no server, generated once per scheduled run, so any relative-to-now text written at
        # generation time goes stale the moment the viewer's clock moves past that instant (e.g.
        # a posting stays labeled "today" forever if viewed the next day off a page that hasn't
        # regenerated). The raw ISO date (or the id-only fallbacks below) goes in data-posted /
        # data-first-seen instead, and the script at the bottom recomputes the relative label and
        # the "posted today" highlight against the viewer's own current date on every page load.
        kw = ", ".join(e["kw_hits"]) if e["kw_hits"] else "—"
        spons_evidence = html.escape(e.get("sponsorship_evidence") or "no blocking language found")
        exp_years = e.get("experience")
        exp_label = f"{exp_years}+ years" if isinstance(exp_years, int) else "N/A"
        exp_evidence = html.escape(e.get("experience_evidence") or "")
        salary = e.get("salary") or "Unclear"
        salary_evidence = html.escape(e.get("salary_evidence") or "")
        spons_label = e["sponsorship_status"] if e["sponsorship_status"] != "Unclear" else "N/A"
        salary_label = salary if salary != "Unclear" else "N/A"
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
        exp_data = f' data-exp="{exp_years}"' if isinstance(exp_years, int) else ' data-exp="-1"'
        first_seen = e.get("first_seen") or ""
        # Same "never assert the positive, only flag with evidence" convention as sponsorship: a
        # role that hasn't shown a repeat-posting pattern is N/A, not "clear" of it, since it just
        # hasn't had the chance to repeat yet. Tagged red (see tr.ghost in the CSS) but never
        # hidden or removed from its normal table/section - still fully visible and actionable.
        is_ghost = e.get("ghost_status") == "Likely"
        ghost_label = "Likely ghost" if is_ghost else "N/A"
        ghost_evidence = html.escape(e.get("ghost_evidence") or "no repeat-posting pattern detected")
        row_class = e["state"] + (" closed" if closed else "") + (" ghost" if is_ghost else "")
        if e.get("posted_source") == "approx":
            posted_title = "approximate - derived from Workday's relative posting-age text, not an exact timestamp"
        elif posted and e.get("posted_source") == "exact":
            posted_title = "exact date from the source"
        else:
            posted_title = "no posting date available from the source - this is when job-scout first discovered it, not when it was actually posted"
        return (
            f'<tr class="{row_class}"{row_title}{exp_data} data-jid="{html.escape(e["id"].split(":", 1)[-1])}"'
            f' data-posted="{html.escape(posted)}" data-first-seen="{html.escape(first_seen)}"'
            f' data-posted-source="{html.escape(e.get("posted_source") or "")}">'
            f'<td class="posted" title="{html.escape(posted_title)}">{html.escape(posted_label(e))}</td>'
            f'<td title="{ghost_evidence}" class="ghost-cell">{html.escape(ghost_label)}</td>'
            f'<td>{html.escape(e["company"])}</td>'
            f'<td>{role_cell}</td>'
            f'{"" if closed else ACTIONS_TD}'
            f'<td>{html.escape(e["location"])}</td>'
            f'<td title="{spons_evidence}">{html.escape(spons_label)}</td>'
            f'<td title="{exp_evidence}">{html.escape(exp_label)}</td>'
            f'<td title="{salary_evidence}" class="salary">{html.escape(salary_label)}</td>'
            f'<td class="kw">{html.escape(kw)}</td>'
            f'{state_cell}'
            f'</tr>'
        )

    head = ("<tr><th>Posted</th><th>Ghost</th><th>Company</th><th>Role</th><th>Actions</th><th>Location</th>"
            "<th>Sponsorship</th><th>Experience</th><th>Salary</th><th>Keywords</th></tr>")
    closed_head = head.replace("<th>Actions</th>", "").replace("</tr>", "<th>State</th></tr>")

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
        f'<details><summary>{html.escape(_STATE_LABELS.get(state, state))} ({len(by_state[state])})</summary>'
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

    updated_label = updated_at.strftime("%Y-%m-%d %H:%M %Z") if updated_at else "unknown"
    updated_iso = updated_at.isoformat(timespec="seconds") if updated_at else ""

    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Jobs Tracker</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafafa; color: #111; }}
h1 {{ font-size: 1.2rem; }}
.meta {{ color: #666; font-size: 0.85rem; margin-top: -0.5rem; margin-bottom: 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #ddd; text-align: left; font-size: 0.9rem; }}
th {{ position: sticky; top: 0; background: #fafafa; }}
tr.today {{ background: #eaffea; font-weight: 600; }}
tr.ghost {{ border-left: 4px solid #c0392b; }}
tr.ghost td.ghost-cell {{ color: #c0392b; font-weight: 700; }}
tr.closed {{ color: #999; }}
tr.closed a, tr.closed {{ text-decoration: line-through; }}
td.posted {{ white-space: nowrap; }}
.approx-marker {{ font-weight: 700; color: #b45309; }}
td.ghost-cell {{ white-space: nowrap; color: #999; }}
td.kw {{ color: #555; font-size: 0.85rem; }}
td.salary {{ white-space: nowrap; }}
span.repost {{ color: #b45309; font-weight: 600; font-size: 0.8rem; text-decoration: none; }}
details {{ margin-bottom: 0.5rem; }}
summary {{ cursor: pointer; font-weight: 600; padding: 4px 0; }}
#filterBar {{ position: sticky; top: 0; background: #fafafa; padding: 0.5rem 0; margin-bottom: 0.5rem; z-index: 1; }}
#filterBox {{ width: 100%; max-width: 420px; padding: 8px 10px; font-size: 1rem; box-sizing: border-box; }}
#filterSearchBtn {{ padding: 8px 14px; font-size: 1rem; margin-left: 6px; cursor: pointer; }}
#filterCount {{ color: #666; font-size: 0.85rem; margin-left: 8px; }}
tr.hidden-by-filter {{ display: none; }}
.exp-filter {{ margin-top: 6px; font-size: 0.85rem; color: #333; }}
.exp-filter select {{ padding: 3px 6px; font-size: 0.85rem; margin: 0 4px; }}
</style></head>
<body>
<h1>Job Scout Queue - {len(open_q)} open, {len(closed_q)} closed</h1>
<p class="meta">Data last updated {updated_label} &middot; <span class="approx-marker">~</span> before a Posted date means approximate (day-count bucket, not an exact timestamp) - hover any date for details</p>
<div id="stale-banner" hidden style="background:#b00020;color:#fff;padding:10px 14px;border-radius:6px;margin:8px 0;font-weight:600"></div>
<script>
(function() {{
  var updated = new Date("{updated_iso}");
  var hours = (Date.now() - updated.getTime()) / 3600000;
  if (!isNaN(hours) && hours > {STALE_ALERT_HOURS}) {{
    var b = document.getElementById("stale-banner");
    b.textContent = "Scout has not updated for " + Math.floor(hours) + " hours (last run " + "{updated_label}" + "). Press Ctrl+F5 first (this may be a cached copy); if it persists the workflow may be stuck, check GitHub Actions.";
    b.hidden = false;
  }}
}})();
</script>
<div id="filterBar">
<input type="search" id="filterBox" placeholder="Filter: comma = OR, space = AND (e.g. python, sql bloomberg)" autocomplete="off">
<button type="button" id="filterSearchBtn">Search</button>
<span id="filterCount"></span>
<div class="exp-filter">
Experience (years): from
<select id="expMin">
{''.join(f'<option value="{n}"{" selected" if n == 0 else ""}>{n}</option>' for n in range(16))}
</select>
to
<select id="expMax">
{''.join(f'<option value="{n}">{n}</option>' for n in range(16))}
<option value="inf" selected>15+</option>
</select>
<label><input type="checkbox" id="expIncludeNA" checked> include N/A (no years-of-experience found)</label>
<br>Posted (days ago): from
<select id="dateMin">
{''.join(f'<option value="{n}"{" selected" if n == 0 else ""}>{n}</option>' for n in DATE_FILTER_DAYS)}
</select>
to
<select id="dateMax">
{''.join(f'<option value="{n}">{n}</option>' for n in DATE_FILTER_DAYS)}
<option value="inf" selected>any</option>
</select>
<label><input type="checkbox" id="dateIncludeNA" checked> include undated</label>
<button type="button" id="expReset">reset all filters</button>
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
  // Client-side filter, no server: comma-separated groups are OR'd, terms within a group
  // are AND'd (e.g. "python, sql bloomberg" = python OR (sql AND bloomberg)). Lets one shared
  // page/URL serve different people with different interests - each viewer just types their own
  // terms; nothing is sent anywhere and nothing is saved except this browser's own last search.
  // The text box only re-filters on Search-click/Enter, not per keystroke - with several thousand
  // rows, re-scanning tr.textContent on every character typed was visibly laggy. The exp/N-A
  // controls stay live since a dropdown change is a single cheap event, not one per keystroke.
  var box = document.getElementById('filterBox');
  var searchBtn = document.getElementById('filterSearchBtn');
  var countEl = document.getElementById('filterCount');
  var expMin = document.getElementById('expMin');
  var expMax = document.getElementById('expMax');
  var expIncludeNA = document.getElementById('expIncludeNA');
  var expReset = document.getElementById('expReset');
  var dateMin = document.getElementById('dateMin');
  var dateMax = document.getElementById('dateMax');
  var dateIncludeNA = document.getElementById('dateIncludeNA');
  var rows = Array.prototype.slice.call(document.querySelectorAll('table tbody tr'));
  var forcedOpen = [];

  function ageDays(tr) {{
    var iso = tr.getAttribute('data-posted') || tr.getAttribute('data-first-seen');
    if (!iso) return null;
    var now = new Date(), t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((t0 - new Date(iso + 'T00:00:00')) / 86400000);
  }}
  function dateActive() {{ return dateMin.value !== '0' || (dateMax.value !== 'inf' && !window.__dateIgnoreMax); }}
  function dateOk(tr) {{
    if (!dateActive()) return true;
    var a = ageDays(tr);
    if (a === null) return dateIncludeNA.checked;
    var lo = parseInt(dateMin.value, 10);
    var hi = (dateMax.value === 'inf' || window.__dateIgnoreMax) ? Infinity : parseInt(dateMax.value, 10);
    return a >= lo && a <= hi;
  }}

  function apply() {{
    var raw = box.value.trim().toLowerCase();
    var lo = parseInt(expMin.value, 10);
    var hi = expMax.value === 'inf' ? Infinity : parseInt(expMax.value, 10);
    forcedOpen.forEach(function(d) {{ d.removeAttribute('open'); }});
    forcedOpen = [];
    var groups = raw ? raw.split(',').map(function(g) {{ return g.trim().split(/\\s+/).filter(Boolean); }})
                          .filter(function(g) {{ return g.length; }}) : null;
    var expActive = !(lo === 0 && hi === Infinity);
    var shown = 0;
    rows.forEach(function(tr) {{
      var textMatch = true;
      if (groups) {{
        var text = tr.textContent.toLowerCase();
        textMatch = groups.some(function(terms) {{ return terms.every(function(t) {{ return text.indexOf(t) !== -1; }}); }});
      }}
      var expMatch = true;
      if (expActive) {{
        var rank = parseInt(tr.getAttribute('data-exp'), 10);
        // -1 means "no years-of-experience language found" - never silently drop those from a
        // numeric range filter (they didn't fail the check, they just have nothing to check).
        expMatch = rank === -1 ? expIncludeNA.checked : (rank >= lo && rank <= hi);
      }}
      var match = textMatch && expMatch && dateOk(tr);
      tr.classList.toggle('hidden-by-filter', !match);
      if (match) {{
        shown++;
        var details = tr.closest('details');
        if (details && !details.open) {{ details.open = true; forcedOpen.push(details); }}
      }}
    }});
    countEl.textContent = (raw || expActive || dateActive()) ? ('showing ' + shown + ' of ' + rows.length) : '';
    try {{
      localStorage.setItem('jobScoutFilter', box.value);
      localStorage.setItem('jobScoutExpMin', expMin.value);
      localStorage.setItem('jobScoutExpMax', expMax.value);
      localStorage.setItem('jobScoutExpIncludeNA', expIncludeNA.checked ? '1' : '0');
      localStorage.setItem('jobScoutDateMin', dateMin.value);
      localStorage.setItem('jobScoutDateMax', dateMax.value);
      localStorage.setItem('jobScoutDateNA', dateIncludeNA.checked ? '1' : '0');
    }} catch (e) {{}}
  }}

  expReset.addEventListener('click', function() {{
    box.value = '';
    expMin.value = '0';
    expMax.value = 'inf';
    expIncludeNA.checked = true;
    dateMin.value = '0';
    dateMax.value = 'inf';
    dateIncludeNA.checked = true;
    apply();
  }});

  try {{
    var saved = localStorage.getItem('jobScoutFilter');
    if (saved) box.value = saved;
    var savedMin = localStorage.getItem('jobScoutExpMin');
    var savedMax = localStorage.getItem('jobScoutExpMax');
    var savedNA = localStorage.getItem('jobScoutExpIncludeNA');
    if (savedMin !== null) expMin.value = savedMin;
    if (savedMax !== null) expMax.value = savedMax;
    if (savedNA !== null) expIncludeNA.checked = savedNA === '1';
    var sdMin = localStorage.getItem('jobScoutDateMin');
    var sdMax = localStorage.getItem('jobScoutDateMax');
    var sdNA = localStorage.getItem('jobScoutDateNA');
    if (sdMin !== null) dateMin.value = sdMin;
    if (sdMax !== null) dateMax.value = sdMax;
    if (sdNA !== null) dateIncludeNA.checked = sdNA === '1';
  }} catch (e) {{}}
  searchBtn.addEventListener('click', apply);
  box.addEventListener('keydown', function(e) {{ if (e.key === 'Enter') {{ e.preventDefault(); apply(); }} }});
  expMin.addEventListener('change', apply);
  expMax.addEventListener('change', apply);
  expIncludeNA.addEventListener('change', apply);
  dateMin.addEventListener('change', apply);
  dateMax.addEventListener('change', apply);
  dateIncludeNA.addEventListener('change', apply);
  apply();
}})();
(function() {{
  // Recomputes "today"/"N days ago"/"seen MM-DD" against the viewer's own current date, every
  // page load - the server only ever writes the raw ISO date into data-posted/data-first-seen,
  // never a relative label, so this never goes stale even if the page itself wasn't regenerated
  // since yesterday.
  function daysAgo(iso) {{
    var d = new Date(iso + 'T00:00:00');
    var now = new Date();
    var todayLocal = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((todayLocal - d) / 86400000);
  }}
  document.querySelectorAll('tr[data-posted]').forEach(function(tr) {{
    var posted = tr.getAttribute('data-posted');
    var cell = tr.querySelector('td.posted');
    // "~" marks a date derived from Workday's relative day-count text rather than an exact
    // timestamp (see posted_label()/posted_source server-side) - carried through here so the
    // marker survives the dynamic relabel instead of only appearing on first render.
    var prefix = tr.getAttribute('data-posted-source') === 'approx' ? '~' : '';
    var label;
    if (posted) {{
      var n = daysAgo(posted);
      label = prefix + (n <= 0 ? 'today' : n === 1 ? 'yesterday' : n + 'd ago');
      tr.classList.toggle('today', n <= 0);
    }} else {{
      var firstSeen = tr.getAttribute('data-first-seen');
      label = firstSeen ? 'seen ' + firstSeen.slice(5) : 'unknown';
      tr.classList.remove('today');
    }}
    if (cell) cell.textContent = label;
  }});
  // Row order is dynamic too, not just the label - re-sort each table body by the same date every
  // load (today, then yesterday, then further back; undated rows last) so the order self-corrects
  // exactly like the label does, instead of staying frozen at whatever order the last regeneration
  // computed it in.
  document.querySelectorAll('table tbody').forEach(function(tbody) {{
    var trs = Array.prototype.slice.call(tbody.children);
    trs.sort(function(a, b) {{
      var ka = a.getAttribute('data-posted') || a.getAttribute('data-first-seen') || '';
      var kb = b.getAttribute('data-posted') || b.getAttribute('data-first-seen') || '';
      if (ka === kb) return 0;
      if (!ka) return 1;
      if (!kb) return -1;
      return ka < kb ? 1 : -1;
    }});
    trs.forEach(function(tr) {{ tbody.appendChild(tr); }});
  }});
}})();
</script>
{_extra_script()}
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
                  closed=0, reopened=0, reposts=0, pruned=0)

    if args.from_fetched:
        fetched = load_fetched(args.from_fetched, cfg["companies"])
        if not args.dry_run:
            update_fetch_times(args.from_fetched)
    else:
        fetched, _ = fetch_all(cfg["companies"])

    # One shared pool for every description fetch across every company, not one pool per company -
    # several companies finishing their listing fetch around the same time and each spinning up
    # their own pool caused a brief concurrency spike that tripped Workday's rate limiting (429s).
    describe_pool = ThreadPoolExecutor(max_workers=8)
    floods = []  # (company, n) for companies held back by the flood guard
    tombstones = _load_tombstones() if getattr(args, "recall", False) else set()
    unverified = {c["name"] for c in cfg["companies"]}  # companies not cleanly and completely fetched this run: never prune their entries
    try:
        for c in cfg["companies"]:
            name = c["name"]
            jobs, complete, err = fetched[name]
            if err is None and complete and jobs:
                unverified.discard(name)
            if err is not None:
                counts["errors"] += 1
                print(f"! {name} ({c['ats']}): {err}", file=sys.stderr)
                continue
            counts["companies"] += 1
            jobs = list({j["id"]: j for j in jobs}.values())  # a fetcher that repeats an id must not double-queue it
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
                        text, posted, posted_source = fut.result()
                        descriptions[j["id"]] = text
                        if posted:  # refines/replaces the bulk listing's date with the detail page's exact one
                            j["posted"] = posted
                            j["posted_source"] = posted_source
                    except Exception as e:
                        print(f"! {name} description for {j['title']}: {e}", file=sys.stderr)
                        descriptions[j["id"]] = ""
                        j["desc_failed"] = True
            flood = (not first_run and len(fresh) > max(FLOOD_MIN, FLOOD_FRACTION * len(hits)))
            if flood:
                print(f"! {name}: {len(fresh)}/{len(hits)} matches are new at once (id or search-term change?), "
                      f"queueing silently with one summary alert", file=sys.stderr)
                floods.append((name, len(fresh)))
            _process_company(c, name, fresh, descriptions, first_run, jobs, complete, seen, state, queue, queued_ids,
                              keywords, cfg, args, counts, silent=flood)
            if getattr(args, "recall", False) and not first_run:
                _recall_company(c, name, hits, seen, queued_ids, tombstones, describe_pool, queue, keywords, cfg, args, counts)
            if not args.dry_run:
                _update_liveness(name, jobs, complete, queue, counts, c)
    finally:
        describe_pool.shutdown()

    if args.check:
        return
    _retry_pending_alerts(queue, cfg.get("notify", {}), counts, args.dry_run)
    _send_summaries(cfg.get("notify", {}), floods, counts, args.dry_run)
    if not args.dry_run:
        _reverify_recent_posted(queue, cfg)  # a real network pass - skip on --dry-run like everything else
        _retry_enrichment(queue, cfg, keywords)
    _classify_ghosts(queue)  # queue-wide, so it must run after every company's entries exist
    if not args.dry_run:
        _prune_stale(queue, counts, unverified, configured={c["name"] for c in cfg["companies"]})
        _write_tombstones(counts.get("pruned_ids", []), "pruned")
        _drop_location_filtered(queue, cfg.get("filters", {}), counts)
        HOME.mkdir(parents=True, exist_ok=True)
        # queue first: a crash between the writes must never leave a job marked seen without its queue row
        atomic_json.write(str(HOME / "queue.json"), queue)
        atomic_json.write(str(HOME / "state.json"), state)
    print(f"done: {counts['companies']} companies ({counts['errors']} errors), {counts['matched']} new matches, "
          f"{counts['blocked']} auto-blocked on sponsorship, {counts['queued']} queued, {counts['alerted']} alerts, "
          f"{counts['closed']} closed, {counts['reopened']} reopened, {counts['reposts']} reposts, "
          f"{counts['pruned']} pruned (posted > {PRUNE_AFTER_DAYS}d ago).")


def _drop_location_filtered(queue, filters, counts):
    """Removes still-new queue rows whose location a newer locations_exclude term (or location regex) rejects, so
    the hosted page matches the config without a hand-edit of queue.json. Location only (never the title lists),
    not tombstoned: if a term is later loosened the row simply comes back on the next fetch."""
    loc_only = {"title_include": [], "title_exclude": [], "locations_include": filters.get("locations_include", []),
                "locations_exclude": filters.get("locations_exclude", [])}
    keep = [e for e in queue if e.get("state") != "new" or matches({"title": "x", "location": e.get("location", "")}, loc_only)]
    counts["location_dropped"] = len(queue) - len(keep)
    queue[:] = keep


def _find_repost_source(queue, name, fp, live_ids, eid, claimed):
    """A prior queue entry is this job's repost source if: same company, same role fingerprint,
    its own id is no longer in the live listing (so it's not just a second concurrent opening for
    the same role), not already claimed by another fresh match this same run (two different new
    postings can share a fingerprint - the second one to process must not steal the first one's
    source), and it went quiet recently enough that the match isn't just coincidence.

    The recency check is keyed on when the prior posting was last known live (closed_on, falling
    back to last_seen_live), not on when it was first_seen - a role that stayed posted for months
    before finally closing has an old first_seen even if it just went quiet last week, and gating
    on first_seen would wrongly treat that as "too stale to be a repost". This matters most for
    exactly the long-lived/evergreen postings a repost check should be catching."""
    cutoff = (date.today() - timedelta(days=REPOST_WINDOW_DAYS)).isoformat()
    candidates = [e for e in queue if e["company"] == name and e["id"] != eid
                  and role_fingerprint(e["role"], e["location"]) == fp
                  and e["id"].split(":", 1)[1] not in live_ids
                  and e["id"] not in claimed
                  and (e.get("closed_on") or e.get("last_seen_live") or e.get("first_seen") or "") >= cutoff]
    return max(candidates, key=lambda e: e.get("first_seen", "")) if candidates else None


def _process_company(c, name, fresh, descriptions, first_run, jobs, complete, seen, state, queue, queued_ids,
                      keywords, cfg, args, counts, silent=False):
    live_ids = {jj["id"] for jj in jobs}
    today = date.today().isoformat()
    claimed = set()  # prior entries already matched to a fresh posting earlier in this same loop -
                      # without this, two different fresh postings that share a fingerprint could
                      # both claim the same prior as their repost source
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
        prior = None if (first_run or not complete) else _find_repost_source(queue, name, fp, live_ids, eid, claimed)

        entry_state = {"Blocked": "auto_blocked", "OPT Only": "opt_only"}.get(spons, "new")
        repost_count, repost_of, reposted_on = 0, None, None
        lineage_root, lineage_first_seen = eid, today
        if prior:
            claimed.add(prior["id"])
            repost_count = prior.get("repost_count", 0) + 1
            # prefer the new posting's own exact date over "the day we happened to scrape it" -
            # they only diverge when a run is missed or errors out, but when they do, the actual
            # date is the more honest one to keep permanently in the record
            repost_of, reposted_on = prior["id"], (j.get("posted") or today)
            # walk-free lineage tracking: inherit the root/earliest-date from the prior entry so a
            # long repost chain never needs to be re-walked to answer "how old is this role really"
            lineage_root = prior.get("lineage_root") or prior["id"]
            lineage_first_seen = prior.get("lineage_first_seen") or prior.get("first_seen") or today
            if spons != "Blocked" and prior.get("state") in ("dismissed", "applied", "shortlisted"):
                entry_state = prior["state"]  # a repost of a role Yazad already acted on inherits that decision
            if not prior.get("closed_on"):
                prior["closed_on"], prior["closed_reason"] = today, "reposted"

        posted = j.get("posted") or ""
        # provenance for the date, surfaced on the tracker page so a date never looks more certain
        # than it is: every non-Workday ATS returns an exact native timestamp from its list call
        # (see the per-fetcher functions above), so a posted value there is always "exact". Workday
        # sets posted_source itself via describe()/_workday_detail_posted (exact startDate vs the
        # approx relative-text fallback) - if describe() never got that far (threw, or this entry
        # skipped it), fall back to "approx" since workday()'s own bulk-listing value is always the
        # relative-text parse.
        posted_source = j.get("posted_source") or (("exact" if c["ats"] != "workday" else "approx") if posted else "")
        entry = dict(id=eid, company=name, role=j["title"], location=j["location"], link=j["url"],
                     posted=posted, posted_source=posted_source, posted_reverified=False,
                     description_status="failed" if j.get("desc_failed") else "ok",
                     site=j.get("site"),  # which Workday site this came from, for multi-site companies
                     source="job-scout", first_seen=today,
                     sponsorship_status=spons, sponsorship_evidence=evidence,
                     experience=experience, experience_evidence=experience_evidence,
                     salary=salary, salary_evidence=salary_evidence,
                     kw_hits=keyword_hits(j["title"], text, keywords),
                     state=entry_state, first_run=first_run,
                     last_seen_live=today, miss_count=0, closed_on=None, closed_reason=None,
                     repost_count=repost_count, repost_of=repost_of, reposted_on=reposted_on,
                     lineage_root=lineage_root, lineage_first_seen=lineage_first_seen,
                     ghost_status="Unclear", ghost_evidence="")  # refreshed queue-wide by _classify_ghosts()
        queue.append(entry)
        queued_ids.add(eid)
        if prior:
            counts["reposts"] += 1
        if spons == "Blocked":
            counts["blocked"] += 1
            continue
        if spons == "OPT Only":
            counts["opt_only"] = counts.get("opt_only", 0) + 1  # listed in its own section, no alert
            continue
        counts["queued"] += 1
        # first run seeds the queue silently; a repost only alerts if it flips from blocked to open -
        # otherwise it's the same decision Yazad already made, just resurfacing under a new id
        if not first_run and not silent and (not prior or prior.get("state") == "auto_blocked"):
            if _alert_cap_reached(counts):
                counts["alert_capped"] = counts.get("alert_capped", 0) + 1
            else:
                counts["alert_tries"] = counts.get("alert_tries", 0) + 1
                if notify(cfg.get("notify", {}), entry, args.dry_run):
                    counts["alerted"] += 1
                elif not args.dry_run:
                    entry["alert_pending"], entry["alert_attempts"] = True, 1  # retried next run by _retry_pending_alerts()
                    counts.setdefault("failed_now", set()).add(eid)  # not retried again within this same run
    if not args.dry_run and (jobs or not first_run):
        # a first fetch that returned nothing must not seed state: once the config is fixed, every match would alert
        state[name] = sorted(seen | {j["id"] for j in jobs})  # union: a capped search must not re-alert on drift
    elif first_run:
        print(f"[dry-run] {name}: first run, would queue {len(fresh)} current matches silently")


def _alert_cap_reached(counts):
    """The cap budgets delivery attempts (successful or not); a delivered alert always counts as an attempt too."""
    return max(counts.get("alerted", 0), counts.get("alert_tries", 0)) >= ALERT_CAP_PER_RUN


def _retry_pending_alerts(queue, notify_cfg, counts, dry):
    """Re-sends alerts that failed earlier, under the per-run cap; gives up after ALERT_MAX_ATTEMPTS so a
    dead ntfy topic cannot retry forever. Closed or acted-on entries are dropped from the retry list."""
    for e in queue:
        if not e.get("alert_pending") or e["id"] in counts.get("failed_now", ()):
            continue
        if e.get("closed_on") or e.get("state") != "new" or e.get("alert_attempts", 1) >= ALERT_MAX_ATTEMPTS:
            e["alert_pending"] = False
            continue
        if _alert_cap_reached(counts):
            continue  # keep walking: later rows may still need their stale flag cleared
        counts["alert_tries"] = counts.get("alert_tries", 0) + 1
        if notify(notify_cfg, e, dry):
            e["alert_pending"] = False
            counts["alerted"] += 1
        elif not dry:
            e["alert_attempts"] = e.get("alert_attempts", 1) + 1


def _recall_company(c, name, hits, seen, queued_ids, tombstones, pool, queue, keywords, cfg, args, counts):
    """--recall only: queue roles that match the filters now, were fetched before (so are in `seen`) yet never
    reached the queue, e.g. because the filters were narrower then. Silent, tagged origin=recall, capped, and it
    skips tombstoned ids and anything old enough to be pruned straight away."""
    cutoff = (date.today() - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    todo = []
    for j in hits:
        eid = f"{name}:{j['id']}"
        if j["id"] not in seen or eid in queued_ids or eid in tombstones:
            continue
        if (j.get("posted") or "")[:10] and j["posted"][:10] < cutoff:
            continue
        todo.append(j)
    room = RECALL_MAX - counts.get("recalled", 0)
    todo = todo[:max(room, 0)]
    if not todo:
        return
    counts["recalled"] = counts.get("recalled", 0) + len(todo)
    print(f"[recall{' dry-run' if args.dry_run else ''}] {name}: {len(todo)} matching roles never queued")
    if args.dry_run:
        return
    descs = {}
    for j, fut in [(j, pool.submit(describe, c, j)) for j in todo]:
        try:
            text, posted, posted_source = fut.result()
            descs[j["id"]] = text
            if posted:
                j["posted"], j["posted_source"] = posted, posted_source
        except Exception as e:  # noqa: BLE001
            print(f"! {name} description for {j['title']}: {e}", file=sys.stderr)
            descs[j["id"]] = ""
    before = len(queue)
    _process_company(c, name, todo, descs, True, [], True, seen, {}, queue, queued_ids, keywords, cfg, args, counts, silent=True)
    for e in queue[before:]:
        e["origin"] = "recall"


def _send_summaries(notify_cfg, floods, counts, dry):
    """One alert per flooded company, plus one for roles past the per-run alert cap."""
    lines = [f"{name}: {n} roles appeared at once, queued silently (flood guard)" for name, n in floods]
    if counts.get("alert_capped"):
        lines.append(f"{counts['alert_capped']} more new roles queued without individual alerts (cap {ALERT_CAP_PER_RUN})")
    path = HOME / "pending_summaries.json"
    pending = [] if dry else load_json(path, [])
    todo = pending + [dict(line=line, attempts=0) for line in lines]
    still = []
    for item in todo:
        ok = notify(notify_cfg, dict(company="Summary", role=item["line"], location="", sponsorship_status="n/a", kw_hits=[],
                                     link=notify_cfg.get("tracker_url") or "https://ntfy.sh"), dry)
        if not ok and not dry and item["attempts"] + 1 < ALERT_MAX_ATTEMPTS:
            still.append(dict(line=item["line"], attempts=item["attempts"] + 1))
    if not dry and (still or pending):
        HOME.mkdir(parents=True, exist_ok=True)
        atomic_json.write(str(path), still)


def _still_listed(c, e):
    """Closure double-check for term-search boards (Workday, Oracle, Eightfold): True unless the posting's own
    detail lookup definitively says it is gone. A list miss alone must not close it, because a keyword
    search can drop a live posting. Any failure answers True so an unverifiable posting stays open."""
    ats = c.get("ats")
    if ats == "workday":
        return _workday_still_listed(c, e)
    pid = e["id"].split(":", 1)[1]
    try:
        if ats == "oracle":
            d = http(f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?"
                     + urllib.parse.urlencode({"onlyData": "true", "finder": f"ById;Id={pid}"}))
            return bool(d.get("items"))
        if ats == "eightfold":
            http(f"https://{c['host']}/api/apply/v2/jobs/{pid}?domain={c['domain']}", headers={"Accept": "application/json"})
            return True
        if ats == "smartrecruiters":
            http(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings/{pid}")
            return True
    except urllib.error.HTTPError as ex:
        return ex.code not in (404, 410)
    except Exception:
        return True
    return True


def _workday_still_listed(c, e):
    """True when a requisition the list diff missed is still on the Workday board (searched by its req id).
    Keyword-term searches occasionally drop a live posting, so a miss alone must not close it. Any
    failure answers True: an unverifiable posting stays open rather than being closed on a guess."""
    path = e["id"].split(":", 1)[1]
    tok = re.search(r"_([A-Za-z0-9\-]+)$", path)
    if not tok:
        return True
    req = tok.group(1)
    if re.match(r"^[A-Za-z]*-?\d+-\d+$", req):
        req = req.rsplit("-", 1)[0]  # a trailing -N is a repost suffix, the search index holds the base req id
    site = e.get("site") or (c["site"][0] if isinstance(c.get("site"), list) else c.get("site"))
    try:
        d = http(f"https://{c['host']}/wday/cxs/{c['tenant']}/{site}/jobs",
                 data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": req})
        return any(req in (p.get("externalPath") or "") for p in d.get("jobPostings", []))
    except Exception:
        return True


def _update_liveness(name, jobs, complete, queue, counts, c=None):
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
        # hourly runs make two misses only two hours: also require the last confirmed sighting to be before today
        if e["miss_count"] >= close_after and (e.get("last_seen_live") or "") < today:
            if c and c.get("ats") in ("workday", "oracle", "eightfold", "smartrecruiters") and _still_listed(c, e):
                e["last_seen_live"], e["miss_count"] = today, 0  # live but dropped out of the keyword searches
                continue
            e["closed_on"], e["closed_reason"] = today, "removed"
            counts["closed"] += 1


def _load_tombstones():
    path = HOME / "removed.jsonl"
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["id"])
        except (ValueError, KeyError):
            continue
    return out


def _write_tombstones(ids, reason):
    """Append-only record of queue entries removed on purpose, so --recall never resurrects them."""
    if not ids:
        return
    HOME.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    with open(HOME / "removed.jsonl", "a", encoding="utf-8", newline="\n") as f:
        for i in sorted(ids):
            f.write(json.dumps(dict(id=i, reason=reason, on=today)) + "\n")


def _prune_stale(queue, counts, unverified=frozenset(), configured=None):
    """Drops "new"/"auto_blocked" entries whose employer-posted date is older than
    PRUNE_AFTER_DAYS, so queue.json (and the HTML page rendering it) doesn't grow forever. Never
    touches shortlisted/applied/dismissed - those came from an explicit `--mark`, a human decision
    the scout has no business overriding just because time passed. An entry with no cleanly-parsed
    posted date is kept rather than guessed at. Entries of `unverified` companies (fetch failed, shard
    missing, truncated or empty listing this run) are kept too: absence from a bad fetch proves nothing."""
    cutoff = (date.today() - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    kept = []
    pruned = 0
    for e in queue:
        posted = e.get("posted") or ""  # null dates exist in the queue; len(None) would crash the prune
        # an old posting still on the employer's board today is live (employers bump/relist), so keep it
        still_live = e.get("last_seen_live") == date.today().isoformat() and not e.get("closed_on")
        is_stale = (e["state"] in ("new", "opt_only", "auto_blocked") and len(posted) >= 10 and posted[:10] < cutoff
                    and not still_live and e["company"] not in unverified
                    and (e.get("closed_on") or (configured is not None and e["company"] not in configured)))
        if is_stale:
            pruned += 1
            counts.setdefault("pruned_ids", []).append(e["id"])
        else:
            kept.append(e)
    counts["pruned"] = pruned
    queue[:] = kept


def main():
    ap = argparse.ArgumentParser(description="Free ATS job discovery, standalone (no tracker dedup).")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recall", action="store_true", help="also queue matching roles that were fetched before but never queued (silent, capped, manual)")
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--all", action="store_true", help="with --queue: include every state")
    ap.add_argument("--json", action="store_true", help="with --queue: JSON output")
    ap.add_argument("--days", type=int, help="with --queue: only postings posted within the last N days")
    ap.add_argument("--html", metavar="PATH", help="with --queue: write a clickable HTML page instead of stdout")
    ap.add_argument("--mark", nargs=2, metavar=("ID", "STATE"))
    ap.add_argument("--fetch-shard", metavar="I/N", help="fetch only shard I of N and write --out, no state changes")
    ap.add_argument("--out", metavar="PATH", help="with --fetch-shard: result file")
    ap.add_argument("--from-fetched", metavar="DIR", help="run using shard result files instead of fetching")
    args = ap.parse_args()
    if args.fetch_shard:
        return cmd_fetch_shard(args)
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
